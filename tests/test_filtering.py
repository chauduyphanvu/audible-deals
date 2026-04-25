"""Tests for audible_deals.filtering — pure product filtering, sorting, deduplication."""

from __future__ import annotations

import pytest

from audible_deals.filtering import (
    dedupe_editions,
    filter_products,
    first_in_series,
    price_per_hour,
    sort_local,
    value_score,
)
from audible_deals.client import Product
from tests.conftest import make_product


# ===================================================================
# filter_products
# ===================================================================

class TestFilterProducts:
    def test_max_price(self, products_for_filtering):
        filtered, breakdown = filter_products(products_for_filtering, max_price=5.0)
        assert all(p.price is not None and p.price <= 5.0 for p in filtered)
        assert breakdown.get("max price", 0) > 0

    def test_min_rating(self, products_for_filtering):
        filtered, _ = filter_products(products_for_filtering, min_rating=4.0)
        assert all(p.rating >= 4.0 for p in filtered)

    def test_min_hours(self, products_for_filtering):
        filtered, _ = filter_products(products_for_filtering, min_hours=5.0)
        assert all(p.hours >= 5.0 for p in filtered)

    def test_language(self, products_for_filtering):
        filtered, _ = filter_products(products_for_filtering, language="french")
        assert all(p.language.lower() == "french" for p in filtered)
        assert len(filtered) == 1

    def test_on_sale(self, products_for_filtering):
        filtered, _ = filter_products(products_for_filtering, on_sale=True)
        assert all(p.discount_pct is not None and p.discount_pct > 0 for p in filtered)
        assert not any(p.asin in ("NO_PRICE", "EXPENSIVE") for p in filtered)

    def test_skip_asins(self, products_for_filtering):
        filtered, _ = filter_products(products_for_filtering, skip_asins={"CHEAP1", "CHEAP2"})
        assert not any(p.asin in {"CHEAP1", "CHEAP2"} for p in filtered)

    def test_exclude_category_ids(self, products_for_filtering):
        filtered, _ = filter_products(
            products_for_filtering, exclude_category_ids={"cat_erotica"}
        )
        assert not any(p.asin == "EROTICA" for p in filtered)

    def test_no_filters(self, products_for_filtering):
        filtered, breakdown = filter_products(products_for_filtering)
        assert len(filtered) == len(products_for_filtering)
        assert breakdown == {}

    def test_combined_filters(self, products_for_filtering):
        filtered, _ = filter_products(
            products_for_filtering,
            max_price=5.0, min_rating=4.0, language="english",
        )
        for p in filtered:
            assert p.price is not None and p.price <= 5.0
            assert p.rating >= 4.0
            assert p.language.lower() == "english"


# ===================================================================
# price_per_hour
# ===================================================================

class TestPricePerHour:
    def test_normal(self):
        p = make_product(price=10.0, length_minutes=600)
        assert price_per_hour(p) == pytest.approx(1.0)

    def test_no_price(self):
        p = make_product(price=None)
        assert price_per_hour(p) == float("inf")

    def test_zero_hours(self):
        p = make_product(price=5.0, length_minutes=0)
        assert price_per_hour(p) == float("inf")


# ===================================================================
# sort_local
# ===================================================================

class TestSortLocal:
    @pytest.fixture
    def products(self):
        return [
            make_product(asin="A", price=5.0, rating=3.0, length_minutes=300,
                         release_date="2024-01-01", list_price=10.0),
            make_product(asin="B", price=2.0, rating=5.0, length_minutes=600,
                         release_date="2024-06-01", list_price=20.0),
            make_product(asin="C", price=8.0, rating=4.0, length_minutes=120,
                         release_date="2023-01-01", list_price=10.0),
        ]

    def test_sort_price(self, products):
        result = sort_local(products, "price")
        prices = [p.price for p in result]
        assert prices == sorted(prices)

    def test_sort_price_reverse(self, products):
        result = sort_local(products, "-price")
        prices = [p.price for p in result]
        assert prices == sorted(prices, reverse=True)

    def test_sort_rating(self, products):
        result = sort_local(products, "rating")
        ratings = [p.rating for p in result]
        assert ratings == sorted(ratings, reverse=True)

    def test_sort_length(self, products):
        result = sort_local(products, "length")
        lengths = [p.length_minutes for p in result]
        assert lengths == sorted(lengths, reverse=True)

    def test_sort_date(self, products):
        result = sort_local(products, "date")
        dates = [p.release_date for p in result]
        assert dates == sorted(dates, reverse=True)

    def test_sort_discount(self, products):
        result = sort_local(products, "discount")
        discounts = [p.discount_pct or 0 for p in result]
        assert discounts == sorted(discounts, reverse=True)

    def test_sort_price_per_hour(self, products):
        result = sort_local(products, "price-per-hour")
        pphs = [price_per_hour(p) for p in result]
        assert pphs == sorted(pphs)

    def test_sort_unknown_passthrough(self, products):
        result = sort_local(products, "relevance")
        assert [p.asin for p in result] == ["A", "B", "C"]

    def test_sort_price_with_none(self):
        products = [
            make_product(asin="X", price=None),
            make_product(asin="Y", price=3.0),
        ]
        result = sort_local(products, "price")
        assert result[0].asin == "Y"
        assert result[1].asin == "X"


# ===================================================================
# dedupe_editions
# ===================================================================

