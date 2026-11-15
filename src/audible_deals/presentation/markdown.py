"""Markdown table rendering for pasting results into Reddit and GitHub."""

from __future__ import annotations

import re
from collections.abc import Sequence

from audible_deals.metrics import whispersync_margin, whispersync_wins
from audible_deals.presentation.common import discount_str, price_str, rating_str
from audible_deals.product import Product

_NEWLINES = re.compile(r"\r\n|\r|\n")
_TEXT_ESCAPES = {char: "\\" + char for char in "\\|*_~`"}
_TEXT_TABLE = str.maketrans(_TEXT_ESCAPES)
_LINK_TABLE = str.maketrans({**_TEXT_ESCAPES, "[": "\\[", "]": "\\]"})


def _text(value: str) -> str:
    return _NEWLINES.sub(" ", value).translate(_TEXT_TABLE)


def _link_text(value: str) -> str:
    return _NEWLINES.sub(" ", value).translate(_LINK_TABLE)


def _book_cell(product: Product) -> str:
    cell = f"[{_link_text(product.full_title)}]({product.url})"
    if product.authors_str:
        cell += f" by {_text(product.authors_str)}"
    if product.series_name:
        cell += f" · {_text(product.series_name)}"
        if product.series_position:
            cell += f" #{_text(product.series_position)}"
    return cell


def _price_cell(product: Product) -> str:
    if product.price is None:
        cell = "n/a"
    else:
        cell = price_str(product.price, product.currency)
        discount = product.discount_pct
        if discount is not None and discount > 0 and product.list_price > product.price:
            list_price = price_str(product.list_price, product.currency)
            cell += f" ~~{list_price}~~ ({discount_str(discount)})"
    if product.in_plus_catalog:
        cell += " · Plus"
    if product.owns_kindle:
        if whispersync_wins(product, None):
            cell += " · Kindle owner price"
    elif (margin := whispersync_margin(product, None)) is not None and margin > 0:
        addon = price_str(product.whispersync_price, product.currency)
        cell += f" · [Kindle]({product.kindle_url}) + {addon} narration"
    return cell


def render_markdown(products: Sequence[Product], *, title: str | None = None) -> str:
    """Render products as a Markdown table, with an optional bold title line."""
    lines = [
        "| # | Book | Price | Hours | Rating |",
        "|--:|:-----|------:|------:|:-------|",
    ]
    for index, product in enumerate(products, start=1):
        hours = f"{product.hours:.1f}" if product.hours else ""
        rating = (
            rating_str(product.rating, product.num_ratings) if product.rating else ""
        )
        lines.append(
            f"| {index} | {_book_cell(product)} | {_price_cell(product)}"
            f" | {hours} | {rating} |"
        )
    table = "\n".join(lines) + "\n"
    if not title:
        return table
    count = len(products)
    noun = "book" if count == 1 else "books"
    return f"**{_text(title)}** ({count} {noun})\n\n{table}"
