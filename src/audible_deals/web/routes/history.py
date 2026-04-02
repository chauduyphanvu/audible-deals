"""History and recap blueprints — price history and recap pages."""

from __future__ import annotations

import datetime
import json

from flask import Blueprint, abort, current_app, render_template, request

from audible_deals.cli import (
    HISTORY_DIR,
    _ASIN_RE,
    _load_wishlist,
)
from . import currency, get_locale

bp = Blueprint("history", __name__)


def _relative_date(date_str: str, today: datetime.date) -> str:
    try:
        d = datetime.date.fromisoformat(date_str)
        delta = (today - d).days
        if delta == 0:
            return "today"
        elif delta == 1:
            return "yesterday"
        elif delta < 7:
            return f"{delta}d ago"
        elif delta < 30:
            return f"{delta // 7}w ago"
        else:
            return f"{delta // 30}mo ago"
    except ValueError:
        return ""


@bp.get("/history/<asin>")
def history_page(asin: str):
    """Price history page for a single ASIN."""
    if not _ASIN_RE.fullmatch(asin):
        abort(400, "Invalid ASIN")

    cur = currency()
    try:
        entries = json.loads((HISTORY_DIR / f"{asin}.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        entries = []

    if not entries:
        return render_template(
            "history.html",
            asin=asin,
            title=None,
            entries=[],
            low=None,
            high=None,
            current=None,
            sparkline_points="",
            currency=cur,
            active_page=None,
        )

    today = datetime.date.today()

    # Enrich each entry with relative date and change delta
    enriched: list[dict] = []
    prev_price: float | None = None
    for entry in entries:
        price = entry.get("price")
        change: float | None = None
        if prev_price is not None and price is not None:
            change = round(price - prev_price, 2)
        enriched.append({
            "date": entry.get("date", ""),
            "relative": _relative_date(entry.get("date", ""), today),
            "price": price,
            "title": entry.get("title", ""),
            "change": change,
        })
        prev_price = price

    # Stats
    prices = [e["price"] for e in enriched if e["price"] is not None]
    low = min(prices) if prices else None
    high = max(prices) if prices else None
    current = prices[-1] if prices else None

    # Title: most recent entry with a non-empty title
    title = ""
    for e in reversed(enriched):
        if e.get("title"):
            title = e["title"]
            break

    # SVG sparkline polyline: scale prices to a 200x40 viewBox
    sparkline_points = ""
    if len(prices) > 1:
        lo, hi = min(prices), max(prices)
        width = 200
        height = 40
        n = len(prices)
        step = width / (n - 1) if n > 1 else width
        pts = []
        for i, p in enumerate(prices):
            x = round(i * step, 1)
            # Invert Y axis (SVG 0 is top, we want high price at top)
            if hi == lo:
                y = height / 2
            else:
                y = round(height - (p - lo) / (hi - lo) * height, 1)
            pts.append(f"{x},{y}")
        sparkline_points = " ".join(pts)

    return render_template(
        "history.html",
        asin=asin,
        title=title,
        entries=enriched,
        low=low,
        high=high,
        current=current,
        sparkline_points=sparkline_points,
        currency=cur,
        active_page=None,
    )


@bp.get("/recap")
def recap_page():
    """Price recap page — price drops, new items, and wishlist hits."""
    cur = currency()
    try:
        days = int(request.args.get("days", 7))
        if days < 1:
            days = 7
    except (ValueError, TypeError):
        days = 7

    if not HISTORY_DIR.exists():
        return render_template(
            "recap.html",
            drops=[],
            new_items=[],
            wishlist_hits=[],
            days=days,
            currency=cur,
            active_page="recap",
        )

    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()

    drops: list[dict] = []
    new_items: list[dict] = []

    for hist_file in HISTORY_DIR.glob("*.json"):
        asin = hist_file.stem
        if not _ASIN_RE.fullmatch(asin):
            continue
        try:
            entries = json.loads(hist_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not entries:
            continue

        title = ""
        for e in reversed(entries):
            if e.get("title"):
                title = e["title"]
                break

        recent = [e for e in entries if e.get("date", "") >= cutoff]
        if not recent:
            continue

        # New item: all entries are within the window
        if entries[0].get("date", "") >= cutoff and len(entries) == len(recent):
            new_items.append({
                "asin": asin,
                "title": title,
                "price": entries[-1]["price"],
            })
            continue

        # Price drop
        before = [e for e in entries if e.get("date", "") < cutoff]
        if before and recent:
            old_price = before[-1]["price"]
            new_price = recent[-1]["price"]
            if new_price < old_price:
                savings = round(old_price - new_price, 2)
                drops.append({
                    "asin": asin,
                    "title": title,
                    "old_price": old_price,
                    "new_price": new_price,
                    "savings": savings,
                })

    # Sort drops by savings descending
    drops.sort(key=lambda x: x["savings"], reverse=True)

    # Wishlist hits
    wishlist_items = _load_wishlist()
    wishlist_hits: list[dict] = []
    for item in wishlist_items:
        item_asin = item.get("asin", "")
        if not _ASIN_RE.fullmatch(item_asin):
            continue
        hist_file = HISTORY_DIR / f"{item_asin}.json"
        if not hist_file.exists():
            continue
        try:
            entries = json.loads(hist_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        max_price = item.get("max_price")
        if entries and max_price is not None and entries[-1]["price"] <= max_price:
            wishlist_hits.append({
                "asin": item_asin,
                "title": item.get("title", ""),
                "price": entries[-1]["price"],
                "target": max_price,
            })

    return render_template(
        "recap.html",
        drops=drops,
        new_items=new_items,
        wishlist_hits=wishlist_hits,
        days=days,
        currency=cur,
        active_page="recap",
    )
