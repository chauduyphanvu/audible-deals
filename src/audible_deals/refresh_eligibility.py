"""Recently surfaced products eligible for background refresh, in ``state.db``."""

from __future__ import annotations

import datetime
import logging
import math

from audible_deals import constants, db
from audible_deals.product import Product

logger = logging.getLogger(__name__)


def _date_string(value: object) -> str:
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


def load_refresh_eligibility() -> dict[str, dict[str, str]]:
    """``{locale: {asin: date it was last surfaced}}``."""
    entries: dict[str, dict[str, str]] = {}
    for locale, asin, surfaced_on in db.state().execute(
        "SELECT locale, asin, surfaced_on FROM refresh_eligibility ORDER BY locale, asin"
    ):
        entries.setdefault(locale, {})[asin] = surfaced_on
    return entries


def mark_refresh_eligible(
    products: list[Product], observed_on: datetime.date | str | None = None
) -> None:
    """Record that *products* were surfaced today (a later date never moves back)."""
    observed = _date_string(observed_on or datetime.date.today())
    surfaced = [
        (product.locale, product.asin, observed)
        for product in products
        if _numeric_price(product.price)
        and product.locale in constants.LOCALE_DOMAIN
        and constants._ASIN_RE.fullmatch(product.asin)
    ]
    if not surfaced:
        return
    with db.transaction(db.state()) as conn:
        conn.executemany(
            "INSERT INTO refresh_eligibility (locale, asin, surfaced_on) VALUES (?, ?, ?) "
            "ON CONFLICT (locale, asin) DO UPDATE SET "
            "surfaced_on = MAX(surfaced_on, excluded.surfaced_on)",
            surfaced,
        )