class TestDedupeEditions:
    def test_keeps_cheapest(self):
        products = [
            make_product(asin="A", series_name="S", series_position="1", price=10.0),
            make_product(asin="B", series_name="S", series_position="1", price=5.0),
        ]
        result, removed = dedupe_editions(products)
        assert removed == 1
        assert len(result) == 1
        assert result[0].asin == "B"

    def test_no_series_pass_through(self):
        products = [
            make_product(asin="A", series_name="", series_position=""),
            make_product(asin="B", series_name="", series_position=""),
        ]
        result, removed = dedupe_editions(products)
        assert removed == 0
        assert len(result) == 2

    def test_different_positions_kept(self):
        products = [
            make_product(asin="A", series_name="S", series_position="1", price=5.0),
            make_product(asin="B", series_name="S", series_position="2", price=5.0),
        ]
        result, removed = dedupe_editions(products)
        assert removed == 0
        assert len(result) == 2

    def test_case_insensitive(self):
        products = [
            make_product(asin="A", series_name="Epic", series_position="1", price=10.0),
            make_product(asin="B", series_name="epic", series_position="1", price=5.0),
        ]
        result, removed = dedupe_editions(products)
        assert removed == 1


# ===================================================================
# first_in_series
# ===================================================================

class TestFirstInSeries:
    def test_keeps_lowest_position(self):
        products = [
            make_product(asin="A", series_name="S", series_position="3"),
            make_product(asin="B", series_name="S", series_position="1"),
            make_product(asin="C", series_name="S", series_position="2"),
        ]
        result, collapsed = first_in_series(products)
        assert collapsed == 2
        assert len(result) == 1
        assert result[0].asin == "B"

    def test_non_series_pass_through(self):
        products = [
            make_product(asin="A", series_name=""),
            make_product(asin="B", series_name=""),
        ]
        result, collapsed = first_in_series(products)
        assert collapsed == 0
        assert len(result) == 2

    def test_different_series(self):
        products = [
            make_product(asin="A", series_name="S1", series_position="2"),
            make_product(asin="B", series_name="S2", series_position="3"),
        ]
        result, collapsed = first_in_series(products)
        assert collapsed == 2
        assert len(result) == 0

    def test_different_series_with_book1(self):
        products = [
            make_product(asin="A", series_name="S1", series_position="1"),
            make_product(asin="B", series_name="S2", series_position="1"),
        ]
        result, collapsed = first_in_series(products)
        assert collapsed == 0
        assert len(result) == 2

    def test_non_numeric_position(self):
        products = [
            make_product(asin="A", series_name="S", series_position="Book 1"),
            make_product(asin="B", series_name="S", series_position="1"),
        ]
        result, collapsed = first_in_series(products)
        assert collapsed == 1
        assert result[0].asin == "B"


class TestFirstInSeriesStrict:
    def test_book3_only_gets_filtered_out(self):
        products = [
            make_product(asin="FIS1", series_name="Epic Series", series_position="3"),
        ]
        result, collapsed = first_in_series(products)
        assert collapsed == 1
        assert len(result) == 0

    def test_prequel_at_half_passes(self):
        products = [
            make_product(asin="FIS2", series_name="Epic Series", series_position="0.5"),
        ]
        result, collapsed = first_in_series(products)
        assert collapsed == 0
        assert len(result) == 1
        assert result[0].asin == "FIS2"

    def test_position_one_point_zero_passes(self):
        products = [
            make_product(asin="FIS3", series_name="Epic Series", series_position="1.0"),
        ]
        result, collapsed = first_in_series(products)
        assert collapsed == 0
        assert len(result) == 1
        assert result[0].asin == "FIS3"

    def test_book1_in_series_passes(self):
        products = [
            make_product(asin="FIS4", series_name="A Series", series_position="1"),
            make_product(asin="FIS5", series_name="A Series", series_position="2"),
        ]
        result, collapsed = first_in_series(products)
        assert collapsed == 1
        assert result[0].asin == "FIS4"

    def test_non_series_pass_through_unchanged(self):
        products = [
            make_product(asin="FIS6", series_name=""),
            make_product(asin="FIS7", series_name=""),
        ]
        result, collapsed = first_in_series(products)
        assert collapsed == 0
        assert len(result) == 2

    def test_mixed_book1_and_no_book1(self):
        products = [
            make_product(asin="FIS8", series_name="HasBook1", series_position="1"),
            make_product(asin="FIS9", series_name="NoBook1", series_position="3"),
        ]
        result, collapsed = first_in_series(products)
        asins = [p.asin for p in result]
        assert "FIS8" in asins
        assert "FIS9" not in asins
        assert collapsed == 1


# ===================================================================
# filter_products — series filter
# ===================================================================

class TestFilterSeries:
    def test_filter_series_match(self):
        products = [
            make_product(asin="S1", series_name="The Stormlight Archive"),
            make_product(asin="S2", series_name="Mistborn"),
            make_product(asin="S3", series_name="Stormlight Chronicles"),
        ]
        filtered, breakdown = filter_products(products, series="stormlight")
        assert len(filtered) == 2
        assert all(p.asin in ("S1", "S3") for p in filtered)

    def test_filter_series_no_match(self):
        products = [
            make_product(asin="S1", series_name="Mistborn"),
            make_product(asin="S2", series_name="The Way of Kings"),
        ]
        filtered, breakdown = filter_products(products, series="wheel of time")
        assert len(filtered) == 0
        assert breakdown.get("series") == 2

    def test_filter_series_case_insensitive(self):
        products = [
            make_product(asin="S1", series_name="The Dresden Files"),
        ]
        filtered, _ = filter_products(products, series="DRESDEN")
        assert len(filtered) == 1

    def test_filter_series_empty_no_filter(self):
        products = [
            make_product(asin="S1", series_name="Mistborn"),
            make_product(asin="S2", series_name=""),
        ]
        filtered, breakdown = filter_products(products, series="")
        assert len(filtered) == 2
        assert "series" not in breakdown
