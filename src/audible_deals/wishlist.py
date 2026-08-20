"""Wishlist persistence and entry construction."""

from __future__ import annotations

import datetime
import json
import math
from dataclasses import dataclass

import click

from audible_deals import constants
from audible_deals.constants import _ASIN_RE
from audible_deals.product import Product
from audible_deals.storage import load_json_file, save_json_file


@dataclass(frozen=True)
class WishlistIssue:
    index: int
    reason: str


@dataclass(frozen=True)
class WishlistInspection:
    asin_items: list[dict]
    author_items: list[dict]
    issues: list[WishlistIssue]


def _valid_target(value: object, *, optional: bool) -> bool:
    if value is None:
        return optional
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return isinstance(value, float) and math.isfinite(value) and value >= 0


def inspect_wishlist(raw: object) -> WishlistInspection:
    """Classify valid entries and indexed semantic issues without mutation."""
    asin_items: list[dict] = []
    author_items: list[dict] = []
    issues: list[WishlistIssue] = []
    if not isinstance(raw, list):
        return WishlistInspection([], [], [WishlistIssue(0, "expected a list")])

    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            issues.append(WishlistIssue(index, "entry must be an object"))
            continue
        if entry.get("type") == "author":
            author = entry.get("author")
            if not isinstance(author, str) or not author.strip():
                issues.append(WishlistIssue(index, "author must be a non-empty string"))
                continue
            if not _valid_target(entry.get("max_price"), optional=False):
                issues.append(
                    WishlistIssue(
                        index, "author max_price must be a finite non-negative number"
                    )
                )
                continue
            author_items.append(entry)
            continue

        asin = entry.get("asin")
        if not isinstance(asin, str) or not _ASIN_RE.fullmatch(asin):
            issues.append(
                WishlistIssue(index, "asin must be 2-14 alphanumeric characters")
            )
            continue
        if not _valid_target(entry.get("max_price"), optional=True):
            issues.append(
                WishlistIssue(
                    index, "max_price must be null or a finite non-negative number"
                )
            )
            continue
        asin_items.append(entry)

    return WishlistInspection(asin_items, author_items, issues)


def warn_wishlist_issues(issues: list[WishlistIssue]) -> None:
    """Warn once per Click command when invalid wishlist entries are skipped."""
    if not issues:
        return
    ctx = click.get_current_context(silent=True)
    if ctx is not None:
        warning_key = "audible_deals_wishlist_warning"
        if ctx.meta.get(warning_key):
            return
        ctx.meta[warning_key] = True
    click.echo(
        f"Warning: skipped {len(issues)} invalid wishlist "
        f"entr{'y' if len(issues) == 1 else 'ies'}.",
        err=True,
    )


def load_wishlist() -> list[dict]:
    return load_json_file(constants.WISHLIST_FILE, list, "wishlist")


def load_wishlist_for_mutation() -> list[dict]:
    """Load a list-shaped wishlist without replacing malformed saved data."""
    try:
        contents = constants.WISHLIST_FILE.read_text()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        raise click.ClickException(f"Cannot modify wishlist: could not read it: {exc}")

    try:
        data = json.loads(contents)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Cannot modify wishlist: malformed JSON: {exc}")
    if not isinstance(data, list):
        raise click.ClickException("Cannot modify wishlist: expected a JSON list.")
    return data


def save_wishlist(items: list[dict]) -> None:
    save_json_file(constants.WISHLIST_FILE, items, "wishlist")


def partition_wishlist(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split wishlist entries into (asin_items, author_items)."""
    inspection = inspect_wishlist(items)
    warn_wishlist_issues(inspection.issues)
    return inspection.asin_items, inspection.author_items


def wishlist_entry(product: Product, max_price: float | None) -> dict:
    """Build a wishlist dict from a Product."""
    return {
        "asin": product.asin,
        "title": product.title,
        "max_price": max_price,
        "added": datetime.date.today().isoformat(),
    }
