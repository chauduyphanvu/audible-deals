"""What the results browsers do to your data, apart from how they look.

The prompt loop (``cli/interactive.py``) and the full-screen browser
(``audible_deals/tui``) are two views over the same actions: re-sort the saved
result session, dismiss titles, add titles to the wishlist, read price history.
They live here so neither view carries its own copy of the logic.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Collection, Iterable
from dataclasses import dataclass

from audible_deals.filtering import _SORT_KEYS, sort_local
from audible_deals.price_history import load_price_history
from audible_deals.product import Product
from audible_deals.results_cache import save_dismissed_asins, update_session_view
from audible_deals.wishlist import WishlistIssue
from audible_deals.wishlist_service import (
    WishlistAddResult,
    add_products,
    plan_product_add,
)

logger = logging.getLogger(__name__)

#: Every sort the browsers offer, in the order the full-screen ``s`` key cycles
#: through them (``release-date`` is an alias of ``date``, ``asin`` is no use to a
#: reader).
SORT_CYCLE = (
    "price",
    "-price",
    "discount",
    "rating",
    "price-per-hour",
    "value",
    "length",
    "date",
    "bestsellers",
    "title",
    "author",
)

VALID_SORT_KEYS = sorted(_SORT_KEYS)

NO_TARGET_INVALID = "Invalid price, no target set"
NO_TARGET_NOT_POSITIVE = "Target must be a finite number greater than 0, no target set"


def next_sort_key(current: str | None) -> str:
    """The sort after *current* in :data:`SORT_CYCLE` (the first, if unknown)."""
    if current not in SORT_CYCLE:
        return SORT_CYCLE[0]
    return SORT_CYCLE[(SORT_CYCLE.index(current) + 1) % len(SORT_CYCLE)]


def persist_view(products: Iterable[Product], *, sort: str | None = None) -> bool:
    """Save the order shown to the user in the result session, so ``@N`` matches it.

    Returns ``False`` (and does not raise) when the session cannot be updated: the
    browser keeps working, only ``@N`` would lag behind.
    """
    try:
        update_session_view([product.asin for product in products], sort=sort)
    except Exception:
        logger.debug("could not update the saved result view", exc_info=True)
        return False
    return True


def resort(products: list[Product], sort_key: str) -> list[Product]:
    """*products* sorted by *sort_key*, remembered in the saved result session."""
    ordered = sort_local(products, sort_key)
    persist_view(ordered, sort=sort_key)
    return ordered


def dismiss(products: list[Product], asins: Collection[str]) -> list[Product]:
    """Globally dismiss *asins* and return *products* without them.

    The dismissed titles are skipped by every later ``find``/``search`` and
    ``track run``. Raises when the dismissal cannot be saved (nothing is removed).
    """
    save_dismissed_asins(set(asins))
    remaining = [product for product in products if product.asin not in asins]
    persist_view(remaining)
    return remaining


@dataclass(frozen=True)
class WishlistSelection:
    """Which of the chosen titles still need adding to the wishlist."""

    pending: tuple[Product, ...]
    #: Chosen titles that are already on it (a title chosen twice counts once as
    #: pending and then as already present).
    already: tuple[Product, ...]
    issues: tuple[WishlistIssue, ...]


def select_for_wishlist(selected: Iterable[Product]) -> WishlistSelection:
    """Split *selected* into titles to add and titles the wishlist already has."""
    chosen = list(selected)
    plan = plan_product_add(product.asin for product in chosen)
    stored = set(plan.already_present)
    pending_asins = set(plan.pending_asins)
    seen: set[str] = set()
    pending: list[Product] = []
    already: list[Product] = []
    for product in chosen:
        if product.asin in stored or product.asin in seen:
            already.append(product)
        elif product.asin in pending_asins:
            seen.add(product.asin)
            pending.append(product)
    return WishlistSelection(tuple(pending), tuple(already), plan.issues)


def add_to_wishlist(
    pending: Iterable[Product], target_price: float | None
) -> WishlistAddResult:
    """Add *pending* titles, each with the same target price (or none)."""
    return add_products(list(pending), target_price)


@dataclass(frozen=True)
class TargetPrice:
    """A parsed target-price answer: a price, nothing, or why it was rejected."""

    price: float | None = None
    problem: str | None = None


def parse_target_price(raw: str) -> TargetPrice:
    """Parse what the user typed as a wishlist target (empty means no target)."""
    text = raw.strip()
    if not text:
        return TargetPrice()
    try:
        parsed = float(text)
    except ValueError:
        return TargetPrice(problem=NO_TARGET_INVALID)
    if math.isfinite(parsed) and parsed > 0:
        return TargetPrice(price=parsed)
    return TargetPrice(problem=NO_TARGET_NOT_POSITIVE)


def history_entries(product: Product) -> list[dict]:
    """The recorded prices of *product* in its marketplace, oldest first."""
    return load_price_history(product.asin, product.locale)
