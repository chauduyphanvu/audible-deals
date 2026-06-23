"""Regression tests for confirmed bugs in audible_deals.client."""

from __future__ import annotations

import json

from audible_deals.client import DealsClient


# ===================================================================
# Bug 5: categories cache with valid-JSON-but-non-dict content must
# be treated as a cache miss, not crash with AttributeError.
# ===================================================================


class TestNonDictCategoriesCache:
    def _write_cache(self, content: str) -> None:
        from audible_deals.client import CATEGORIES_CACHE_FILE

        cache_file = CATEGORIES_CACHE_FILE.with_suffix(".us.json")
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(content)

    def test_list_content_is_cache_miss(self, tmp_config):
        self._write_cache(json.dumps([1, 2, 3]))
        dc = DealsClient(locale="us")
        assert dc._load_categories_cache() is None

    def test_string_content_is_cache_miss(self, tmp_config):
        self._write_cache(json.dumps("just a string"))
        dc = DealsClient(locale="us")
        assert dc._load_categories_cache() is None

    def test_number_content_is_cache_miss(self, tmp_config):
        self._write_cache(json.dumps(42))
        dc = DealsClient(locale="us")
        assert dc._load_categories_cache() is None


# ===================================================================
# Bug 6: get_wishlist must drop entries missing asin/title, matching
# get_library_pages and get_products_batch.
# ===================================================================


class TestWishlistFiltersIncompleteEntries:
    def _make_dc(self, api):
        return DealsClient(auth_file=api.tmp_path / "auth.json", locale="us")

    def test_drops_entries_without_asin_or_title(self, api):
        api.get_mock.return_value = {
            "products": [
                {"asin": "B001GOOD", "title": "Good Book"},
                {"asin": "", "title": "No ASIN"},
                {"asin": "B002NOTITLE", "title": ""},
                {"title": "Missing ASIN key"},
            ]
        }
        dc = self._make_dc(api)
        with dc:
            products = dc.get_wishlist()
        assert [p.asin for p in products] == ["B001GOOD"]

    def test_pagination_uses_raw_length_not_filtered(self, api):
        from audible_deals.constants import MAX_PAGE_SIZE

        # A full first page where every entry lacks an asin: filtered list is
        # empty but pagination must continue because the raw page was full.
        page0 = {"products": [{"asin": "", "title": "x"} for _ in range(MAX_PAGE_SIZE)]}
        page1 = {"products": [{"asin": "B00REAL", "title": "Real Book"}]}
        api.get_mock.side_effect = [page0, page1]
        dc = self._make_dc(api)
        with dc:
            products = dc.get_wishlist()
        assert api.get_mock.call_count == 2
        assert [p.asin for p in products] == ["B00REAL"]
