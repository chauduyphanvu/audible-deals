"""Reusable recap, notification, and webhook delivery workflow."""

from __future__ import annotations

import datetime
import json as json_mod
import logging
import random
import sys
import time
import urllib.request
from collections.abc import Callable

import click

from audible_deals.client import DealsClient
from audible_deals.config_store import load_notify_state, save_notify_state
from audible_deals.display import console, display_recap
from audible_deals.filtering import filter_products
from audible_deals.metrics import buy_verdict, effective_price
from audible_deals.price_history import (
    find_all_atl_hits,
    find_wishlist_atl_hits,
    find_wishlist_hits,
    has_price_history,
    load_all_price_histories,
    scan_price_changes,
)
from audible_deals.product import Product
from audible_deals.validation import validate_webhook_url
from audible_deals.webhooks import (
    format_recap_payload,
    format_webhook_payload,
    parse_webhook_headers as _parse_wh_headers,
)
from audible_deals.wishlist import load_wishlist, partition_wishlist

logger = logging.getLogger(__name__)
_WEBHOOK_RETRY_DELAYS = (2.0, 6.0)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects: a 3xx is raised as an error instead.

    validate_webhook_url only vets the original URL, so following a redirect
    would reach unvetted (possibly internal/metadata) hosts and resend the
    request headers — leaking secrets from --webhook-header.
    """

    def redirect_request(self, *args, **kwargs):
        return None


urllib.request.install_opener(urllib.request.build_opener(_NoRedirectHandler))


def parse_webhook_headers(raw: tuple[str, ...]) -> dict[str, str]:
    """Parse 'Name: Value' strings into a headers dict. Raises UsageError on bad input."""
    try:
        return _parse_wh_headers(raw, strict=True)
    except ValueError as e:
        raise click.UsageError(str(e))


def post_webhook(url: str, body: bytes, headers: dict[str, str]) -> None:
    """POST body to url with headers, retrying up to 3 times. Raises ClickException on failure."""
    import urllib.request

    last_exc: Exception | None = None
    for attempt in range(1, 4):
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            logger.debug(
                "webhook POST %s attempt %d/3 payload_bytes=%d", url, attempt, len(body)
            )
            urllib.request.urlopen(req, timeout=10)
            return
        except Exception as e:
            last_exc = e
            if attempt < 3:
                delay = _WEBHOOK_RETRY_DELAYS[attempt - 1] + random.uniform(-0.3, 0.3)
                logger.warning("webhook POST attempt %d/3 failed: %s", attempt, e)
                time.sleep(max(0.0, delay))
    logger.error("webhook POST failed", exc_info=last_exc)
    raise click.ClickException(f"Webhook failed: {last_exc}")


def empty_recap_payload(days: int, include_atl: bool) -> dict:
    payload: dict = {"days": days, "drops": [], "new_count": 0, "wishlist_hits": []}
    if include_atl:
        payload["atl_hits"] = []
    return payload


def _build_recap_payload(
    days: int,
    drops: list[tuple[str, str, float, float]],
    new_items: list,
    wishlist_hits: list[dict],
    atl_hits: list[dict] | None,
) -> dict:
    """Build the JSON/webhook recap payload from scanned price changes."""

    def _drop_pct(old: float, new: float) -> int:
        return round((old - new) / old * 100) if old > 0 else 0

    payload: dict = {
        "days": days,
        "drops": [
            {
                "asin": asin,
                "title": title,
                "old_price": old,
                "new_price": new,
                "drop_pct": _drop_pct(old, new),
            }
            for asin, title, old, new in sorted(
                drops, key=lambda x: x[2] - x[3], reverse=True
            )
        ],
        "new_count": len(new_items),
        "wishlist_hits": [
            {"asin": h["asin"], "title": h.get("title", "")} for h in wishlist_hits
        ],
    }
    if atl_hits is not None:
        payload["atl_hits"] = [
            {
                "asin": h["asin"],
                "title": h.get("title", ""),
                "price": h["price"],
                "target": h.get("target"),
            }
            for h in atl_hits
        ]
    return payload


def run_recap(
    days,
    show_new,
    atl,
    atl_all,
    json_flag,
    webhook,
    webhook_format,
    currency,
    extra_headers=None,
):
    logger.info(
        "recap days=%s show_new=%s atl=%s atl_all=%s", days, show_new, atl, atl_all
    )
    if json_flag:
        console.file = sys.stderr
    histories = load_all_price_histories()
    drops, new_items = scan_price_changes(days, histories=histories)
    if not drops and not new_items and not has_price_history():
        if json_flag:
            empty = empty_recap_payload(days, atl or atl_all)
            click.echo(json_mod.dumps(empty, indent=2))
            return
        console.print(
            "[dim]No price history yet. Run 'deals find' or 'deals search' to start tracking.[/dim]"
        )
        return
    wishlist_hits = find_wishlist_hits()
    if atl_all:
        atl_hits: list[dict] | None = find_all_atl_hits(histories=histories)
    elif atl:
        atl_hits = find_wishlist_atl_hits()
    else:
        atl_hits = None

    if json_flag or webhook:
        payload = _build_recap_payload(days, drops, new_items, wishlist_hits, atl_hits)
        if json_flag:
            click.echo(json_mod.dumps(payload, indent=2))
            return
        if webhook:
            nothing = (
                not drops
                and not new_items
                and not wishlist_hits
                and not (atl_hits or [])
            )
            if nothing:
                console.print("[dim]Nothing to send.[/dim]")
                return
            try:
                body, headers = format_recap_payload(
                    payload, webhook_format, currency=currency
                )
            except ValueError as e:
                raise click.ClickException(str(e))
            if extra_headers:
                headers = {**headers, **extra_headers}
            post_webhook(webhook, body, headers)
            console.print("[green]Sent recap to webhook[/green]")
            return

    display_recap(
        drops,
        new_items,
        wishlist_hits,
        days,
        currency,
        show_new,
        atl_hits=atl_hits,
        atl_all=atl_all,
    )


def collect_target_hits(
    dc: DealsClient,
    asin_items: list[dict],
    author_items: list[dict],
    record_products: Callable[[list[Product]], None],
    credit_price: float | None = None,
) -> tuple[list[dict], dict[str, dict], set[str]]:
    """Fetch wishlist items and author searches, collecting at-target hits.

    Returns (hits, extras keyed by asin, hit asins).
    """
    targets = {item["asin"]: item.get("max_price") for item in asin_items}

    hits: list[dict] = []
    extras: dict[str, dict] = {}
    hit_asins: set[str] = set()

    def add_hit(p: Product, target: float, author: str | None = None) -> None:
        hit_asins.add(p.asin)
        hit: dict = {
            "asin": p.asin,
            "title": p.title,
            "price": round(p.price, 2),
            "target": target,
            "url": p.url,
        }
        if author is not None:
            hit["author"] = author
        if credit_price is not None:
            hit["verdict"] = buy_verdict(p, credit_price)
            eff = effective_price(p, credit_price)
            hit["effective_price"] = round(eff, 2) if eff is not None else None
        hits.append(hit)
        extras[p.asin] = {
            "currency": p.currency,
            "discount_pct": float(p.discount_pct or 0.0),
        }

    with dc:
        products = dc.get_products_batch([item["asin"] for item in asin_items])
        record_products(products)
        for p in products:
            target = targets.get(p.asin)
            if target is not None and p.price is not None and p.price <= target:
                add_hit(p, target)

        for awatch in author_items:
            author_name = awatch.get("author", "")
            author_target = awatch.get("max_price")
            if not author_name or author_target is None:
                continue
            author_results: list = []
            for page_products, _, _ in dc.search_pages(
                keywords=author_name,
                sort_by="Relevance",
                max_pages=2,
            ):
                author_results.extend(page_products)
            filtered_author, _ = filter_products(
                author_results,
                author=author_name,
                max_price=author_target,
                drop_zero_length=True,
            )
            record_products(filtered_author)
            for p in filtered_author:
                if p.asin not in hit_asins:
                    add_hit(p, author_target, author=author_name)

    return hits, extras, hit_asins


def apply_cooldown(
    hits: list[dict], cooldown: int, today: datetime.date
) -> tuple[list[dict], int, dict]:
    """Drop hits already notified within the cooldown window unless cheaper.

    Returns (kept hits, suppressed count, loaded notify state).
    """
    notify_state = load_notify_state()
    suppressed = 0
    kept: list[dict] = []
    for hit in hits:
        entry = notify_state.get(hit["asin"])
        try:
            if (
                entry
                and hit["price"] >= float(entry["price"])
                and (today - datetime.date.fromisoformat(entry["date"])).days < cooldown
            ):
                suppressed += 1
                continue
        except (KeyError, ValueError, TypeError):
            pass
        kept.append(hit)
    return kept, suppressed, notify_state


def persist_notify_state(
    notify_state: dict,
    hits: list[dict],
    asin_items: list[dict],
    hit_asins: set[str],
    today: datetime.date,
) -> None:
    """Record notified hits and prune state to current wishlist/hit ASINs."""
    wishlist_asins = {item["asin"] for item in asin_items}
    today_iso = today.isoformat()
    for hit in hits:
        notify_state[hit["asin"]] = {"price": hit["price"], "date": today_iso}
    keep_asins = wishlist_asins | hit_asins
    notify_state = {k: v for k, v in notify_state.items() if k in keep_asins}
    save_notify_state(notify_state)


def run_notify(
    webhook,
    webhook_format,
    webhook_template,
    cooldown,
    extra_headers=None,
    *,
    locale: str,
    currency: str,
    credit_price: float | None,
    get_client: Callable[[str], DealsClient],
    record_products: Callable[[list[Product]], None],
) -> bool | None:
    logger.info(
        "notify webhook_set=%s webhook_format=%s webhook_template=%s",
        bool(webhook),
        webhook_format,
        webhook_template,
    )
    if webhook_template is not None and webhook_format != "generic":
        raise click.UsageError(
            "--webhook-template and --webhook-format are mutually exclusive"
        )
    if webhook_template is not None and not webhook:
        raise click.UsageError("--webhook-template requires --webhook")
    if webhook:
        validate_webhook_url(webhook)

    items = load_wishlist()
    if not items:
        console.print("[dim]Wishlist is empty. Use 'deals wishlist add' first.[/dim]")
        return None

    asin_items, author_items = partition_wishlist(items)

    dc = get_client(locale)
    hits, extras, hit_asins = collect_target_hits(
        dc,
        asin_items,
        author_items,
        record_products,
        credit_price=credit_price,
    )
    had_hits = bool(hits)

    suppressed = 0
    if cooldown is not None:
        today = datetime.date.today()
        hits, suppressed, notify_state = apply_cooldown(hits, cooldown, today)

    if not hits:
        if not webhook:
            click.echo(json_mod.dumps({"deals": [], "count": 0}, indent=2))
        else:
            if suppressed:
                console.print(
                    f"[dim]{suppressed} deal(s) suppressed by cooldown. Nothing sent.[/dim]"
                )
            else:
                console.print(
                    "[dim]No items at target price. Nothing sent to webhook.[/dim]"
                )
        return had_hits

    if webhook:
        tmpl_str = (
            webhook_template.read_text(encoding="utf-8")
            if webhook_template is not None
            else None
        )
        try:
            body, headers = format_webhook_payload(
                hits,
                webhook_format,
                currency=currency,
                template=tmpl_str,
                extras=extras,
            )
        except ValueError as e:
            raise click.ClickException(str(e))
        if extra_headers:
            headers = {**headers, **extra_headers}
        post_webhook(webhook, body, headers)
        console.print(f"[green]Sent {len(hits)} deal(s) to webhook[/green]")
    else:
        click.echo(json_mod.dumps({"deals": hits, "count": len(hits)}, indent=2))

    if cooldown is not None:
        persist_notify_state(notify_state, hits, asin_items, hit_asins, today)
    return had_hits
