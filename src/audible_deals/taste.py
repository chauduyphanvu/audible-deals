"""Taste profile built locally from the user's Audible library.

Pure profile construction and fit scoring; persistence goes through
storage.py into TASTE_CACHE_FILE. No API calls happen here.
"""

from __future__ import annotations

import collections
import datetime
import logging

from audible_deals import constants
from audible_deals.metrics import value_score
from audible_deals.product import Product
from audible_deals.storage import load_json_file, save_json_file

logger = logging.getLogger(__name__)

PROFILE_MAX_AGE_SECONDS = 86400  # rebuild after a day; library changes slowly
TOP_AUTHORS = 5
TOP_NARRATORS = 5
TOP_GENRES = 3
TOP_SERIES = 5
MIN_SERIES_OWNED = 2

_FIT_SERIES = 5.0
_FIT_AUTHOR = 3.0
_FIT_NARRATOR = 2.0
_FIT_GENRE = 1.0


def _top_counts(counter: collections.Counter[str], top_n: int) -> list[dict]:
    """Top entries seen at least twice; fall back to the top 3 one-offs."""
    repeated = [(name, c) for name, c in counter.most_common() if c >= 2]
    picked = repeated[:top_n] or counter.most_common(3)
    return [{"name": name, "count": count} for name, count in picked]


def build_profile(lib_products: list[Product]) -> dict:
    """Derive top authors/narrators/genres and in-progress series from a library."""
    author_counts: collections.Counter[str] = collections.Counter()
    narrator_counts: collections.Counter[str] = collections.Counter()
    genre_counts: collections.Counter[str] = collections.Counter()
    genre_names: dict[str, str] = {}
    series_map: dict[str, list[Product]] = {}

    for p in lib_products:
        for a in p.authors:
            author_counts[a] += 1
        for n in p.narrators:
            narrator_counts[n] += 1
        for cid, cname in zip(p.category_ids, p.categories):
            genre_counts[cid] += 1
            genre_names[cid] = cname
        if p.series_name:
            series_map.setdefault(p.series_name, []).append(p)

    series = sorted(
        (
            {
                "name": name,
                "owned": len(books),
                "series_asin": next(
                    (b.series_asin for b in books if b.series_asin), ""
                ),
            }
            for name, books in series_map.items()
            if len(books) >= MIN_SERIES_OWNED
        ),
        key=lambda s: -s["owned"],
    )[:TOP_SERIES]

    profile = {
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "library_size": len(lib_products),
        "owned_asins": [p.asin for p in lib_products],
        "authors": _top_counts(author_counts, TOP_AUTHORS),
        "narrators": _top_counts(narrator_counts, TOP_NARRATORS),
        "genres": [
            {"id": cid, "name": genre_names.get(cid, ""), "count": count}
            for cid, count in genre_counts.most_common(TOP_GENRES)
        ],
        "series": series,
    }
    logger.debug(
        "built taste profile: %d books, %d authors, %d series",
        len(lib_products),
        len(profile["authors"]),
        len(series),
    )
    return profile


def load_cached_profile(
    max_age_s: int = PROFILE_MAX_AGE_SECONDS,
) -> dict | None:
    """Return the cached profile, or None when missing/stale/corrupt."""
    data = load_json_file(constants.TASTE_CACHE_FILE, dict, "taste profile")
    if not data:
        return None
    try:
        built = datetime.datetime.fromisoformat(data["built_at"])
    except (KeyError, ValueError, TypeError):
        return None
    if (datetime.datetime.now() - built).total_seconds() > max_age_s:
        logger.debug("taste profile stale (built %s)", data.get("built_at"))
        return None
    return data


def save_profile(profile: dict) -> None:
    save_json_file(constants.TASTE_CACHE_FILE, profile, "taste profile")


def fit_score(
    p: Product, profile: dict, series_of: dict[str, str]
) -> tuple[float, list[str]]:
    """Score how well a candidate matches the profile. Returns (points, reasons)."""
    points = 0.0
    reasons: list[str] = []

    series_name = series_of.get(p.asin)
    if series_name:
        points += _FIT_SERIES
        reasons.append(f"next in {series_name}")

    profile_authors = {a["name"] for a in profile.get("authors", [])}
    matched_author = next((a for a in p.authors if a in profile_authors), None)
    if matched_author:
        points += _FIT_AUTHOR
        reasons.append(f"author: {matched_author}")

    profile_narrators = {n["name"] for n in profile.get("narrators", [])}
    matched_narrator = next((n for n in p.narrators if n in profile_narrators), None)
    if matched_narrator:
        points += _FIT_NARRATOR
        reasons.append(f"narrator: {matched_narrator}")

    profile_genres = {g["id"] for g in profile.get("genres", [])}
    if any(cid in profile_genres for cid in p.category_ids):
        points += _FIT_GENRE
        if not reasons:
            reasons.append("favorite genre")

    return points, reasons


def rank_by_fit(
    products: list[Product], profile: dict, series_of: dict[str, str]
) -> tuple[list[Product], dict[str, str]]:
    """Rank by (fit, value); drop zero-fit items. Returns (ranked, asin -> reason)."""
    scored: list[tuple[float, float, Product, str]] = []
    for p in products:
        points, reasons = fit_score(p, profile, series_of)
        if points <= 0:
            continue
        scored.append((points, value_score(p), p, ", ".join(reasons)))
    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [p for _, _, p, _ in scored], {p.asin: why for _, _, p, why in scored}
