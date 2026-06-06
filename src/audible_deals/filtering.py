"""Pure filtering, sorting, and deduplication functions for product lists.

All functions in this module are pure — no I/O, no file paths, no state.
They operate on ``list[Product]`` and return transformed lists.
"""

from __future__ import annotations

import logging

from audible_deals.client import Product
from audible_deals.metrics import price_per_hour, value_score
from audible_deals.parsing import parse_series_position

logger = logging.getLogger(__name__)


def filter_products(
    products: list[Product],
    *,
    max_price: float | None = None,
    min_rating: float = 0.0,
    min_ratings: int = 0,
    min_hours: float = 0.0,
    language: str = "",
    narrator: str = "",
    author: str = "",
    exclude_authors: tuple[str, ...] = (),
    exclude_narrators: tuple[str, ...] = (),
    on_sale: bool = False,
    skip_asins: set[str] | None = None,
    exclude_category_ids: set[str] | None = None,
    genre: str = "",
    max_pph: float | None = None,
    min_discount: int = 0,
    series: str = "",
    publisher: str = "",
    skip_plus: bool = False,
    only_plus: bool = False,
    exclude_keywords: tuple[str, ...] = (),
    drop_zero_length: bool = False,
    max_hist_percentile: int | None = None,
    hist_percentile: dict[str, int] | None = None,
    min_price_drop: float = 0.0,
    price_drops: dict[str, float] | None = None,
    require_history: bool = False,
    released_after: str = "",
    released_before: str = "",
) -> tuple[list[Product], dict[str, int]]:
    """Apply client-side filters. Returns (filtered, breakdown_by_filter)."""
    filtered = products
    breakdown: dict[str, int] = {}

    def _apply(label: str, keep) -> None:
        nonlocal filtered
        before = len(filtered)
        filtered = [p for p in filtered if keep(p)]
        if removed := before - len(filtered):
            breakdown[label] = removed

    if drop_zero_length:
        _apply("no runtime", lambda p: p.length_minutes != 0)

    if skip_asins:
        _apply("owned", lambda p: p.asin not in skip_asins)

    if max_price is not None:
        _apply("max price", lambda p: p.price is not None and p.price <= max_price)

    if min_rating > 0:
        _apply("min rating", lambda p: p.rating >= min_rating)

    if min_ratings > 0:
        _apply("min ratings", lambda p: p.num_ratings >= min_ratings)

    if min_hours > 0:
        _apply("min hours", lambda p: p.hours >= min_hours)

    if max_pph is not None:
        _apply("max $/hr", lambda p: price_per_hour(p) <= max_pph)

    if language:
        lang_lower = language.lower()
        _apply("language", lambda p: p.language.lower() == lang_lower)

    if narrator:
        narrator_lower = narrator.lower()
        _apply(
            "narrator", lambda p: any(narrator_lower in n.lower() for n in p.narrators)
        )

    if author:
        author_lower = author.lower()
        _apply("author", lambda p: any(author_lower in a.lower() for a in p.authors))

    if series:
        series_lower = series.lower()
        _apply("series", lambda p: series_lower in p.series_name.lower())

    if publisher:
        publisher_lower = publisher.lower()
        _apply("publisher", lambda p: publisher_lower in p.publisher.lower())

    if exclude_authors:
        excl_authors = [a.lower() for a in exclude_authors]
        _apply(
            "excluded authors",
            lambda p: (
                not any(
                    ex in a for a in map(str.lower, p.authors) for ex in excl_authors
                )
            ),
        )

    if exclude_narrators:
        excl_narrators = [n.lower() for n in exclude_narrators]
        _apply(
            "excluded narrators",
            lambda p: (
                not any(
                    ex in n
                    for n in map(str.lower, p.narrators)
                    for ex in excl_narrators
                )
            ),
        )

    if on_sale and min_discount <= 0:
        _apply("on sale", lambda p: p.discount_pct is not None and p.discount_pct > 0)

    if min_discount > 0:
        _apply(
            "min discount",
            lambda p: p.discount_pct is not None and p.discount_pct >= min_discount,
        )

    if exclude_category_ids:
        _apply(
            "excluded genres",
            lambda p: not any(cid in exclude_category_ids for cid in p.category_ids),
        )

    if genre:
        genre_lower = genre.lower()
        _apply("genre", lambda p: any(genre_lower in c.lower() for c in p.categories))

    if skip_plus:
        _apply("plus catalog", lambda p: not p.in_plus_catalog)
    elif only_plus:
        _apply("not plus", lambda p: p.in_plus_catalog)

    if exclude_keywords:
        keywords_lower = [k.lower() for k in exclude_keywords]
        _apply(
            "excluded keywords",
            lambda p: (
                not any(
                    k in p.title.lower() or k in p.subtitle.lower()
                    for k in keywords_lower
                )
            ),
        )

    if max_hist_percentile is not None and hist_percentile is not None:
        _apply(
            "hist percentile",
            lambda p: (
                hist_percentile[p.asin] <= max_hist_percentile
                if p.asin in hist_percentile
                else not require_history
            ),
        )

    if min_price_drop > 0 and price_drops is not None:
        _apply(
            "price drop",
            lambda p: (
                price_drops[p.asin] >= min_price_drop
                if p.asin in price_drops
                else not require_history
            ),
        )

    if released_after:
        _apply(
            "released after",
            lambda p: bool(p.release_date) and p.release_date[:10] >= released_after,
        )

    if released_before:
        _apply(
            "released before",
            lambda p: bool(p.release_date) and p.release_date[:10] <= released_before,
        )

    logger.debug(
        "filter_products in=%d out=%d breakdown=%s",
        len(products),
        len(filtered),
        breakdown,
    )
    return filtered, breakdown


