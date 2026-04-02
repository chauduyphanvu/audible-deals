"""Integration tests — exercise real DealsClient methods and CLI flag combinations.

These tests mock at the audible.Client.get() level (HTTP boundary) so the
entire DealsClient → CLI → display pipeline runs with real code.
"""

from __future__ import annotations

import csv
import datetime
import json
from io import StringIO
from unittest.mock import call

import pytest
from click.testing import CliRunner

from audible_deals.client import DealsClient, MAX_PAGE_SIZE
from audible_deals.cli import _export_products, _record_prices, cli
from tests.conftest import make_product, make_raw


# ===================================================================
# A. Client-level integration (mock audible.Client.get)
# ===================================================================

class TestClientIntegration:
    def _make_client(self, api):
        dc = DealsClient(auth_file=api.tmp_path / "auth.json", locale="us")
        return dc

    def test_search_catalog_tuple_response(self, api):
        """Tuple responses are unwrapped correctly."""
        api.get_mock.return_value = (
            {"products": [make_raw("A1")], "total_results": 1},
            {},  # extra element the audible lib sometimes returns
        )
        dc = self._make_client(api)
        products, total = dc.search_catalog(keywords="test")
        assert len(products) == 1
        assert products[0].asin == "A1"
        assert total == 1

    def test_search_catalog_dict_response(self, api):
        """Plain dict responses work identically."""
        api.get_mock.return_value = {
            "products": [make_raw("A2")],
            "total_results": 1,
        }
        dc = self._make_client(api)
        products, total = dc.search_catalog(keywords="test")
        assert len(products) == 1
        assert products[0].asin == "A2"

    def test_search_pages_stops_at_total(self, api):
        """Pagination stops when page * 50 >= total."""
        page1 = [make_raw(f"P{i}") for i in range(50)]
        page2 = [make_raw(f"Q{i}") for i in range(25)]

        api.get_mock.side_effect = [
            {"products": page1, "total_results": 75},
            {"products": page2, "total_results": 75},
        ]
        dc = self._make_client(api)
        results = list(dc.search_pages(max_pages=10))
        assert len(results) == 2
        assert len(results[0][0]) == 50
        assert len(results[1][0]) == 25
        assert api.get_mock.call_count == 2

    def test_search_pages_stops_on_empty(self, api):
        """Pagination stops when a page returns no products."""
        page1 = [make_raw(f"P{i}") for i in range(50)]

        api.get_mock.side_effect = [
            {"products": page1, "total_results": 200},
            {"products": [], "total_results": 200},
        ]
        dc = self._make_client(api)
        results = list(dc.search_pages(max_pages=10))
        assert len(results) == 2
        assert len(results[1][0]) == 0

    def test_get_products_batch_splits(self, api):
        """Batches of >50 are split into multiple API calls."""
        def mock_get(endpoint, **kwargs):
            asins = kwargs.get("asins", "").split(",")
            return {"products": [make_raw(a) for a in asins]}

        api.get_mock.side_effect = mock_get
        dc = self._make_client(api)
        asins = [f"B{i:03d}" for i in range(75)]
        products = dc.get_products_batch(asins)
        assert len(products) == 75
        assert api.get_mock.call_count == 2

    def test_get_products_batch_skips_missing(self, api):
        """Missing/invalid products in batch response are silently skipped."""
        api.get_mock.return_value = {
            "products": [
                make_raw("OK1"),
                {"asin": "", "title": ""},  # invalid — empty asin
                make_raw("OK2"),
            ]
        }
        dc = self._make_client(api)
        products = dc.get_products_batch(["OK1", "MISSING", "OK2"])
        assert len(products) == 2
        assert {p.asin for p in products} == {"OK1", "OK2"}

    def test_get_library_asins_multi_page(self, api):
        """Library pagination fetches multiple pages until <1000 items."""
        page1_items = [{"asin": f"LIB{i:04d}"} for i in range(1000)]
        page2_items = [{"asin": f"LIB{i:04d}"} for i in range(1000, 1500)]

        api.get_mock.side_effect = [
            {"items": page1_items},
            {"items": page2_items},
        ]
        dc = self._make_client(api)
        asins = dc.get_library_asins()
        assert len(asins) == 1500
        assert api.get_mock.call_count == 2

    def test_get_library_asins_caches(self, api):
        """Second call returns cached result without API call."""
        api.get_mock.return_value = {"items": [{"asin": "X"}]}
        dc = self._make_client(api)
        dc.get_library_asins()
        dc.get_library_asins()
        assert api.get_mock.call_count == 1

    def test_get_wishlist_single_page(self, api):
        """get_wishlist() parses products and stops when page is under max."""
        api.get_mock.return_value = {
            "products": [make_raw("WL1"), make_raw("WL2")],
        }
        dc = self._make_client(api)
        products = dc.get_wishlist()
        assert len(products) == 2
        assert {p.asin for p in products} == {"WL1", "WL2"}
        assert api.get_mock.call_count == 1
        # Verify correct endpoint and params
        call_args = api.get_mock.call_args
        assert "wishlist" in call_args[0][0]
        assert call_args[1]["page"] == 0

    def test_get_wishlist_multi_page(self, api):
        """get_wishlist() paginates until a page has fewer than MAX_PAGE_SIZE items."""
        page1 = [make_raw(f"WP{i}") for i in range(50)]
        page2 = [make_raw(f"WQ{i}") for i in range(10)]

        api.get_mock.side_effect = [
            {"products": page1},
            {"products": page2},
        ]
        dc = self._make_client(api)
        products = dc.get_wishlist()
        assert len(products) == 60
        assert api.get_mock.call_count == 2

    def test_get_wishlist_empty(self, api):
        """get_wishlist() returns empty list when Audible wishlist is empty."""
        api.get_mock.return_value = {"products": []}
        dc = self._make_client(api)
        products = dc.get_wishlist()
        assert products == []
        assert api.get_mock.call_count == 1

    def test_get_wishlist_tuple_response(self, api):
        """Tuple responses are unwrapped correctly in get_wishlist()."""
        api.get_mock.return_value = (
            {"products": [make_raw("WT1")]},
            {},
        )
        dc = self._make_client(api)
        products = dc.get_wishlist()
        assert len(products) == 1
        assert products[0].asin == "WT1"

    def test_import_auth_libation(self, api):
        """Libation AccountsSettings.json format is extracted correctly."""
        libation_data = {
            "Accounts": [{
                "IdentityTokens": {
                    "access_token": "at123",
                    "refresh_token": "rt456",
                    "adp_token": "adp",
                    "device_private_key": "dpk",
                    "device_info": {"id": "dev1"},
                    "customer_info": {"name": "User"},
                    "locale_code": "us",
                }
            }]
        }
        src = api.tmp_path / "libation.json"
        src.write_text(json.dumps(libation_data))

        dc = self._make_client(api)
        dc.import_auth(src)

        written = json.loads(dc.auth_file.read_text())
        assert written["access_token"] == "at123"
        assert written["refresh_token"] == "rt456"
        assert written["encryption"] is False
        assert "Accounts" not in written

    def test_import_auth_audiblecli(self, api):
        """audible-cli Mkb79Auth format gets encryption=False added."""
        cli_data = {"access_token": "tok", "refresh_token": "rt"}
        src = api.tmp_path / "audible_auth.json"
        src.write_text(json.dumps(cli_data))

        dc = self._make_client(api)
        dc.import_auth(src)

        written = json.loads(dc.auth_file.read_text())
        assert written["access_token"] == "tok"
        assert written["encryption"] is False

    def test_categories_cache_miss_then_hit(self, api):
        """First call fetches from API and caches; second reads from disk."""
        api.get_mock.return_value = {
            "categories": [{"id": "1", "name": "Fiction"}, {"id": "2", "name": "SciFi"}]
        }
        dc1 = self._make_client(api)
        cats1 = dc1.get_categories()
        assert len(cats1) == 2
        assert api.get_mock.call_count == 1

        # Fresh instance, same locale — should read from cache
        api.get_mock.reset_mock()
        dc2 = self._make_client(api)
        cats2 = dc2.get_categories()
        assert cats2 == cats1
        assert api.get_mock.call_count == 0

    def test_categories_subcategory_no_cache(self, api):
        """Subcategory fetches hit API and don't write cache."""
        api.get_mock.return_value = {
            "category": {
                "children": [{"id": "sub1", "name": "Hard SciFi"}]
            }
        }
        dc = self._make_client(api)
        subs = dc.get_categories(root="parent123")
        assert len(subs) == 1
        assert subs[0]["name"] == "Hard SciFi"

        # Verify the call was made to the subcategory endpoint
        api.get_mock.assert_called_once()
        call_args = api.get_mock.call_args
        assert "parent123" in call_args[0][0]


