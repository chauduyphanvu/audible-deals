"""Shared series and series-book identity helpers."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from decimal import Decimal

from audible_deals.product import Product


_NUMERIC_TOKEN = re.compile(r"\d+(?:\.\d+)?")


def normalize_identity_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def series_identity(product: Product) -> str | None:
    series_asin = normalize_identity_text(product.series_asin)
    if series_asin:
        return series_asin
    series_name = normalize_identity_text(product.series_name)
    return series_name or None


def parse_numeric_series_position(position: str) -> Decimal | None:
    position = normalize_identity_text(position)
    numeric_tokens = _NUMERIC_TOKEN.findall(position)
    if len(numeric_tokens) == 1:
        return Decimal(numeric_tokens[0])
    return None


def series_book_identity_from_parts(title: str, position: str) -> str:
    numeric_position = parse_numeric_series_position(position)
    if numeric_position is not None:
        return format(numeric_position.normalize(), "f")
    return normalize_identity_text(title)


def series_book_identity(product: Product) -> str:
    return series_book_identity_from_parts(product.title, product.series_position)


def group_series_books(products: Iterable[Product]) -> dict[str, list[Product]]:
    groups: dict[str, list[Product]] = {}
    seen_books: set[tuple[str, str]] = set()
    for product in products:
        identity = series_identity(product)
        if identity is None:
            continue
        book_key = identity, series_book_identity(product)
        if book_key in seen_books:
            continue
        seen_books.add(book_key)
        groups.setdefault(identity, []).append(product)
    return groups
