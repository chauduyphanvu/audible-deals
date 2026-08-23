"""Audiobook product data model and catalog API response parsing."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from audible_deals.constants import LOCALE_CURRENCY, product_url

logger = logging.getLogger(__name__)


@dataclass
class Product:
    """Audiobook product from Audible catalog."""

    asin: str
    title: str
    subtitle: str = ""
    authors: list[str] = field(default_factory=list)
    narrators: list[str] = field(default_factory=list)
    publisher: str = ""
    price: float | None = None
    list_price: float | None = None
    length_minutes: int = 0
    rating: float = 0.0
    num_ratings: int = 0
    categories: list[str] = field(default_factory=list)
    category_ids: list[str] = field(default_factory=list)
    series_name: str = ""
    series_position: str = ""
    series_asin: str = ""
    language: str = ""
    release_date: str = ""
    in_plus_catalog: bool = False
    locale: str = "us"

    @property
    def full_title(self) -> str:
        if self.subtitle:
            return f"{self.title}: {self.subtitle}"
        return self.title

    @property
    def hours(self) -> float:
        return round(self.length_minutes / 60, 1) if self.length_minutes else 0.0

    @property
    def discount_pct(self) -> int | None:
        if self.price is not None and self.list_price and self.list_price > 0:
            return round((1 - self.price / self.list_price) * 100)
        return None

    @property
    def authors_str(self) -> str:
        return ", ".join(self.authors[:3])

    @property
    def narrators_str(self) -> str:
        return ", ".join(self.narrators[:2])

    @property
    def currency(self) -> str:
        return LOCALE_CURRENCY.get(self.locale, "$")

    @property
    def url(self) -> str:
        return product_url(self.asin, self.locale)


def _finite_float(
    value: object,
    default: float | None = None,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if isinstance(value, bool) or value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(result):
        return default
    if minimum is not None and result < minimum:
        return default
    if maximum is not None and result > maximum:
        return default
    return result


def _finite_int(value: object, default: int = 0) -> int:
    number = _finite_float(value, minimum=0)
    if number is None:
        return default
    try:
        return int(number)
    except (OverflowError, ValueError):
        return default


def _dict_items(value: object):
    if not isinstance(value, list):
        return
    yield from (item for item in value if isinstance(item, dict))


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _extract_rating(raw: dict) -> tuple[float, int]:
    """Extract (rating, num_ratings) from the nested overall_distribution."""
    rating_data = raw.get("rating") or {}
    rating = 0.0
    num_ratings = 0
    if isinstance(rating_data, dict):
        dist = rating_data.get("overall_distribution") or {}
        if not isinstance(dist, dict):
            return rating, num_ratings
        parsed_rating = _finite_float(
            dist.get("display_average_rating"), 0.0, minimum=0, maximum=5
        )
        if parsed_rating is None:
            logger.debug(
                "parse_product %s: bad display_average_rating", raw.get("asin")
            )
        else:
            rating = parsed_rating
        parsed_count = _finite_int(dist.get("num_ratings"), 0)
        if parsed_count == 0 and dist.get("num_ratings") not in (None, 0, 0.0, "", "0"):
            logger.debug("parse_product %s: bad num_ratings", raw.get("asin"))
        num_ratings = parsed_count
    return rating, num_ratings


def _extract_categories(raw: dict) -> tuple[list[str], list[str]]:
    """Extract (category names, category ids) by flattening the ladder structure.

    Pairs are deduped on id in one pass so categories[i] always corresponds to
    category_ids[i]; build_profile relies on that positional alignment.
    """
    by_id: dict[str, str] = {}
    for ladder in _dict_items(raw.get("category_ladders")):
        for cat in _dict_items(ladder.get("ladder")):
            cid = _text(cat.get("id"))
            name = _text(cat.get("name"))
            if cid and name and cid not in by_id:
                by_id[cid] = name
    return list(by_id.values()), list(by_id.keys())


def _extract_series(raw: dict) -> tuple[str, str, str]:
    """Extract (series_name, series_position, series_asin) from the first series entry."""
    series_list = list(_dict_items(raw.get("series")))
    if series_list:
        s = series_list[0]
        return _text(s.get("title")), _text(s.get("sequence")), _text(s.get("asin"))
    return "", "", ""


def _extract_plus(raw: dict) -> bool:
    """Detect Audible Plus / AYCE plan membership."""
    for plan in _dict_items(raw.get("plans")):
        pname = _text(plan.get("plan_name"))
        if "Plus" in pname or "AYCE" in pname:
            return True
    return False


def parse_product(raw: dict[str, Any], locale: str = "us") -> Product:
    """Parse a raw API product dict into a Product.

    Handles the nested response format from Audible's catalog API.
    """
    if not isinstance(raw, dict):
        raise ValueError("product record must be an object")
    asin = raw.get("asin")
    title = raw.get("title")
    if not isinstance(asin, str) or not asin.strip():
        raise ValueError("product record is missing a valid ASIN")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("product record is missing a valid title")

    price, list_price = _extract_prices(raw)

    authors = [
        name
        for author in _dict_items(raw.get("authors"))
        if (name := _text(author.get("name")))
    ]
    narrators = [
        name
        for narrator in _dict_items(raw.get("narrators"))
        if (name := _text(narrator.get("name")))
    ]

    rating, num_ratings = _extract_rating(raw)
    categories, category_ids = _extract_categories(raw)
    series_name, series_position, series_asin = _extract_series(raw)
    in_plus = _extract_plus(raw)

    return Product(
        asin=asin,
        title=title,
        subtitle=_text(raw.get("subtitle")),
        authors=authors,
        narrators=narrators,
        publisher=_text(raw.get("publisher_name")),
        price=price,
        list_price=list_price,
        length_minutes=_finite_int(raw.get("runtime_length_min"), 0),
        rating=rating,
        num_ratings=num_ratings,
        categories=categories,
        category_ids=category_ids,
        series_name=series_name,
        series_position=series_position,
        series_asin=series_asin,
        language=_text(raw.get("language")),
        release_date=_text(raw.get("release_date")),
        in_plus_catalog=in_plus,
        locale=locale,
    )


def _base_price(obj) -> float | None:
    """Pull the numeric base amount from a {'base': x} price dict."""
    if isinstance(obj, dict) and obj.get("base") is not None:
        return _finite_float(obj["base"], minimum=0)
    return None


def _extract_prices(raw: dict) -> tuple[float | None, float | None]:
    """Extract (current/sale price, original list price). Checks lowest_price first for deals."""
    price_obj = raw.get("price")
    price = list_price = None
    if isinstance(price_obj, dict):
        list_price = _base_price(price_obj.get("list_price"))
        price = _base_price(price_obj.get("lowest_price"))
        if price is None:
            price = list_price
    elif isinstance(price_obj, (int, float)) and not isinstance(price_obj, bool):
        price = _finite_float(price_obj, minimum=0)
    if list_price is None:
        lp = raw.get("list_price")
        if isinstance(lp, (int, float)) and not isinstance(lp, bool):
            list_price = _finite_float(lp, minimum=0)
    return price, list_price
