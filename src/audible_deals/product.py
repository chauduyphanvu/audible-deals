"""Audiobook product data model and catalog API response parsing."""

from __future__ import annotations

import logging
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


def _extract_rating(raw: dict) -> tuple[float, int]:
    """Extract (rating, num_ratings) from the nested overall_distribution."""
    rating_data = raw.get("rating") or {}
    rating = 0.0
    num_ratings = 0
    if isinstance(rating_data, dict):
        dist = rating_data.get("overall_distribution") or {}
        try:
            rating = float(dist.get("display_average_rating", 0) or 0)
        except (ValueError, TypeError):
            logger.debug(
                "parse_product %s: bad display_average_rating", raw.get("asin")
            )
        try:
            num_ratings = int(dist.get("num_ratings", 0) or 0)
        except (ValueError, TypeError):
            logger.debug("parse_product %s: bad num_ratings", raw.get("asin"))
    return rating, num_ratings


def _extract_categories(raw: dict) -> tuple[list[str], list[str]]:
    """Extract (category names, category ids) by flattening the ladder structure.

    Pairs are deduped on id in one pass so categories[i] always corresponds to
    category_ids[i]; build_profile relies on that positional alignment.
    """
    by_id: dict[str, str] = {}
    for ladder in raw.get("category_ladders") or []:
        for cat in ladder.get("ladder") or []:
            cid = cat.get("id", "")
            name = cat.get("name", "")
            if cid and name and cid not in by_id:
                by_id[cid] = name
    return list(by_id.values()), list(by_id.keys())


def _extract_series(raw: dict) -> tuple[str, str, str]:
    """Extract (series_name, series_position, series_asin) from the first series entry."""
    series_list = raw.get("series") or []
    if series_list:
        s = series_list[0]
        return s.get("title", ""), s.get("sequence", ""), s.get("asin", "")
    return "", "", ""


def _extract_plus(raw: dict) -> bool:
    """Detect Audible Plus / AYCE plan membership."""
    for plan in raw.get("plans") or []:
        pname = plan.get("plan_name", "")
        if "Plus" in pname or "AYCE" in pname:
            return True
    return False


def parse_product(raw: dict[str, Any], locale: str = "us") -> Product:
    """Parse a raw API product dict into a Product.

    Handles the nested response format from Audible's catalog API.
    """
    price, list_price = _extract_prices(raw)

    authors = [a.get("name", "") for a in (raw.get("authors") or []) if a.get("name")]
    narrators = [
        n.get("name", "") for n in (raw.get("narrators") or []) if n.get("name")
    ]

    rating, num_ratings = _extract_rating(raw)
    categories, category_ids = _extract_categories(raw)
    series_name, series_position, series_asin = _extract_series(raw)
    in_plus = _extract_plus(raw)

    return Product(
        asin=raw.get("asin", ""),
        title=raw.get("title", ""),
        subtitle=raw.get("subtitle", ""),
        authors=authors,
        narrators=narrators,
        publisher=raw.get("publisher_name", ""),
        price=price,
        list_price=list_price,
        length_minutes=raw.get("runtime_length_min", 0) or 0,
        rating=rating,
        num_ratings=num_ratings,
        categories=categories,
        category_ids=category_ids,
        series_name=series_name,
        series_position=series_position,
        series_asin=series_asin,
        language=raw.get("language", ""),
        release_date=raw.get("release_date", ""),
        in_plus_catalog=in_plus,
        locale=locale,
    )


def _base_price(obj) -> float | None:
    """Pull the numeric base amount from a {'base': x} price dict."""
    if isinstance(obj, dict) and obj.get("base") is not None:
        try:
            return float(obj["base"])
        except (ValueError, TypeError):
            return None
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
    elif isinstance(price_obj, (int, float)):
        price = float(price_obj)
    if list_price is None:
        lp = raw.get("list_price")
        if isinstance(lp, (int, float)):
            list_price = float(lp)
    return price, list_price
