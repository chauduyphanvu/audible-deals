"""Persistence for recently surfaced products eligible for background refresh."""

from __future__ import annotations

import datetime
import json
import logging
import math
from pathlib import Path

from audible_deals import constants
from audible_deals.locking import advisory_lock
from audible_deals.product import Product
from audible_deals.results_cache import load_seen_asins
from audible_deals.storage import _atomic_write

logger = logging.getLogger(__name__)

_VERSION = 1


def _lock_file() -> Path:
    path = constants.REFRESH_ELIGIBILITY_FILE
    return path.with_name(f".{path.name}.lock")


def _date_string(value: datetime.date | str) -> str:
    if isinstance(value, datetime.datetime):
        value = value.date()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, str):
        return datetime.date.fromisoformat(value).isoformat()
    raise TypeError("observation date must be a date or ISO date string")


def _numeric_price(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _decode_store(raw: object) -> dict[str, dict[str, str]]:
    if not isinstance(raw, dict) or raw.get("version") != _VERSION:
        raise ValueError("unsupported version or invalid root")
    marketplaces = raw.get("marketplaces")
    if not isinstance(marketplaces, dict):
        raise ValueError("marketplaces must be an object")
    decoded: dict[str, dict[str, str]] = {}
    for locale, entries in marketplaces.items():
        if locale not in constants.LOCALE_DOMAIN or not isinstance(entries, dict):
            raise ValueError("invalid marketplace entry")
        decoded[locale] = {}
        for asin, observed_on in entries.items():
            if not isinstance(asin, str) or not constants._ASIN_RE.fullmatch(asin):
                raise ValueError("invalid ASIN entry")
            if not isinstance(observed_on, str):
                raise ValueError("invalid observation date")
            decoded[locale][asin] = _date_string(observed_on)
    return decoded


def _wire(entries: dict[str, dict[str, str]]) -> dict:
    return {
        "version": _VERSION,
        "marketplaces": {
            locale: dict(sorted(values.items()))
            for locale, values in sorted(entries.items())
            if values
        },
    }


def _migrate_seen_histories() -> dict[str, dict[str, str]]:
    seen = load_seen_asins()
    migrated: dict[str, dict[str, str]] = {}
    if not seen or not constants.HISTORY_DIR.exists():
        return migrated
    for history_file in sorted(constants.HISTORY_DIR.glob("*.json")):
        asin = history_file.stem
        if asin not in seen or not constants._ASIN_RE.fullmatch(asin):
            continue
        try:
            raw = json.loads(history_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        marketplaces = raw.get("marketplaces") if isinstance(raw, dict) else None
        if not isinstance(marketplaces, dict):
            continue
        for locale, history in marketplaces.items():
            if locale not in constants.LOCALE_DOMAIN or not isinstance(history, list):
                continue
            valid_dates = []
            for entry in history:
                if not isinstance(entry, dict) or not _numeric_price(
                    entry.get("price")
                ):
                    continue
                try:
                    valid_dates.append(_date_string(entry.get("date")))
                except (TypeError, ValueError):
                    continue
            if valid_dates:
                migrated.setdefault(locale, {})[asin] = max(valid_dates)
    return migrated


def _load_locked() -> tuple[dict[str, dict[str, str]], bool]:
    path = constants.REFRESH_ELIGIBILITY_FILE
    if path.exists():
        try:
            return _decode_store(json.loads(path.read_text())), True
        except (json.JSONDecodeError, OSError, ValueError):
            logger.warning(
                "refresh eligibility at %s is corrupt or unsupported, ignoring",
                path,
                exc_info=True,
            )
            return {}, False
    entries = _migrate_seen_histories()
    _atomic_write(path, json.dumps(_wire(entries), indent=2, allow_nan=False))
    return entries, True


def load_refresh_eligibility() -> dict[str, dict[str, str]]:
    with advisory_lock(_lock_file(), wait=True):
        entries, _writable = _load_locked()
    return entries


def mark_refresh_eligible(
    products: list[Product], observed_on: datetime.date | str | None = None
) -> None:
    observed = _date_string(observed_on or datetime.date.today())
    surfaced = [
        product
        for product in products
        if _numeric_price(product.price)
        and product.locale in constants.LOCALE_DOMAIN
        and constants._ASIN_RE.fullmatch(product.asin)
    ]
    if not surfaced:
        return
    with advisory_lock(_lock_file(), wait=True):
        entries, writable = _load_locked()
        if not writable:
            return
        changed = False
        for product in surfaced:
            marketplace = entries.setdefault(product.locale, {})
            if observed > marketplace.get(product.asin, ""):
                marketplace[product.asin] = observed
                changed = True
        if changed:
            _atomic_write(
                constants.REFRESH_ELIGIBILITY_FILE,
                json.dumps(_wire(entries), indent=2, allow_nan=False),
            )
