"""Wishlist persistence and entry construction."""

from __future__ import annotations

import datetime

from audible_deals import constants
from audible_deals.product import Product
from audible_deals.storage import load_json_file, save_json_file


def load_wishlist() -> list[dict]:
    return load_json_file(constants.WISHLIST_FILE, list, "wishlist")


def save_wishlist(items: list[dict]) -> None:
    save_json_file(constants.WISHLIST_FILE, items, "wishlist")


def partition_wishlist(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split wishlist entries into (asin_items, author_items)."""
    return (
        [i for i in items if i.get("asin")],
        [i for i in items if i.get("type") == "author"],
    )


def wishlist_entry(product: Product, max_price: float | None) -> dict:
    """Build a wishlist dict from a Product."""
    return {
        "asin": product.asin,
        "title": product.title,
        "max_price": max_price,
        "added": datetime.date.today().isoformat(),
    }
