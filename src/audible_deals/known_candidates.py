"""Known candidates: titles seen before that a scan did not return.

A scan sees a few hundred of a category's titles, ranked by how Audible sorts
them. A title that was cheap last week but sits beyond the last scanned page
today is invisible to it. The product index (the ``products`` table, filled
whenever a title is shown to you) remembers such titles; after a scan, the
ones worth another look are re-priced live in at most two batch calls and go
through the same filters as everything else.

Selection runs on last-known data, so it can only judge what does not change
much (language, length, authors, rating...). Price and discount are left to
the live re-price.
"""

from __future__ import annotations

import datetime
import json
import logging
import math
from collections.abc import Collection, Set

from audible_deals import db
from audible_deals.constants import MAX_PAGE_SIZE
from audible_deals.filtering import filter_products
from audible_deals.metrics import price_per_hour
from audible_deals.product import Product
from audible_deals.result_models import FilterContext
from audible_deals.scan_cache import unpack_products

logger = logging.getLogger(__name__)

MAX_KNOWN_CANDIDATES = 100
MAX_AGE_DAYS = 90
#: Rows read from the index before the filters run (cheapest first).
_READ_LIMIT = 5000


def calls_for(count: int) -> int:
    """Batch calls needed to re-price *count* titles."""
    return math.ceil(count / MAX_PAGE_SIZE)


def _cutoff(today: datetime.date | None) -> str:
    day = today or datetime.date.today()
    return (day - datetime.timedelta(days=MAX_AGE_DAYS)).isoformat()


def _scope(category_ids: Collection[str]) -> tuple[str, list[str]]:
    """SQL restricting to products in any of *category_ids* ("" means no restriction)."""
    wanted = sorted({cid for cid in category_ids if cid})
    if not wanted or "" in category_ids:
        return "", []
    marks = ",".join("?" * len(wanted))
    return (
        "AND EXISTS (SELECT 1 FROM product_categories c WHERE c.locale = p.locale "
        f"AND c.asin = p.asin AND c.category_id IN ({marks}))",  # noqa: S608
        wanted,
    )


def indexed_count(
    locale: str,
    category_ids: Collection[str] = ("",),
    *,
    today: datetime.date | None = None,
) -> int:
    """How many indexed titles could be candidates (before filters), capped at the maximum.

    This is the ceiling used to plan the extra calls before a scan has run.
    """
    scope_sql, scope_args = _scope(category_ids)
    row = (
        db.state()
        .execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM products p "  # noqa: S608
            "JOIN product_index i ON i.locale = p.locale AND i.asin = p.asin "
            "WHERE p.locale = ? AND p.last_price IS NOT NULL "
            f"AND p.last_seen >= ? {scope_sql} LIMIT ?)",
            (locale, _cutoff(today), *scope_args, MAX_KNOWN_CANDIDATES),
        )
        .fetchone()
    )
    return int(row[0])


def select_known(
    locale: str,
    *,
    category_ids: Collection[str],
    exclude: Set[str],
    context: FilterContext,
    limit: int = MAX_KNOWN_CANDIDATES,
    today: datetime.date | None = None,
) -> list[str]:
    """ASINs worth re-pricing, best first.

    A candidate is in *locale*'s index, in one of the scanned categories
    (any, when *category_ids* holds ""), seen within the last 90 days, not in
    *exclude* (what the scan returned) and accepted by *context* judged on its
    last-known data. They are ranked by last-known price, then price per hour.
    """
    scope_sql, scope_args = _scope(category_ids)
    rows = (
        db.state()
        .execute(
            "SELECT i.data FROM products p "  # noqa: S608
            "JOIN product_index i ON i.locale = p.locale AND i.asin = p.asin "
            "WHERE p.locale = ? AND p.last_price IS NOT NULL "
            f"AND p.last_seen >= ? {scope_sql} "
            "ORDER BY p.last_price, p.asin LIMIT ?",
            (locale, _cutoff(today), *scope_args, _READ_LIMIT),
        )
        .fetchall()
    )
    candidates: list[Product] = []
    for (data,) in rows:
        try:
            [product] = unpack_products([json.loads(data)])
        except (ValueError, TypeError):
            logger.debug("skipping an unreadable indexed product", exc_info=True)
            continue
        if product.asin not in exclude and product.locale == locale:
            candidates.append(product)
    accepted = filter_products(candidates, context).products
    ranked = sorted(
        accepted,
        key=lambda p: (
            p.price if p.price is not None else math.inf,
            price_per_hour(p),
            p.asin,
        ),
    )
    return [product.asin for product in ranked[:limit]]