# ===================================================================
# B. CLI pipeline integration (flag combinations via CliRunner)
# ===================================================================

class TestCLIPipelineIntegration:
    def test_deep_mode_deduplicates(self, mock_client, tmp_config):
        """--deep iterates 3 sort orders and deduplicates overlapping ASINs."""
        # Each sort order returns different products with some overlap
        pass1 = [make_product(asin="D1", price=3.0, series_name="", series_position=""),
                 make_product(asin="D2", price=4.0, series_name="", series_position="")]
        pass2 = [make_product(asin="D2", price=4.0, series_name="", series_position=""),  # overlap
                 make_product(asin="D3", price=5.0, series_name="", series_position="")]
        pass3 = [make_product(asin="D1", price=3.0, series_name="", series_position=""),  # overlap
                 make_product(asin="D4", price=2.0, series_name="", series_position="")]

        call_count = 0
        def fake_search_pages(**kwargs):
            nonlocal call_count
            data = [pass1, pass2, pass3][call_count]
            call_count += 1
            yield data, 1, len(data)

        mock_client.search_pages.side_effect = fake_search_pages

        out_file = tmp_config / "deep.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--deep", "--pages", "1", "--max-price", "20",
            "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [item["asin"] for item in data]
        assert sorted(asins) == ["D1", "D2", "D3", "D4"]

    def test_skip_owned(self, mock_client, tmp_config):
        """--skip-owned excludes ASINs from the user's library."""
        products = [
            make_product(asin="OWNED1", price=3.0, series_name="", series_position=""),
            make_product(asin="FREE1", price=2.0, series_name="", series_position=""),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        mock_client.get_library_asins.return_value = {"OWNED1"}

        out_file = tmp_config / "skip.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--skip-owned", "--pages", "1", "--max-price", "20",
            "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [item["asin"] for item in data]
        assert "OWNED1" not in asins
        assert "FREE1" in asins

    def test_all_languages_includes_all(self, mock_client, tmp_config):
        """--all-languages bypasses the default locale language filter."""
        products = [
            make_product(asin="EN1", price=3.0, language="english", series_name=""),
            make_product(asin="FR1", price=3.0, language="french", series_name=""),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])

        out_file = tmp_config / "alllang.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--all-languages", "--pages", "1", "--max-price", "20",
            "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        langs = {item["language"] for item in data}
        assert "english" in langs
        assert "french" in langs

    def test_default_language_filter(self, mock_client, tmp_config):
        """Default locale=us filters to english only."""
        products = [
            make_product(asin="EN1", price=3.0, language="english", series_name=""),
            make_product(asin="FR1", price=3.0, language="french", series_name=""),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])

        out_file = tmp_config / "deflang.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--pages", "1", "--max-price", "20",
            "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        langs = {item["language"] for item in data}
        assert langs == {"english"}

    def test_categories_root(self, mock_client, tmp_config):
        """categories command displays top-level list."""
        mock_client.get_categories.return_value = [
            {"id": "1", "name": "Fiction"}, {"id": "2", "name": "SciFi"},
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["categories"])
        assert result.exit_code == 0, result.output
        assert "Top-Level" in result.output
        assert "Fiction" in result.output

    def test_categories_parent(self, mock_client, tmp_config):
        """categories --parent drills into subcategories."""
        mock_client.get_categories.return_value = [
            {"id": "1a", "name": "Hard SciFi"},
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["categories", "--parent", "ABC123"])
        assert result.exit_code == 0, result.output
        assert "Subcategories" in result.output
        assert "Hard SciFi" in result.output
        mock_client.get_categories.assert_called_once_with(root="ABC123")

    def test_combined_first_in_series_exclude_genre_limit(self, mock_client, tmp_config):
        """Multiple flags interact correctly in one pipeline."""
        products = [
            make_product(asin="S1P1", series_name="S1", series_position="1",
                         price=3.0, category_ids=["cat_ok"]),
            make_product(asin="S1P2", series_name="S1", series_position="2",
                         price=2.0, category_ids=["cat_ok"]),
            make_product(asin="S1P3", series_name="S1", series_position="3",
                         price=1.0, category_ids=["cat_ok"]),
            make_product(asin="ERO1", series_name="", series_position="",
                         price=1.0, category_ids=["cat_erotica"]),
            make_product(asin="SOLO1", series_name="", series_position="",
                         price=4.0, category_ids=["cat_ok"]),
            make_product(asin="SOLO2", series_name="", series_position="",
                         price=5.0, category_ids=["cat_ok"]),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, len(products))])
        mock_client.resolve_genre.return_value = ("cat_erotica", "Erotica")

        out_file = tmp_config / "combined.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--first-in-series", "--exclude-genre", "erotica",
            "--limit", "2", "--pages", "1", "--max-price", "20",
            "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [item["asin"] for item in data]
        # ERO1 excluded by genre, S1P2/S1P3 collapsed by first-in-series, limit 2
        assert "ERO1" not in asins
        assert "S1P2" not in asins
        assert "S1P3" not in asins
        assert len(asins) == 2


