"""Click-free notification collection, cooldown, and delivery services."""

from __future__ import annotations

import copy
import datetime
import logging
from dataclasses import dataclass
from typing import Callable

from audible_deals.automation_models import (
    CREDIT_PRICE_REASON,
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
from audible_deals.results_cache import load_dismissed_asins
from audible_deals.webhook_client import WebhookClient
from audible_deals.webhooks import (
    format_monitor_webhook_payload,
    format_webhook_message,
    format_webhook_payload,
)
from audible_deals.wishlist import inspect_wishlist, load_wishlist

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NotificationRuntime:
    get_client: Callable[[str], DealsClient]
    record_products: Callable[[list[Product]], None]
    webhook_client: WebhookClient
    load_wishlist: Callable[[], list[dict]] = load_wishlist
    load_notify_state: Callable[[], dict] = load_notify_state
    save_notify_state: Callable[[dict], None] = save_notify_state
    load_dismissed_asins: Callable[[], set[str]] = load_dismissed_asins
    today: Callable[[], datetime.date] = datetime.date.today


@dataclass(frozen=True)
class WishlistHits:
    hits: tuple[NotificationHit, ...]
    extras: dict[str, dict]
    hit_asins: frozenset[str]
    account_asins: frozenset[str] = frozenset()
    account_error: str | None = None


def is_auth_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if status in (401, 403):
        return True
    message = str(exc).lower()
    return "not authenticated" in message or "deals login" in message


def _alert_target(
    product: Product, target: float | None, credit_price: float | None
) -> tuple[float, str | None] | None:
    """Return the (target, reason) a product alerts against, or None.

    A book without its own target alerts when buying it with cash beats spending
    a credit: not in the Plus catalog and strictly below the credit price.
    """
    if product.price is None:
        return None
    if target is not None:
        return (target, None) if product.price <= target else None
    if credit_price is not None and buy_verdict(product, credit_price) == "cash":
        return credit_price, CREDIT_PRICE_REASON
    return None


def _fetch_account_wishlist(client: DealsClient) -> tuple[list[Product], str | None]:
    try:
        with client:
            return client.get_wishlist(), None
    except Exception as exc:
        if is_auth_error(exc):
            raise
        logger.exception("Audible wishlist fetch failed")
        return [], f"{type(exc).__name__}: {exc}"


def collect_wishlist_hits(
    get_client: Callable[[str], DealsClient],
    asin_items: list[dict],
    author_items: list[dict],
    record_products: Callable[[list[Product]], None],
    *,
    locale: str,
    credit_price: float | None = None,
    account_wishlist: bool = False,
    load_dismissed_asins: Callable[[], set[str]] = load_dismissed_asins,
) -> WishlistHits:
    """Collect hits for the local wishlist and, optionally, the Audible wishlist.

    Each ASIN entry is priced in its own marketplace; author watches and the
    Audible account wishlist use *locale*, the marketplace the credit price is
    in, so only books priced there can alert below the credit price.
    """
    hits: list[NotificationHit] = []
    extras: dict[str, dict] = {}
    hit_asins: set[str] = set()

    def add(
        product: Product,
        alert: tuple[float, str | None] | None,
        author: str | None = None,
    ) -> None:
        if alert is None or product.asin in hit_asins:
            return
        target, reason = alert
        hit_asins.add(product.asin)
        verdict = (
            buy_verdict(product, credit_price) if credit_price is not None else None
        )
        effective = effective_price(product, credit_price)
        hits.append(
            NotificationHit(
                asin=product.asin,
                title=product.title,
                price=round(product.price, 2),
                target=target,
                url=product.url,
                author=author,
                verdict=verdict,
                effective_price=round(effective, 2) if effective is not None else None,
                reason=reason,
            )
        )
        extras[product.asin] = {
            "currency": product.currency,
            "discount_pct": float(product.discount_pct or 0.0),
        }

    account: dict[str, Product] = {}
    account_error = None
    if account_wishlist:
        listed, account_error = _fetch_account_wishlist(get_client(locale))
        if listed:
            record_products(listed)
        account = {product.asin: product for product in listed}

    by_locale: dict[str, list[dict]] = {}
    for item in asin_items:
        by_locale.setdefault(item.get("locale", locale), []).append(item)
    for item_locale, items in by_locale.items():
        listed_here = account if item_locale == locale else {}
        products = [listed_here[i["asin"]] for i in items if i["asin"] in listed_here]
        missing = [i["asin"] for i in items if i["asin"] not in listed_here]
        if missing:
            client = get_client(item_locale)
            with client:
                fetched = client.get_products_batch(missing)
            record_products(fetched)
            products.extend(fetched)
        targets = {item["asin"]: item.get("max_price") for item in items}
        local_credit = credit_price if item_locale == locale else None
        for product in products:
            add(
                product, _alert_target(product, targets.get(product.asin), local_credit)
            )

    if author_items:
        client = get_client(locale)
        with client:
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
                    add(product, (target, None), author=author)

    if account:
        local_asins = {item["asin"] for item in asin_items}
        dismissed = load_dismissed_asins()
        for asin, product in account.items():
            if asin not in local_asins and asin not in dismissed:
                add(product, _alert_target(product, None, credit_price))

    return WishlistHits(
        hits=tuple(hits),
        extras=extras,
        hit_asins=frozenset(hit_asins),
        account_asins=frozenset(account),
        account_error=account_error,
    )


def cooldown_keep_asins(collected: WishlistHits, notify_state: dict) -> frozenset[str]:
    """ASINs whose cooldown entries survive besides local wishlist entries."""
    keep = collected.hit_asins | collected.account_asins
    if collected.account_error:
        # The Audible wishlist is unknown this run; pruning its entries would
        # let books still in cooldown alert again on the next successful run.
        keep |= frozenset(notify_state)
    return keep


def drop_lapsed_credit_entries(notify_state: dict, collected: WishlistHits) -> dict:
    """Forget below-credit alerts for books no longer below the credit price.

    Without this, a book that rose and later dipped to the same price would
    stay suppressed.
    """
    if collected.account_error:
        return notify_state
    return {
        asin: entry
        for asin, entry in notify_state.items()
        if asin in collected.hit_asins
        or not isinstance(entry, dict)
        or entry.get("reason") != CREDIT_PRICE_REASON
    }


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
            # A book can sit below the credit price for months, so a
            # below-credit alert repeats only when the price falls further.
            standing = (
                hit.reason == CREDIT_PRICE_REASON
                and entry.get("reason") == CREDIT_PRICE_REASON
            )
            if (
                entry
                and hit.price >= float(entry["price"])
                and (
                    standing
                    or (today - datetime.date.fromisoformat(entry["date"])).days
                    < cooldown
                )
            ):
                suppressed += 1
                continue
        except (KeyError, ValueError, TypeError, AttributeError):
            pass
        kept.append(hit)
    return tuple(kept), suppressed


def build_notify_state(
    notify_state: dict,
    hits: tuple[NotificationHit, ...],
    asin_items: list[dict],
    keep_asins: frozenset[str],
    today: datetime.date,
) -> dict:
    updated = copy.deepcopy(notify_state)
    today_iso = today.isoformat()
    for hit in hits:
        updated[hit.asin] = {"price": hit.price, "date": today_iso}
        if hit.reason is not None:
            updated[hit.asin]["reason"] = hit.reason
    keep = {item["asin"] for item in asin_items} | set(keep_asins)
    return {key: value for key, value in updated.items() if key in keep}


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
    if not asin_items and not author_items and not request.audible_wishlist:
        return NotificationRunResult(
            hits=(),
            had_hits=False,
            empty_wishlist=True,
            wishlist_issues=tuple(inspection.issues),
        )

    collected = collect_wishlist_hits(
        runtime.get_client,
        asin_items,
        author_items,
        runtime.record_products,
        locale=request.locale,
        credit_price=request.credit_price,
        account_wishlist=request.audible_wishlist,
        load_dismissed_asins=runtime.load_dismissed_asins,
    )

    had_hits = bool(collected.hits)
    kept = collected.hits
    suppressed = 0
    today: datetime.date | None = None
    notify_state: dict | None = None
    current_state: dict = {}
    if request.cooldown is not None:
        today = runtime.today()
        loaded = runtime.load_notify_state()
        notify_state = loaded if isinstance(loaded, dict) else {}
        current_state = drop_lapsed_credit_entries(notify_state, collected)
        kept, suppressed = apply_cooldown(kept, request.cooldown, today, current_state)

    if kept and request.webhook:
        template = (
            request.webhook_template.read_text(encoding="utf-8")
            if request.webhook_template is not None
            else None
        )
        deliver_hits(
            kept,
            collected.extras,
            request.webhook,
            request.webhook_format,
            request.currency,
            request.webhook_headers,
            runtime.webhook_client,
            template=template,
        )
    pending_notify_state = None
    if notify_state is not None:
        assert today is not None
        if kept:
            current_state = build_notify_state(
                current_state,
                kept,
                asin_items,
                cooldown_keep_asins(collected, current_state),
                today,
            )
        if current_state != notify_state:
            pending_notify_state = current_state

    return NotificationRunResult(
        hits=kept,
        had_hits=had_hits,
        suppressed=suppressed,
        wishlist_issues=tuple(inspection.issues),
        pending_notify_state=pending_notify_state,
        audible_wishlist_error=collected.account_error,
    )