def _price_or(p: Product, missing: float) -> float:
    return p.price if p.price is not None else missing


# sort name -> (key function, reverse). Missing prices always sort last.
_SORT_KEYS = {
    "price": (lambda p: _price_or(p, float("inf")), False),
    "-price": (lambda p: _price_or(p, float("-inf")), True),
    "rating": (lambda p: p.rating, True),
    "length": (lambda p: p.length_minutes, True),
    "date": (lambda p: p.release_date or "", True),
    "release-date": (lambda p: p.release_date or "", True),
    "discount": (lambda p: p.discount_pct if p.discount_pct is not None else 0, True),
    "price-per-hour": (price_per_hour, False),
    "value": (lambda p: (value_score(p), p.rating), True),
    "title": (lambda p: p.title.lower(), False),
    "author": (lambda p: p.authors_str.lower(), False),
    "asin": (lambda p: p.asin, False),
    "bestsellers": (lambda p: p.num_ratings, True),
}


def sort_local(products: list[Product], sort: str) -> list[Product]:
    """Re-sort locally when combining pages (server sort is per-page)."""
    if sort not in _SORT_KEYS:
        return products
    key, reverse = _SORT_KEYS[sort]
    return sorted(products, key=key, reverse=reverse)


def dedupe_editions(products: list[Product]) -> tuple[list[Product], int]:
    """Remove duplicate editions of the same book (same series + position).

    Keeps the cheapest edition. Always-on — no flag needed.
    """
    best: dict[tuple[str, str], Product] = {}
    for p in products:
        if not p.series_name or not p.series_position:
            continue
        key = (p.series_name.lower(), p.series_position.lower())
        existing = best.get(key)
        if existing is None or _price_or(p, float("inf")) < _price_or(
            existing, float("inf")
        ):
            best[key] = p

    best_asins = {p.asin for p in best.values()}
    result = []
    removed = 0
    for p in products:
        if not p.series_name or not p.series_position:
            result.append(p)
        elif p.asin in best_asins:
            result.append(p)
            best_asins.discard(p.asin)  # only include first occurrence
        else:
            removed += 1
    if removed:
        logger.debug("dedupe_editions removed=%d", removed)
    return result, removed


def _series_pos(p: Product) -> float:
    return parse_series_position(p.series_position)


def first_in_series(products: list[Product]) -> tuple[list[Product], int]:
    """Keep only the lowest-position item per series (must be <= 1.0).

    Non-series items pass through unchanged. Series whose lowest-available
    position is > 1.0 are excluded entirely (Book 1 wasn't in the result set).
    """
    best: dict[str, tuple[Product, float]] = {}  # key -> (product, position)
    for p in products:
        if not p.series_name:
            continue
        key = p.series_name.lower()
        pos = _series_pos(p)
        existing = best.get(key)
        if existing is None or pos < existing[1]:
            best[key] = (p, pos)

    best_asins = {p.asin for p, pos in best.values() if pos <= 1.0}
    result = []
    collapsed = 0
    for p in products:
        if not p.series_name:
            result.append(p)
        elif p.asin in best_asins:
            result.append(p)
            best_asins.discard(p.asin)
        else:
            collapsed += 1
    if collapsed:
        logger.debug("first_in_series collapsed=%d", collapsed)
    return result, collapsed