# ===================================================================
# C. Edge cases
# ===================================================================

class TestEdgeCases:
    def test_empty_search_results(self, mock_client, tmp_config):
        """CLI handles zero search results gracefully."""
        mock_client.search_pages.return_value = iter([([], 1, 0)])

        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--pages", "1", "--max-price", "10",
        ])
        assert result.exit_code == 0, result.output
        assert "No products found" in result.output

    def test_record_prices_365_cap(self, tmp_config):
        """History is truncated to 365 entries."""
        hist_dir = tmp_config / "history"
        hist_dir.mkdir(parents=True, exist_ok=True)
        hist_file = hist_dir / "CAP1.json"

        # Pre-write 365 entries with dates going back
        old_entries = [
            {"date": f"2025-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", "price": 10.0 + i * 0.01}
            for i in range(365)
        ]
        hist_file.write_text(json.dumps(old_entries))

        # Record a new price (today, which is different from all old dates)
        products = [make_product(asin="CAP1", price=1.99)]
        _record_prices(products)

        entries = json.loads(hist_file.read_text())
        assert len(entries) == 365
        assert entries[-1]["price"] == 1.99  # newest entry
        assert entries[0] != old_entries[0]  # oldest was dropped

    def test_record_prices_cross_day(self, tmp_config):
        """Recording on a new day appends a new entry."""
        hist_dir = tmp_config / "history"
        hist_dir.mkdir(parents=True, exist_ok=True)
        hist_file = hist_dir / "DAY1.json"

        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        hist_file.write_text(json.dumps([{"date": yesterday, "price": 9.99}]))

        products = [make_product(asin="DAY1", price=5.99)]
        _record_prices(products)

        entries = json.loads(hist_file.read_text())
        assert len(entries) == 2
        assert entries[0]["date"] == yesterday
        assert entries[1]["date"] == datetime.date.today().isoformat()
        assert entries[1]["price"] == 5.99

    def test_csv_list_field_joining(self, tmp_path):
        """CSV export joins list fields with '; ' separator."""
        products = [make_product(
            asin="CSV1",
            authors=["Alice", "Bob", "Carol"],
            narrators=["Narrator A", "Narrator B"],
            categories=["Fiction", "Mystery"],
            category_ids=["c1", "c2"],
        )]
        path = tmp_path / "lists.csv"
        _export_products(products, path)

        with open(path) as f:
            reader = csv.DictReader(f)
            row = next(reader)
        assert row["authors"] == "Alice; Bob; Carol"
        assert row["narrators"] == "Narrator A; Narrator B"
        assert row["categories"] == "Fiction; Mystery"
        assert row["category_ids"] == "c1; c2"
