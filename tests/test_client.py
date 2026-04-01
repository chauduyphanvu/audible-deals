"""Tests for audible_deals.client — Product model, parsing, price extraction."""

from __future__ import annotations

import json
import time

import pytest

from audible_deals.client import (
    Product,
    _extract_list_price,
    _extract_price,
    parse_product,
)
from tests.conftest import RAW_API_PRODUCT, RAW_API_PRODUCT_MINIMAL, make_product


# ===================================================================
# Product dataclass properties
# ===================================================================

class TestProductProperties:
    def test_full_title_with_subtitle(self):
        p = make_product(title="Main", subtitle="Sub")
        assert p.full_title == "Main: Sub"

    def test_full_title_without_subtitle(self):
        p = make_product(title="Main", subtitle="")
        assert p.full_title == "Main"

    def test_hours_conversion(self):
        p = make_product(length_minutes=150)
        assert p.hours == 2.5

    def test_hours_zero(self):
        p = make_product(length_minutes=0)
        assert p.hours == 0.0

    def test_discount_pct(self):
        p = make_product(price=5.0, list_price=20.0)
        assert p.discount_pct == 75

    def test_discount_pct_no_discount(self):
        p = make_product(price=20.0, list_price=20.0)
        assert p.discount_pct == 0

    def test_discount_pct_no_price(self):
        p = make_product(price=None, list_price=20.0)
        assert p.discount_pct is None

    def test_discount_pct_no_list_price(self):
        p = make_product(price=5.0, list_price=None)
        assert p.discount_pct is None

    def test_discount_pct_zero_list_price(self):
        p = make_product(price=5.0, list_price=0.0)
        assert p.discount_pct is None

    def test_authors_str_truncates(self):
        p = make_product(authors=["A", "B", "C", "D"])
        assert p.authors_str == "A, B, C"

    def test_narrators_str_truncates(self):
        p = make_product(narrators=["N1", "N2", "N3"])
        assert p.narrators_str == "N1, N2"

    def test_url(self):
        p = make_product(asin="B00FOOBAR")
        assert p.url == "https://www.audible.com/pd/B00FOOBAR"


# ===================================================================
# Price extraction
# ===================================================================

class TestPriceExtraction:
    def test_lowest_price(self):
        raw = {"price": {"lowest_price": {"base": 2.99}, "list_price": {"base": 15.0}}}
        assert _extract_price(raw) == 2.99

    def test_falls_back_to_list_price(self):
        raw = {"price": {"list_price": {"base": 15.0}}}
        assert _extract_price(raw) == 15.0

    def test_simple_numeric_price(self):
        raw = {"price": 9.99}
        assert _extract_price(raw) == 9.99

    def test_no_price(self):
        raw = {}
        assert _extract_price(raw) is None

    def test_none_base(self):
        raw = {"price": {"lowest_price": {"base": None}, "list_price": {"base": None}}}
        assert _extract_price(raw) is None

    def test_extract_list_price_nested(self):
        raw = {"price": {"list_price": {"base": 20.0}}}
        assert _extract_list_price(raw) == 20.0

    def test_extract_list_price_top_level(self):
        raw = {"list_price": 25.0}
        assert _extract_list_price(raw) == 25.0

    def test_extract_list_price_missing(self):
        raw = {}
        assert _extract_list_price(raw) is None


# ===================================================================
# parse_product
# ===================================================================

