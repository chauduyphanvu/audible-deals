"""Watch blueprint — price checking and webhook notifications."""

from __future__ import annotations

import json
import urllib.request

from flask import Blueprint, current_app, render_template, request

from audible_deals.cli import (
    _load_wishlist,
    _validate_webhook_url,
)
from audible_deals.client import DealsClient

from . import currency, get_locale

bp = Blueprint("watch", __name__)


@bp.get("/watch")
def watch_page():
    """Watch prices page."""
    return render_template("watch.html", active_page="watch")


@bp.get("/hx/watch/check")
def hx_watch_check():
    """HTMX: check current prices for all wishlist items."""
    locale = get_locale()
    cur = currency()

    items = _load_wishlist()
    if not items:
        return render_template(
            "partials/_watch_table.html",
            products=[],
            targets={},
            currency=cur,
            hits=0,
            error=None,
        )

    targets: dict[str, float | None] = {
        item["asin"]: item.get("max_price") for item in items
    }

    try:
        with DealsClient(locale=locale) as dc:
            products = dc.get_products_batch([item["asin"] for item in items])
    except Exception as exc:
        return render_template(
            "partials/_watch_table.html",
            products=[],
            targets=targets,
            currency=cur,
            hits=0,
            error=f"Price check failed: {exc}",
        ), 502

    hits = sum(
        1
        for p in products
        if p.price is not None
        and targets.get(p.asin) is not None
        and p.price <= targets[p.asin]
    )

    return render_template(
        "partials/_watch_table.html",
        products=products,
        targets=targets,
        currency=cur,
        hits=hits,
        error=None,
    )


@bp.post("/hx/notify")
def hx_notify():
    """HTMX: validate webhook URL and POST deals to it."""
    webhook = request.form.get("webhook", "").strip()
    locale = get_locale()
    cur = currency()

    if not webhook:
        return _notify_result(
            error="Please enter a webhook URL.",
            sent=0,
            currency=cur,
        ), 422

    try:
        _validate_webhook_url(webhook)
    except Exception as exc:
        return _notify_result(
            error=str(exc),
            sent=0,
            currency=cur,
        ), 422

    items = _load_wishlist()
    if not items:
        return _notify_result(
            error="Wishlist is empty.",
            sent=0,
            currency=cur,
        )

    # Only items with a target price can ever be hits — skip the rest.
    targeted = [item for item in items if item.get("max_price") is not None]
    if not targeted:
        return _notify_result(
            error=None,
            sent=0,
            currency=cur,
            info="No target prices set. Add target prices to your wishlist items to receive notifications.",
        )

    targets: dict[str, float] = {
        item["asin"]: item["max_price"] for item in targeted
    }

    try:
        with DealsClient(locale=locale) as dc:
            products = dc.get_products_batch([item["asin"] for item in targeted])
    except Exception as exc:
        return _notify_result(
            error=f"Price check failed: {exc}",
            sent=0,
            currency=cur,
        ), 502

    hits = []
    for p in products:
        target = targets.get(p.asin)
        if target is not None and p.price is not None and p.price <= target:
            hits.append({
                "asin": p.asin,
                "title": p.title,
                "price": round(p.price, 2),
                "target": target,
                "url": p.url,
            })

    if not hits:
        return _notify_result(
            error=None,
            sent=0,
            currency=cur,
            info="No items are currently at or below their target price.",
        )

    payload = json.dumps({"deals": hits, "count": len(hits)}).encode()
    req = urllib.request.Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        return _notify_result(
            error=f"Webhook delivery failed: {exc}",
            sent=0,
            currency=cur,
        ), 502

    return _notify_result(
        error=None,
        sent=len(hits),
        currency=cur,
    )


def _notify_result(
    *,
    error: str | None,
    sent: int,
    currency: str,
    info: str | None = None,
) -> str:
    return render_template(
        "partials/_notify_result.html",
        error=error,
        sent=sent,
        currency=cur,
        info=info,
    )
