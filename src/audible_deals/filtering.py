"""Pure filtering, sorting, and deduplication functions for product lists.

All functions in this module are pure — no I/O, no file paths, no state.
They operate on ``list[Product]`` and return transformed lists.
"""

from __future__ import annotations

from audible_deals.client import Product


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
) -> tuple[list[Product], dict[str, int]]:
    """Apply client-side filters. Returns (filtered, breakdown_by_filter)."""
    filtered = products
    breakdown: dict[str, int] = {}

    if skip_asins:
        before = len(filtered)
        filtered = [p for p in filtered if p.asin not in skip_asins]
        if (removed := before - len(filtered)):
            breakdown["owned"] = removed

    if max_price is not None:
        before = len(filtered)
        filtered = [p for p in filtered if p.price is not None and p.price <= max_price]
        if (removed := before - len(filtered)):
            breakdown["max price"] = removed

    if min_rating > 0:
        before = len(filtered)
        filtered = [p for p in filtered if p.rating >= min_rating]
        if (removed := before - len(filtered)):
            breakdown["min rating"] = removed

    if min_ratings > 0:
        before = len(filtered)
        filtered = [p for p in filtered if p.num_ratings >= min_ratings]
        if (removed := before - len(filtered)):
            breakdown["min ratings"] = removed

    if min_hours > 0:
        before = len(filtered)
        filtered = [p for p in filtered if p.hours >= min_hours]
        if (removed := before - len(filtered)):
            breakdown["min hours"] = removed

    if max_pph is not None:
        before = len(filtered)
        filtered = [p for p in filtered if price_per_hour(p) <= max_pph]
        if (removed := before - len(filtered)):
            breakdown["max $/hr"] = removed

    if language:
        before = len(filtered)
        lang_lower = language.lower()
        filtered = [p for p in filtered if p.language.lower() == lang_lower]
        if (removed := before - len(filtered)):
            breakdown["language"] = removed

    if narrator:
        before = len(filtered)
        narrator_lower = narrator.lower()
        filtered = [
            p for p in filtered
            if any(narrator_lower in n.lower() for n in p.narrators)
        ]
        if (removed := before - len(filtered)):
            breakdown["narrator"] = removed

    if author:
        before = len(filtered)
        author_lower = author.lower()
        filtered = [
            p for p in filtered
            if any(author_lower in a.lower() for a in p.authors)
        ]
        if (removed := before - len(filtered)):
            breakdown["author"] = removed

    if series:
        before = len(filtered)
        series_lower = series.lower()
        filtered = [
            p for p in filtered
            if series_lower in p.series_name.lower()
        ]
        if (removed := before - len(filtered)):
            breakdown["series"] = removed

    if publisher:
        before = len(filtered)
        publisher_lower = publisher.lower()
        filtered = [
            p for p in filtered
            if publisher_lower in p.publisher.lower()
        ]
        if (removed := before - len(filtered)):
            breakdown["publisher"] = removed

    if exclude_authors:
        before = len(filtered)
        exclude_lower = [a.lower() for a in exclude_authors]
        filtered = [
            p for p in filtered
            if not any(
                ex in author_lc
                for author_lc in (a.lower() for a in p.authors)
                for ex in exclude_lower
            )
        ]
        if (removed := before - len(filtered)):
            breakdown["excluded authors"] = removed

    if exclude_narrators:
        before = len(filtered)
        exclude_lower = [n.lower() for n in exclude_narrators]
        filtered = [
            p for p in filtered
            if not any(
                ex in narrator_lc
                for narrator_lc in (n.lower() for n in p.narrators)
                for ex in exclude_lower
            )
        ]
        if (removed := before - len(filtered)):
            breakdown["excluded narrators"] = removed

    if on_sale and min_discount <= 0:
        before = len(filtered)
        filtered = [p for p in filtered if p.discount_pct is not None and p.discount_pct > 0]
        if (removed := before - len(filtered)):
            breakdown["on sale"] = removed

    if min_discount > 0:
        before = len(filtered)
        filtered = [
            p for p in filtered
            if p.discount_pct is not None and p.discount_pct >= min_discount
        ]
        if (removed := before - len(filtered)):
            breakdown["min discount"] = removed

    if exclude_category_ids:
        before = len(filtered)
        filtered = [
            p for p in filtered
            if not any(cid in exclude_category_ids for cid in p.category_ids)
        ]
        if (removed := before - len(filtered)):
            breakdown["excluded genres"] = removed

    if genre:
        before = len(filtered)
        genre_lower = genre.lower()
        filtered = [
            p for p in filtered
            if any(genre_lower in cat.lower() for cat in p.categories)
        ]
        if (removed := before - len(filtered)):
            breakdown["genre"] = removed

    return filtered, breakdown


def price_per_hour(p: Product) -> float:
    """Calculate price per hour of audio. Returns inf for missing data."""
    if p.price is None or p.hours <= 0:
        return float("inf")
    return p.price / p.hours


def value_score(p: Product) -> float:
    """Composite value score: (rating * hours) / price. Higher is better."""
    if p.price is None or p.hours <= 0 or p.rating <= 0:
        return 0.0
    if p.price <= 0:
        return float("inf")
    return (p.rating * p.hours) / p.price


def sort_local(products: list[Product], sort: str) -> list[Product]:
    """Re-sort locally when combining pages (server sort is per-page)."""
    if sort == "price":
        return sorted(products, key=lambda p: (p.price if p.price is not None else 9999))
    elif sort == "-price":
        return sorted(products, key=lambda p: (p.price if p.price is not None else 0), reverse=True)
    elif sort == "rating":
        return sorted(products, key=lambda p: p.rating, reverse=True)
    elif sort == "length":
        return sorted(products, key=lambda p: p.length_minutes, reverse=True)
    elif sort in ("date", "release-date"):
        return sorted(products, key=lambda p: p.release_date or "", reverse=True)
    elif sort == "discount":
        return sorted(
            products,
            key=lambda p: p.discount_pct if p.discount_pct is not None else 0,
            reverse=True,
        )
    elif sort == "price-per-hour":
        return sorted(products, key=price_per_hour)
    elif sort == "value":
        return sorted(products, key=lambda p: (value_score(p), p.rating), reverse=True)
    elif sort == "title":
        return sorted(products, key=lambda p: p.title.lower())
    elif sort == "author":
        return sorted(products, key=lambda p: p.authors_str.lower())
    elif sort == "asin":
        return sorted(products, key=lambda p: p.asin)
    elif sort == "bestsellers":
        return sorted(products, key=lambda p: p.num_ratings, reverse=True)
    return products


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
        if existing is None:
            best[key] = p
        else:
            p_price = p.price if p.price is not None else float("inf")
            e_price = existing.price if existing.price is not None else float("inf")
            if p_price < e_price:
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
    return result, removed


def _series_pos(p: Product) -> float:
    try:
        return float(p.series_position) if p.series_position else float("inf")
    except ValueError:
        return float("inf")


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
    return result, collapsed