class TestParseProduct:
    def test_full_product(self, raw_api_product):
        p = parse_product(raw_api_product)
        assert p.asin == "B00RAWTEST"
        assert p.title == "Raw Title"
        assert p.subtitle == "Raw Sub"
        assert p.authors == ["Author A", "Author B"]
        assert p.narrators == ["Narrator X"]
        assert p.publisher == "Raw Publisher"
        assert p.price == 3.99
        assert p.list_price == 14.99
        assert p.length_minutes == 720
        assert p.rating == 4.5
        assert p.num_ratings == 2500
        assert "Science Fiction & Fantasy" in p.categories
        assert "cat1" in p.category_ids
        assert p.series_name == "Epic Series"
        assert p.series_position == "3"
        assert p.language == "english"
        assert p.in_plus_catalog is True

    def test_minimal_product(self, raw_api_product_minimal):
        p = parse_product(raw_api_product_minimal)
        assert p.asin == "B00MINIMAL"
        assert p.title == "Minimal"
        assert p.price is None
        assert p.authors == []
        assert p.categories == []
        assert p.in_plus_catalog is False

    def test_category_deduplication(self):
        raw = {
            "asin": "X", "title": "X",
            "category_ladders": [
                {"ladder": [{"id": "c1", "name": "Fiction"}, {"id": "c2", "name": "Mystery"}]},
                {"ladder": [{"id": "c1", "name": "Fiction"}, {"id": "c3", "name": "Thriller"}]},
            ],
        }
        p = parse_product(raw)
        assert p.categories.count("Fiction") == 1
        assert p.category_ids.count("c1") == 1

    def test_plus_detection_ayce(self):
        raw = {"asin": "X", "title": "X", "plans": [{"plan_name": "AYCE Monthly"}]}
        p = parse_product(raw)
        assert p.in_plus_catalog is True

    def test_rating_handles_bad_data(self):
        raw = {"asin": "X", "title": "X", "rating": {"overall_distribution": {
            "display_average_rating": "bad", "num_ratings": "bad"
        }}}
        p = parse_product(raw)
        assert p.rating == 0.0
        assert p.num_ratings == 0


# ===================================================================
# Category caching (disk)
# ===================================================================

class TestCategoryCache:
    def test_save_and_load(self, tmp_config):
        from audible_deals.client import DealsClient
        dc = DealsClient(locale="us")
        dc.auth_file = tmp_config / "auth.json"

        cats = [{"id": "1", "name": "Fiction"}, {"id": "2", "name": "SciFi"}]
        dc._save_categories_cache(cats)

        loaded = dc._load_categories_cache()
        assert loaded == cats

    def test_expired_cache(self, tmp_config, monkeypatch):
        from audible_deals.client import DealsClient, CATEGORIES_CACHE_TTL
        dc = DealsClient(locale="us")

        cats = [{"id": "1", "name": "Fiction"}]
        dc._save_categories_cache(cats)

        # Simulate stale cache by shifting time forward
        real_time = time.time
        monkeypatch.setattr(time, "time", lambda: real_time() + CATEGORIES_CACHE_TTL + 1)
        loaded = dc._load_categories_cache()
        assert loaded is None

    def test_missing_cache(self, tmp_config):
        from audible_deals.client import DealsClient
        dc = DealsClient(locale="us")
        assert dc._load_categories_cache() is None

    def test_corrupt_cache(self, tmp_config):
        from audible_deals.client import DealsClient, CATEGORIES_CACHE_FILE
        cache_file = CATEGORIES_CACHE_FILE.with_suffix(".us.json")
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text("not json{{{")

        dc = DealsClient(locale="us")
        assert dc._load_categories_cache() is None


# ===================================================================
# Genre resolution
# ===================================================================

class TestResolveGenre:
    def _make_client_with_cats(self, cats):
        from audible_deals.client import DealsClient
        dc = DealsClient(locale="us")
        dc._categories_cache = cats
        return dc

    def test_exact_match(self):
        cats = [{"id": "1", "name": "Romance"}, {"id": "2", "name": "History"}]
        dc = self._make_client_with_cats(cats)
        assert dc.resolve_genre("romance") == ("1", "Romance")

    def test_alias_expansion(self):
        cats = [{"id": "1", "name": "Science Fiction & Fantasy"}]
        dc = self._make_client_with_cats(cats)
        assert dc.resolve_genre("sci-fi") == ("1", "Science Fiction & Fantasy")

    def test_substring_match(self):
        cats = [{"id": "1", "name": "Mystery, Thriller & Suspense"}]
        dc = self._make_client_with_cats(cats)
        cid, name = dc.resolve_genre("thriller")
        assert cid == "1"

    def test_ambiguous_raises(self):
        cats = [{"id": "1", "name": "Art History"}, {"id": "2", "name": "Art & Design"}]
        dc = self._make_client_with_cats(cats)
        with pytest.raises(ValueError, match="Ambiguous"):
            dc.resolve_genre("art")

    def test_no_match_raises(self):
        cats = [{"id": "1", "name": "Romance"}]
        dc = self._make_client_with_cats(cats)
        with pytest.raises(ValueError, match="No genre matching"):
            dc.resolve_genre("zzzznothing")
