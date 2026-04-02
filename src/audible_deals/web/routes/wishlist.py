"""Wishlist blueprint — manage the audiobook watchlist."""

from __future__ import annotations

from flask import Blueprint, abort, current_app, render_template, request

from audible_deals.cli import (
    _ASIN_RE,
    _load_wishlist,
    _save_wishlist,
    _wishlist_entry,
)
from audible_deals.client import DealsClient

from . import currency, get_locale

bp = Blueprint("wishlist", __name__)


@bp.get("/wishlist")
def wishlist_page():
    """Wishlist management page."""
    items = _load_wishlist()
    return render_template(
        "wishlist.html",
        items=items,
        currency=currency(),
        active_page="wishlist",
    )


@bp.post("/hx/wishlist/add")
def hx_wishlist_add():
    """HTMX: add an item to the wishlist by ASIN."""
    asin = request.form.get("asin", "").strip().upper()
    max_price_raw = request.form.get("max_price", "").strip()
    cur = currency()

    def _table(items, error=None, toast=None):
        return render_template(
            "partials/_wishlist_table.html",
            items=items,
            currency=cur,
            error=error,
            toast=toast,
        )

    items = _load_wishlist()

    if not _ASIN_RE.fullmatch(asin):
        return _table(items, error="Invalid ASIN format. ASINs are 2–14 alphanumeric characters."), 422

    max_price: float | None = None
    if max_price_raw:
        try:
            max_price = float(max_price_raw)
            if max_price <= 0:
                raise ValueError
        except ValueError:
            return _table(items, error="Target price must be a positive number."), 422

    existing = {item["asin"] for item in items}
    if asin in existing:
        return _table(items, error=f"{asin} is already on your wishlist."), 409

    locale = get_locale()
    try:
        with DealsClient(locale=locale) as dc:
            product = dc.get_product(asin)
    except ValueError:
        return _table(items, error=f"Product not found: {asin}"), 404
    except Exception as exc:
        return _table(items, error=f"API error: {exc}"), 502

    items.append(_wishlist_entry(product, max_price))
    _save_wishlist(items)

    return _table(items, toast=f"\u201c{product.title}\u201d added to wishlist.")


@bp.delete("/hx/wishlist/<asin>")
def hx_wishlist_remove(asin: str):
    """HTMX: remove an item from the wishlist."""
    if not _ASIN_RE.fullmatch(asin):
        abort(400, "Invalid ASIN")

    items = _load_wishlist()
    items = [i for i in items if i["asin"] != asin]
    _save_wishlist(items)

    return render_template(
        "partials/_wishlist_table.html",
        items=items,
        currency=currency(),
        error=None,
        toast="Removed from wishlist.",
    )


@bp.post("/hx/wishlist/sync")
def hx_wishlist_sync():
    """HTMX: sync wishlist from Audible account, merging with local list."""
    locale = get_locale()
    try:
        with DealsClient(locale=locale) as dc:
            remote_products = dc.get_wishlist()
    except Exception as exc:
        items = _load_wishlist()
        return render_template(
            "partials/_wishlist_table.html",
            items=items,
            currency=currency(),
            error=f"Sync failed: {exc}",
        ), 502

    items = _load_wishlist()
    existing = {item["asin"] for item in items}
    added = 0
    for product in remote_products:
        if product.asin not in existing:
            items.append(_wishlist_entry(product, None))
            existing.add(product.asin)
            added += 1

    _save_wishlist(items)

    return render_template(
        "partials/_wishlist_table.html",
        items=items,
        currency=currency(),
        error=None,
        toast=f"Synced from Audible — {added} new item(s) added." if added else "Already up to date.",
    )
