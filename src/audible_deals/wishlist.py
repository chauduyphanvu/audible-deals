"""Wishlist persistence and entry construction."""

from __future__ import annotations

import contextlib
import datetime
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import click

from audible_deals import constants
from audible_deals.constants import LOCALE_DOMAIN, _ASIN_RE
from audible_deals.locking import advisory_lock
from audible_deals.product import Product
from audible_deals.storage import _fsync_parent, load_json_file, save_json_file


@dataclass(frozen=True)
class WishlistIssue:
    index: int
    reason: str


@dataclass(frozen=True)
class WishlistInspection:
    asin_items: list[dict]
    author_items: list[dict]
    issues: list[WishlistIssue]


class WishlistMutationError(ValueError):
    """A saved wishlist cannot be safely changed."""


def _valid_target(value: object, *, optional: bool) -> bool:
    if value is None:
        return optional
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return isinstance(value, float) and math.isfinite(value) and value >= 0


def _reject_json_constant(value: str):
    raise ValueError(f"invalid JSON constant: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"JSON number is not finite: {value}")
    return parsed


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
        locale = entry.get("locale")
        if locale is not None and (
            not isinstance(locale, str) or locale not in LOCALE_DOMAIN
        ):
            issues.append(
                WishlistIssue(index, "locale must be a supported marketplace")
            )
            continue
        asin_items.append(entry)

    return WishlistInspection(asin_items, author_items, issues)


def warn_wishlist_issues(issues: Sequence[WishlistIssue]) -> None:
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
        f"entr{'y' if len(issues) == 1 else 'ies'}. "
        "Run 'deals wishlist repair --dry-run' to inspect them.",
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
        raise WishlistMutationError(f"Cannot modify wishlist: could not read it: {exc}")

    try:
        data = json.loads(contents)
    except json.JSONDecodeError as exc:
        raise WishlistMutationError(f"Cannot modify wishlist: malformed JSON: {exc}")
    if not isinstance(data, list):
        raise WishlistMutationError("Cannot modify wishlist: expected a JSON list.")
    return data


def load_wishlist_for_repair() -> tuple[list[dict], bytes | None]:
    """Load the exact saved bytes and require a list-shaped JSON document."""
    try:
        contents = constants.WISHLIST_FILE.read_bytes()
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        raise WishlistMutationError(f"Cannot repair wishlist: could not read it: {exc}")

    try:
        data = json.loads(
            contents,
            parse_constant=_reject_json_constant,
            parse_float=_parse_finite_float,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise WishlistMutationError(f"Cannot repair wishlist: malformed JSON: {exc}")
    if not isinstance(data, list):
        raise WishlistMutationError("Cannot repair wishlist: expected a JSON list.")
    return data, contents


@contextlib.contextmanager
def wishlist_lock():
    """Serialize wishlist read-modify-write operations across processes."""
    lock_file = constants.WISHLIST_FILE.with_name(
        f".{constants.WISHLIST_FILE.name}.lock"
    )
    with advisory_lock(lock_file, wait=True):
        yield


def create_wishlist_backup(contents: bytes) -> Path:
    """Create an owner-only, collision-safe backup beside the wishlist."""
    source = constants.WISHLIST_FILE
    suffix = 0
    while True:
        ending = ".bak" if suffix == 0 else f".bak.{suffix}"
        candidate = source.with_name(source.name + ending)
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            suffix += 1
            continue
        try:
            with os.fdopen(fd, "wb") as backup:
                backup.write(contents)
                backup.flush()
                os.fsync(backup.fileno())
            os.chmod(candidate, 0o600)
            _fsync_parent(candidate)
        except BaseException:
            candidate.unlink(missing_ok=True)
            raise
        return candidate


def save_wishlist(items: list[dict], *, durable: bool = False) -> None:
    save_json_file(constants.WISHLIST_FILE, items, "wishlist", durable=durable)


def partition_wishlist(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split wishlist entries into (asin_items, author_items)."""
    inspection = inspect_wishlist(items)
    warn_wishlist_issues(inspection.issues)
    return inspection.asin_items, inspection.author_items


def wishlist_entry(
    product: Product, max_price: float | None, *, locale: str | None = None
) -> dict:
    """Build a wishlist dict from a Product."""
    return {
        "asin": product.asin,
        "title": product.title,
        "max_price": max_price,
        "added": datetime.date.today().isoformat(),
        "locale": locale or product.locale,
    }
