"""Regression tests for product.py bug fixes."""

from __future__ import annotations

import pytest

from audible_deals.product import (
    _base_price,
    _extract_categories,
    _extract_prices,
    parse_product,
)
from audible_deals.taste import build_profile


# ===================================================================
# Bug 27: non-numeric price base must not crash the whole page
# ===================================================================


class TestBasePriceNonNumeric:
    @pytest.mark.parametrize("bad", ["", "N/A", {"nested": 1}, [1, 2]])
    def test_base_price_returns_none_on_non_numeric(self, bad):
        assert _base_price({"base": bad}) is None

    def test_extract_prices_empty_string_base(self):
        raw = {"price": {"lowest_price": {"base": ""}}}
        assert _extract_prices(raw) == (None, None)

    def test_extract_prices_na_base(self):
        raw = {
            "price": {"lowest_price": {"base": "N/A"}, "list_price": {"base": 14.99}}
        }
        # Falls back to list_price when the sale price is unparsable.
        assert _extract_prices(raw) == (14.99, 14.99)

    def test_extract_prices_string_numeric_still_parses(self):
        raw = {"price": {"lowest_price": {"base": "12.99"}}}
        assert _extract_prices(raw) == (12.99, None)

    def test_parse_product_survives_bad_price(self):
        raw = {"asin": "X", "title": "X", "price": {"lowest_price": {"base": ""}}}
        p = parse_product(raw)
        assert p.asin == "X"
        assert p.price is None


# ===================================================================
# Bug 28: category names/ids must stay positionally aligned
# ===================================================================


class TestCategoryAlignment:
    def test_missing_name_does_not_misalign(self):
        raw = {
            "category_ladders": [
                {
                    "ladder": [
                        {"name": "", "id": "ID_A"},
                        {"name": "Mystery", "id": "ID_B"},
                    ]
                }
            ]
        }
        categories, category_ids = _extract_categories(raw)
        assert len(categories) == len(category_ids)
        pairs = dict(zip(category_ids, categories))
        # The unnamed entry is dropped (no blank genre leaks into display/stats),
        # and the named entry keeps its correct id->name pairing.
        assert "ID_A" not in pairs
        assert pairs["ID_B"] == "Mystery"

    def test_existing_dedup_behavior_preserved(self):
        raw = {
            "category_ladders": [
                {"ladder": [{"id": "c1", "name": "Fiction"}]},
                {
                    "ladder": [
                        {"id": "c1", "name": "Fiction"},
                        {"id": "c3", "name": "Thriller"},
                    ]
                },
            ]
        }
        categories, category_ids = _extract_categories(raw)
        assert category_ids.count("c1") == 1
        assert categories.count("Fiction") == 1

    def test_build_profile_keeps_correct_genre_label(self):
        raw = {
            "asin": "B1",
            "title": "Book",
            "category_ladders": [
                {
                    "ladder": [
                        {"name": "", "id": "ID_A"},
                        {"name": "Mystery", "id": "ID_B"},
                    ]
                }
            ],
        }
        p = parse_product(raw)
        profile = build_profile([p])
        genres = {g["id"]: g["name"] for g in profile["genres"]}
        # The real Mystery genre (ID_B) must be present and correctly labelled,
        # and the unnamed ID_A must not be mislabelled as Mystery.
        assert genres.get("ID_B") == "Mystery"
        assert genres.get("ID_A", "") != "Mystery"
