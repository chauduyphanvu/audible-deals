"""Click-free notification collection, cooldown, and delivery services."""

from __future__ import annotations

import copy
import datetime
from dataclasses import dataclass
from typing import Callable

from audible_deals.automation_models import (
    MonitorDefinition,
    MonitorEvent,
    NotificationHit,
    NotificationRequest,
    NotificationRunResult,
)
from audible_deals.client import DealsClient
from audible_deals.config_store import load_notify_state, save_notify_state
from audible_deals.constants import LOCALE_CURRENCY
from audible_deals.filtering import filter_products
from audible_deals.metrics import buy_verdict, effective_price
from audible_deals.product import Product
from audible_deals.result_models import FilterContext
from audible_deals.webhook_client import WebhookClient
from audible_deals.webhooks import (
    format_monitor_webhook_payload,
    format_webhook_message,
    format_webhook_payload,
)
from audible_deals.wishlist import inspect_wishlist, load_wishlist


@dataclass(frozen=True)
class NotificationRuntime:
    get_client: Callable[[str], DealsClient]
    record_products: Callable[[list[Product]], None]
    webhook_client: WebhookClient
    load_wishlist: Callable[[], list[dict]] = load_wishlist
    load_notify_state: Callable[[], dict] = load_notify_state
    save_notify_state: Callable[[dict], None] = save_notify_state
    today: Callable[[], datetime.date] = datetime.date.today


def collect_target_hits(
    client: DealsClient,
    asin_items: list[dict],
    author_items: list[dict],
    record_products: Callable[[list[Product]], None],
    credit_price: float | None = None,
) -> tuple[tuple[NotificationHit, ...], dict[str, dict], frozenset[str]]:
    targets = {item["asin"]: item.get("max_price") for item in asin_items}
    hits: list[NotificationHit] = []
    extras: dict[str, dict] = {}
    hit_asins: set[str] = set()

    def add_hit(product: Product, target: float, author: str | None = None) -> None:
        hit_asins.add(product.asin)
        verdict = (
            buy_verdict(product, credit_price) if credit_price is not None else None
        )
        effective = effective_price(product, credit_price)
        hit = NotificationHit(
            asin=product.asin,
            title=product.title,
            price=round(product.price, 2),
            target=target,
            url=product.url,
            author=author,
            verdict=verdict,
            effective_price=round(effective, 2) if effective is not None else None,
        )
        hits.append(hit)
        extras[product.asin] = {
            "currency": product.currency,
            "discount_pct": float(product.discount_pct or 0.0),
        }

    with client:
        products = client.get_products_batch([item["asin"] for item in asin_items])
        record_products(products)
        for product in products:
            target = targets.get(product.asin)
            if (
                target is not None
                and product.price is not None
                and product.price <= target
            ):
                add_hit(product, target)

        for watch in author_items:
            author = watch.get("author", "")
            target = watch.get("max_price")
            if not author or target is None:
                continue
            author_results: list[Product] = []
            for page_products, _, _ in client.search_pages(
                keywords=author,
                sort_by="Relevance",
                max_pages=2,
            ):
                author_results.extend(page_products)
            outcome = filter_products(
                author_results,
                FilterContext(
                    author=author,
                    max_price=target,
                    drop_zero_length=True,
                ),
            )
            filtered = list(outcome.products)
            record_products(filtered)
            for product in filtered:
                if product.asin not in hit_asins:
                    add_hit(product, target, author=author)

    return tuple(hits), extras, frozenset(hit_asins)


def apply_cooldown(
    hits: tuple[NotificationHit, ...],
    cooldown: int,
    today: datetime.date,
    notify_state: dict,
) -> tuple[tuple[NotificationHit, ...], int]:
    suppressed = 0
    kept: list[NotificationHit] = []
    for hit in hits:
        entry = notify_state.get(hit.asin)
        try:
            if (
                entry
                and hit.price >= float(entry["price"])
                and (today - datetime.date.fromisoformat(entry["date"])).days < cooldown
            ):
                suppressed += 1
                continue
        except (KeyError, ValueError, TypeError):
            pass
        kept.append(hit)
    return tuple(kept), suppressed


def build_notify_state(
    notify_state: dict,
    hits: tuple[NotificationHit, ...],
    asin_items: list[dict],
    all_hit_asins: frozenset[str],
    today: datetime.date,
) -> dict:
    updated = copy.deepcopy(notify_state)
    today_iso = today.isoformat()
    for hit in hits:
        updated[hit.asin] = {"price": hit.price, "date": today_iso}
    keep_asins = {item["asin"] for item in asin_items} | set(all_hit_asins)
    return {key: value for key, value in updated.items() if key in keep_asins}


