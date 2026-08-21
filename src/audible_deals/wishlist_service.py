"""Click-free wishlist mutation workflows."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from audible_deals.product import Product
from audible_deals.wishlist import (
    WishlistIssue,
    WishlistMutationError,
    create_wishlist_backup,
    inspect_wishlist,
    load_wishlist_for_mutation,
    load_wishlist_for_repair,
    save_wishlist,
    wishlist_entry,
    wishlist_lock,
)


@dataclass(frozen=True)
class WishlistAddPlan:
    pending_asins: tuple[str, ...]
    already_present: tuple[str, ...]
    issues: tuple[WishlistIssue, ...]
    valid_total: int


@dataclass(frozen=True)
class WishlistAddEvent:
    action: Literal["added", "raced"]
    product: Product


@dataclass(frozen=True)
class WishlistAddResult:
    added_products: tuple[Product, ...]
    raced_asins: tuple[str, ...]
    issues: tuple[WishlistIssue, ...]
    valid_total: int
    events: tuple[WishlistAddEvent, ...] = ()


@dataclass(frozen=True)
class AuthorWatchResult:
    author: str
    added: bool
    issues: tuple[WishlistIssue, ...]


@dataclass(frozen=True)
class WishlistRemoveResult:
    removed: int
    remaining: int
    issues: tuple[WishlistIssue, ...]


@dataclass(frozen=True)
class WishlistTargetChange:
    asin: str
    title: str
    max_price: float | None


@dataclass(frozen=True)
class WishlistTargetEvent:
    asin: str
    change: WishlistTargetChange | None


@dataclass(frozen=True)
class WishlistTargetUpdateResult:
    events: tuple[WishlistTargetEvent, ...]
    changes: tuple[WishlistTargetChange, ...]
    not_found_asins: tuple[str, ...]
    issues: tuple[WishlistIssue, ...]


@dataclass(frozen=True)
class WishlistSyncChange:
    action: Literal["added", "updated"]
    product: Product


@dataclass(frozen=True)
class WishlistSyncResult:
    changes: tuple[WishlistSyncChange, ...]
    added: int
    updated: int
    skipped: int
    valid_total: int
    issues: tuple[WishlistIssue, ...]


@dataclass(frozen=True)
class WishlistRepairPlan:
    issues: tuple[WishlistIssue, ...]
    original_contents: bytes | None


@dataclass(frozen=True)
class WishlistRepairResult:
    removed: int
    backup: Path


@dataclass(frozen=True)
class WishlistPurgePlan:
    asin_items: tuple[dict, ...]
    issues: tuple[WishlistIssue, ...]

    def owned_items(self, owned_asins: set[str]) -> tuple[dict, ...]:
        return tuple(item for item in self.asin_items if item["asin"] in owned_asins)


@dataclass(frozen=True)
class WishlistPurgeResult:
    removed: int
    remaining: int


class WishlistSourceChangedError(WishlistMutationError):
    """The wishlist no longer matches a previously inspected source."""


def _valid_total(items: list[dict]) -> int:
    inspection = inspect_wishlist(items)
    return len(inspection.asin_items) + len(inspection.author_items)


def plan_product_add(asins: Iterable[str]) -> WishlistAddPlan:
    items = load_wishlist_for_mutation()
    inspection = inspect_wishlist(items)

    existing = {item["asin"] for item in inspection.asin_items}
    pending: list[str] = []
    pending_set: set[str] = set()
    already_present: list[str] = []
    for asin in asins:
        if asin in existing:
            already_present.append(asin)
        elif asin not in pending_set:
            pending.append(asin)
            pending_set.add(asin)
    return WishlistAddPlan(
        pending_asins=tuple(pending),
        already_present=tuple(already_present),
        issues=tuple(inspection.issues),
        valid_total=len(inspection.asin_items) + len(inspection.author_items),
    )


def add_products(
    products: Iterable[Product],
    max_price: float | None,
    *,
    locale: str | None = None,
) -> WishlistAddResult:
    products = tuple(products)
    with wishlist_lock():
        items = load_wishlist_for_mutation()
        inspection = inspect_wishlist(items)
        existing = {item["asin"] for item in inspection.asin_items}
        added_products: list[Product] = []
        raced_asins: list[str] = []
        events: list[WishlistAddEvent] = []
        for product in products:
            if product.asin in existing:
                raced_asins.append(product.asin)
                events.append(WishlistAddEvent("raced", product))
                continue
            items.append(wishlist_entry(product, max_price, locale=locale))
            existing.add(product.asin)
            added_products.append(product)
            events.append(WishlistAddEvent("added", product))
        if added_products:
            save_wishlist(items)
        valid_total = (
            len(inspection.asin_items)
            + len(inspection.author_items)
            + len(added_products)
        )
    return WishlistAddResult(
        added_products=tuple(added_products),
        raced_asins=tuple(raced_asins),
        issues=tuple(inspection.issues),
        valid_total=valid_total,
        events=tuple(events),
    )


def add_author_watch(author: str, max_price: float) -> AuthorWatchResult:
    with wishlist_lock():
        items = load_wishlist_for_mutation()
        inspection = inspect_wishlist(items)
        author_lower = author.lower()
        if any(
            item.get("author", "").lower() == author_lower
            for item in inspection.author_items
        ):
            return AuthorWatchResult(author, False, tuple(inspection.issues))
        items.append(
            {
                "type": "author",
                "author": author,
                "max_price": max_price,
                "added": datetime.date.today().isoformat(),
            }
        )
        save_wishlist(items)
    return AuthorWatchResult(author, True, tuple(inspection.issues))


def remove_entries(
    asins: Iterable[str] = (), *, author: str | None = None
) -> WishlistRemoveResult:
    remove_set = set(asins)
    with wishlist_lock():
        items = load_wishlist_for_mutation()
        inspection = inspect_wishlist(items)
        valid_entries = inspection.asin_items + inspection.author_items
        valid_ids = {id(item) for item in valid_entries}
        before = len(valid_entries)
        if remove_set:
            items = [
                item
                for item in items
                if not (id(item) in valid_ids and item.get("asin") in remove_set)
            ]
        if author:
            author_lower = author.lower()
            items = [
                item
                for item in items
                if not (
                    id(item) in valid_ids
                    and item.get("type") == "author"
                    and item.get("author", "").lower() == author_lower
                )
            ]
        save_wishlist(items)
        remaining = _valid_total(items)
    return WishlistRemoveResult(
        removed=before - remaining,
        remaining=remaining,
        issues=tuple(inspection.issues),
    )


def update_targets(
    asins: Iterable[str], max_price: float | None
) -> WishlistTargetUpdateResult:
    asins = tuple(asins)
    with wishlist_lock():
        items = load_wishlist_for_mutation()
        inspection = inspect_wishlist(items)
        by_asin = {item["asin"]: item for item in inspection.asin_items}
        changes: list[WishlistTargetChange] = []
        events: list[WishlistTargetEvent] = []
        not_found: list[str] = []
        for asin in asins:
            entry = by_asin.get(asin)
            if entry is None:
                not_found.append(asin)
                events.append(WishlistTargetEvent(asin, None))
                continue
            entry["max_price"] = max_price
            change = WishlistTargetChange(
                asin=asin,
                title=entry.get("title", ""),
                max_price=max_price,
            )
            changes.append(change)
            events.append(WishlistTargetEvent(asin, change))
        save_wishlist(items)
    return WishlistTargetUpdateResult(
        events=tuple(events),
        changes=tuple(changes),
        not_found_asins=tuple(not_found),
        issues=tuple(inspection.issues),
    )


def sync_products(
    products: Iterable[Product], max_price: float | None, *, update: bool = False
) -> WishlistSyncResult:
    products = tuple(products)
    with wishlist_lock():
        items = load_wishlist_for_mutation()
        inspection = inspect_wishlist(items)
        by_asin = {item["asin"]: item for item in inspection.asin_items}
        changes: list[WishlistSyncChange] = []
        added = 0
        updated = 0
        skipped = 0
        for product in products:
            if product.asin in by_asin:
                if update:
                    by_asin[product.asin]["max_price"] = max_price
                    updated += 1
                    changes.append(WishlistSyncChange("updated", product))
                else:
                    skipped += 1
                continue
            entry = wishlist_entry(product, max_price)
            items.append(entry)
            by_asin[product.asin] = entry
            added += 1
            changes.append(WishlistSyncChange("added", product))
        save_wishlist(items)
        valid_total = len(inspection.asin_items) + len(inspection.author_items) + added
    return WishlistSyncResult(
        changes=tuple(changes),
        added=added,
        updated=updated,
        skipped=skipped,
        valid_total=valid_total,
        issues=tuple(inspection.issues),
    )


def plan_repair() -> WishlistRepairPlan:
    items, original_contents = load_wishlist_for_repair()
    inspection = inspect_wishlist(items)
    return WishlistRepairPlan(tuple(inspection.issues), original_contents)


def repair_wishlist(plan: WishlistRepairPlan) -> WishlistRepairResult:
    if plan.original_contents is None:
        raise WishlistMutationError("Cannot repair wishlist: source file is missing.")
    with wishlist_lock():
        items, current_contents = load_wishlist_for_repair()
        if current_contents is None or current_contents != plan.original_contents:
            raise WishlistSourceChangedError(
                "Wishlist changed while awaiting confirmation; rerun repair."
            )
        inspection = inspect_wishlist(items)
        invalid_indexes = {issue.index for issue in inspection.issues}
        repaired = [
            entry for index, entry in enumerate(items) if index not in invalid_indexes
        ]
        backup = create_wishlist_backup(current_contents)
        save_wishlist(repaired, durable=True)
    return WishlistRepairResult(len(inspection.issues), backup)


def plan_owned_purge() -> WishlistPurgePlan:
    items = load_wishlist_for_mutation()
    inspection = inspect_wishlist(items)
    return WishlistPurgePlan(
        asin_items=tuple(inspection.asin_items),
        issues=tuple(inspection.issues),
    )


def purge_confirmed_asins(asins: Iterable[str]) -> WishlistPurgeResult:
    confirmed_asins = set(asins)
    with wishlist_lock():
        items = load_wishlist_for_mutation()
        inspection = inspect_wishlist(items)
        to_remove = [
            item for item in inspection.asin_items if item["asin"] in confirmed_asins
        ]
        remove_ids = {id(item) for item in to_remove}
        kept = [item for item in items if id(item) not in remove_ids]
        save_wishlist(kept)
        removed = len(to_remove)
        remaining = len(inspection.asin_items) + len(inspection.author_items) - removed
    return WishlistPurgeResult(removed, remaining)