def commit_notification_state(
    result: NotificationRunResult, runtime: NotificationRuntime
) -> None:
    if result.pending_notify_state is not None:
        runtime.save_notify_state(copy.deepcopy(result.pending_notify_state))


def deliver_hits(
    hits: tuple[NotificationHit, ...],
    extras: dict[str, dict],
    url: str,
    fmt: str,
    currency: str,
    headers: dict[str, str],
    webhook_client: WebhookClient,
    *,
    template: str | None = None,
) -> None:
    body, payload_headers = format_webhook_payload(
        [hit.to_dict() for hit in hits],
        fmt,
        currency=currency,
        template=template,
        extras=extras,
    )
    webhook_client.post(url, body, {**payload_headers, **headers})


def deliver_monitor_events(
    events: tuple[MonitorEvent, ...],
    monitor: MonitorDefinition,
    global_webhook: str | None,
    global_format: str,
    global_headers: dict[str, str],
    webhook_client: WebhookClient,
) -> None:
    override = monitor.webhook is not None
    destination = monitor.webhook if override else global_webhook
    if not destination:
        return
    body, payload_headers = format_monitor_webhook_payload(
        [event.to_dict() for event in events],
        monitor.webhook_format or global_format,
        locale=monitor.locale,
        currency=LOCALE_CURRENCY.get(monitor.locale, "$"),
    )
    headers = {} if override else global_headers
    webhook_client.post(destination, body, {**payload_headers, **headers})


def deliver_auth_error(
    error: str,
    url: str,
    fmt: str,
    headers: dict[str, str],
    webhook_client: WebhookClient,
) -> None:
    body, payload_headers = format_webhook_message(
        f"Background tracking failed: {error}\nRun 'deals login' to re-authenticate.",
        fmt,
        title="audible-deals needs attention",
    )
    webhook_client.post(url, body, {**payload_headers, **headers})


def run_notification(
    request: NotificationRequest, runtime: NotificationRuntime
) -> NotificationRunResult:
    inspection = inspect_wishlist(runtime.load_wishlist())
    asin_items = inspection.asin_items
    author_items = inspection.author_items
    if not asin_items and not author_items:
        return NotificationRunResult(
            hits=(),
            had_hits=False,
            empty_wishlist=True,
            wishlist_issues=tuple(inspection.issues),
        )

    hits: list[NotificationHit] = []
    extras: dict[str, dict] = {}
    all_hit_asins: set[str] = set()

    def collect(items: list[dict], authors: list[dict], locale: str) -> None:
        new_hits, new_extras, _ = collect_target_hits(
            runtime.get_client(locale),
            items,
            authors,
            runtime.record_products,
            credit_price=request.credit_price,
        )
        for hit in new_hits:
            if hit.asin not in all_hit_asins:
                hits.append(hit)
                all_hit_asins.add(hit.asin)
                if hit.asin in new_extras:
                    extras[hit.asin] = new_extras[hit.asin]

    by_locale: dict[str, list[dict]] = {}
    for item in asin_items:
        by_locale.setdefault(item.get("locale", request.locale), []).append(item)
    for locale, locale_items in by_locale.items():
        collect(locale_items, [], locale)
    if author_items:
        collect([], author_items, request.locale)

    had_hits = bool(hits)
    kept = tuple(hits)
    suppressed = 0
    today: datetime.date | None = None
    notify_state: dict | None = None
    if request.cooldown is not None:
        today = runtime.today()
        loaded = runtime.load_notify_state()
        notify_state = loaded if isinstance(loaded, dict) else {}
        kept, suppressed = apply_cooldown(kept, request.cooldown, today, notify_state)

    if kept and request.webhook:
        template = (
            request.webhook_template.read_text(encoding="utf-8")
            if request.webhook_template is not None
            else None
        )
        deliver_hits(
            kept,
            extras,
            request.webhook,
            request.webhook_format,
            request.currency,
            request.webhook_headers,
            runtime.webhook_client,
            template=template,
        )
    pending_notify_state = None
    if kept and notify_state is not None:
        assert today is not None
        pending_notify_state = build_notify_state(
            notify_state,
            kept,
            asin_items,
            frozenset(all_hit_asins),
            today,
        )

    return NotificationRunResult(
        hits=kept,
        had_hits=had_hits,
        suppressed=suppressed,
        wishlist_issues=tuple(inspection.issues),
        pending_notify_state=pending_notify_state,
    )
