"""Tests for audible_deals.cli — filtering, sorting, deduplication, export, commands."""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from audible_deals.cli import (
    _validate_webhook_url,
    _dedupe_editions,
    _deserialize_product,
    _export_products,
    _fetch_with_progress,
    _filter_products,
    _first_in_series,
    _load_seen_asins,
    _save_seen_asins,
    _looks_like_person_name,
    _parse_interval,
    _price_per_hour,
    _resolve_last_references,
    _serialize_product,
    _sort_local,
    _value_score,
    cli,
)
from audible_deals.client import Product
from tests.conftest import make_product


def _mock_library_pages(mock_client, products):
    """Set up get_library_pages mock yielding a single page."""
    mock_client.get_library_pages.return_value = iter([(products, 1)])


# ===================================================================
# _filter_products
# ===================================================================

class TestFilterProducts:
    def test_max_price(self, products_for_filtering):
        filtered, breakdown = _filter_products(products_for_filtering, max_price=5.0)
        assert all(p.price is not None and p.price <= 5.0 for p in filtered)
        assert breakdown.get("max price", 0) > 0

    def test_min_rating(self, products_for_filtering):
        filtered, _ = _filter_products(products_for_filtering, min_rating=4.0)
        assert all(p.rating >= 4.0 for p in filtered)

    def test_min_hours(self, products_for_filtering):
        filtered, _ = _filter_products(products_for_filtering, min_hours=5.0)
        assert all(p.hours >= 5.0 for p in filtered)

    def test_language(self, products_for_filtering):
        filtered, _ = _filter_products(products_for_filtering, language="french")
        assert all(p.language.lower() == "french" for p in filtered)
        assert len(filtered) == 1

    def test_on_sale(self, products_for_filtering):
        filtered, _ = _filter_products(products_for_filtering, on_sale=True)
        # Only items with a confirmed positive discount should pass
        assert all(p.discount_pct is not None and p.discount_pct > 0 for p in filtered)
        # NO_PRICE (None discount) and EXPENSIVE (0% discount) must be excluded
        assert not any(p.asin in ("NO_PRICE", "EXPENSIVE") for p in filtered)

    def test_skip_asins(self, products_for_filtering):
        filtered, _ = _filter_products(products_for_filtering, skip_asins={"CHEAP1", "CHEAP2"})
        assert not any(p.asin in {"CHEAP1", "CHEAP2"} for p in filtered)

    def test_exclude_category_ids(self, products_for_filtering):
        filtered, _ = _filter_products(
            products_for_filtering, exclude_category_ids={"cat_erotica"}
        )
        assert not any(p.asin == "EROTICA" for p in filtered)

    def test_no_filters(self, products_for_filtering):
        filtered, breakdown = _filter_products(products_for_filtering)
        assert len(filtered) == len(products_for_filtering)
        assert breakdown == {}

    def test_combined_filters(self, products_for_filtering):
        filtered, _ = _filter_products(
            products_for_filtering,
            max_price=5.0, min_rating=4.0, language="english",
        )
        for p in filtered:
            assert p.price is not None and p.price <= 5.0
            assert p.rating >= 4.0
            assert p.language.lower() == "english"


# ===================================================================
# _price_per_hour
# ===================================================================

class TestPricePerHour:
    def test_normal(self):
        p = make_product(price=10.0, length_minutes=600)  # 10hrs
        assert _price_per_hour(p) == pytest.approx(1.0)

    def test_no_price(self):
        p = make_product(price=None)
        assert _price_per_hour(p) == float("inf")

    def test_zero_hours(self):
        p = make_product(price=5.0, length_minutes=0)
        assert _price_per_hour(p) == float("inf")


# ===================================================================
# _sort_local
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
        result = _sort_local(products, "price")
        prices = [p.price for p in result]
        assert prices == sorted(prices)

    def test_sort_price_reverse(self, products):
        result = _sort_local(products, "-price")
        prices = [p.price for p in result]
        assert prices == sorted(prices, reverse=True)

    def test_sort_rating(self, products):
        result = _sort_local(products, "rating")
        ratings = [p.rating for p in result]
        assert ratings == sorted(ratings, reverse=True)

    def test_sort_length(self, products):
        result = _sort_local(products, "length")
        lengths = [p.length_minutes for p in result]
        assert lengths == sorted(lengths, reverse=True)

    def test_sort_date(self, products):
        result = _sort_local(products, "date")
        dates = [p.release_date for p in result]
        assert dates == sorted(dates, reverse=True)

    def test_sort_discount(self, products):
        result = _sort_local(products, "discount")
        discounts = [p.discount_pct or 0 for p in result]
        assert discounts == sorted(discounts, reverse=True)

    def test_sort_price_per_hour(self, products):
        result = _sort_local(products, "price-per-hour")
        pphs = [_price_per_hour(p) for p in result]
        assert pphs == sorted(pphs)

    def test_sort_unknown_passthrough(self, products):
        result = _sort_local(products, "relevance")
        assert [p.asin for p in result] == ["A", "B", "C"]

    def test_sort_price_with_none(self):
        products = [
            make_product(asin="X", price=None),
            make_product(asin="Y", price=3.0),
        ]
        result = _sort_local(products, "price")
        assert result[0].asin == "Y"
        assert result[1].asin == "X"


# ===================================================================
# _dedupe_editions
# ===================================================================

class TestDedupeEditions:
    def test_keeps_cheapest(self):
        products = [
            make_product(asin="A", series_name="S", series_position="1", price=10.0),
            make_product(asin="B", series_name="S", series_position="1", price=5.0),
        ]
        result, removed = _dedupe_editions(products)
        assert removed == 1
        assert len(result) == 1
        assert result[0].asin == "B"

    def test_no_series_pass_through(self):
        products = [
            make_product(asin="A", series_name="", series_position=""),
            make_product(asin="B", series_name="", series_position=""),
        ]
        result, removed = _dedupe_editions(products)
        assert removed == 0
        assert len(result) == 2

    def test_different_positions_kept(self):
        products = [
            make_product(asin="A", series_name="S", series_position="1", price=5.0),
            make_product(asin="B", series_name="S", series_position="2", price=5.0),
        ]
        result, removed = _dedupe_editions(products)
        assert removed == 0
        assert len(result) == 2

    def test_case_insensitive(self):
        products = [
            make_product(asin="A", series_name="Epic", series_position="1", price=10.0),
            make_product(asin="B", series_name="epic", series_position="1", price=5.0),
        ]
        result, removed = _dedupe_editions(products)
        assert removed == 1


# ===================================================================
# _first_in_series
# ===================================================================

class TestFirstInSeries:
    def test_keeps_lowest_position(self):
        products = [
            make_product(asin="A", series_name="S", series_position="3"),
            make_product(asin="B", series_name="S", series_position="1"),
            make_product(asin="C", series_name="S", series_position="2"),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 2
        assert len(result) == 1
        assert result[0].asin == "B"

    def test_non_series_pass_through(self):
        products = [
            make_product(asin="A", series_name=""),
            make_product(asin="B", series_name=""),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 0
        assert len(result) == 2

    def test_different_series(self):
        # Both series have their lowest position > 1.0, so both are excluded
        # (Book 1 wasn't in the result set for either series).
        products = [
            make_product(asin="A", series_name="S1", series_position="2"),
            make_product(asin="B", series_name="S2", series_position="3"),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 2
        assert len(result) == 0

    def test_different_series_with_book1(self):
        # Each series has a Book 1, so both are kept.
        products = [
            make_product(asin="A", series_name="S1", series_position="1"),
            make_product(asin="B", series_name="S2", series_position="1"),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 0
        assert len(result) == 2

    def test_non_numeric_position(self):
        products = [
            make_product(asin="A", series_name="S", series_position="Book 1"),
            make_product(asin="B", series_name="S", series_position="1"),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 1
        assert result[0].asin == "B"


# ===================================================================
# _serialize_product
# ===================================================================

class TestSerializeProduct:
    def test_includes_computed_fields(self):
        p = make_product(price=10.0, list_price=20.0, length_minutes=600)
        d = _serialize_product(p)
        assert d["full_title"] == p.full_title
        assert d["hours"] == p.hours
        assert d["discount_pct"] == p.discount_pct
        assert d["url"] == p.url
        assert "price_per_hour" in d

    def test_rounds_prices(self):
        p = make_product(price=1.9299999, list_price=10.1800001)
        d = _serialize_product(p)
        assert d["price"] == 1.93
        assert d["list_price"] == 10.18

    def test_none_price(self):
        p = make_product(price=None, list_price=None)
        d = _serialize_product(p)
        assert d["price"] is None
        assert d["list_price"] is None
        assert d["price_per_hour"] is None


# ===================================================================
# _export_products
# ===================================================================

class TestExportProducts:
    def test_json_export(self, tmp_path):
        products = [make_product(asin="E1"), make_product(asin="E2")]
        path = tmp_path / "out.json"
        _export_products(products, path)
        data = json.loads(path.read_text())
        assert len(data) == 2
        assert data[0]["asin"] == "E1"

    def test_csv_export(self, tmp_path):
        products = [make_product(asin="E1")]
        path = tmp_path / "out.csv"
        _export_products(products, path)
        content = path.read_text()
        assert "asin" in content
        assert "E1" in content

    def test_empty_csv(self, tmp_path):
        path = tmp_path / "empty.csv"
        _export_products([], path)
        assert path.read_text() == ""

    def test_unsupported_format(self, tmp_path):
        import click
        path = tmp_path / "out.xml"
        with pytest.raises(click.BadParameter, match="Unsupported"):
            _export_products([make_product()], path)


# ===================================================================
# CLI commands (via Click test runner)
# ===================================================================

class TestCLIHelp:
    def test_main_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "find" in result.output
        assert "search" in result.output
        assert "compare" in result.output
        assert "wishlist" in result.output
        assert "watch" in result.output
        assert "history" in result.output

    def test_find_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--help"])
        assert result.exit_code == 0
        assert "--limit" in result.output
        assert "--quiet" in result.output
        assert "--exclude-genre" in result.output
        assert "price-per-hour" in result.output

    def test_search_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "--help"])
        assert result.exit_code == 0
        assert "--limit" in result.output
        assert "--quiet" in result.output
        assert "--exclude-genre" in result.output


class TestFindCommand:
    def test_find_basic(self, mock_client, tmp_config):
        products = [make_product(asin=f"F{i}", price=float(i), list_price=20.0)
                     for i in range(1, 6)]
        mock_client.search_pages.return_value = iter([(products, 1, 5)])
        mock_client.resolve_genre.return_value = ("cat1", "Fiction")

        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--genre", "fiction", "--max-price", "10", "--pages", "1"])
        assert result.exit_code == 0, result.output
        assert "Deals under $10.00" in result.output

    def test_find_json_output(self, mock_client, tmp_config):
        products = [make_product(asin="J1", price=3.0)]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])

        out_file = tmp_config / "out.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--max-price", "10", "--pages", "1", "-q",
            "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 1
        assert data[0]["asin"] == "J1"

    def test_find_limit(self, mock_client, tmp_config):
        products = [make_product(asin=f"L{i}", price=float(i), series_name="", series_position="")
                     for i in range(1, 11)]
        mock_client.search_pages.return_value = iter([(products, 1, 10)])

        out_file = tmp_config / "limit.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--max-price", "20", "--pages", "1", "--limit", "3",
            "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 3

    def test_find_quiet(self, mock_client, tmp_config):
        products = [make_product(price=3.0)]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])

        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--max-price", "10", "--pages", "1", "-q",
        ])
        assert result.exit_code == 0, result.output
        assert "Deals under" not in result.output

    def test_genre_category_conflict(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--genre", "sci-fi", "--category", "123"])
        assert result.exit_code != 0
        assert "not both" in result.output

    def test_output_implies_quiet(self, mock_client, tmp_config):
        """When -o is set without -q, quiet should be implied (no table in stdout)."""
        products = [make_product(price=3.0, series_name="", series_position="")]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        out_file = tmp_config / "implied.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--max-price", "10", "--pages", "1", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        # Table header should NOT appear in console output
        assert "Deals under" not in result.output

    def test_output_explicit_no_quiet_override(self, mock_client, tmp_config):
        """Explicitly passing --no-quiet (or just not passing -q) with -o does imply quiet."""
        products = [make_product(price=3.0, series_name="", series_position="")]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        out_file = tmp_config / "noquiet.json"
        runner = CliRunner()
        # Passing -q explicitly should still suppress table
        result = runner.invoke(cli, [
            "find", "--max-price", "10", "--pages", "1", "--output", str(out_file), "-q",
        ])
        assert result.exit_code == 0, result.output
        assert "Deals under" not in result.output


class TestSearchCommand:
    def test_search_basic(self, mock_client, tmp_config):
        products = [make_product(asin="S1", price=5.0)]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])

        out_file = tmp_config / "search.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "search", "test query", "--pages", "1", "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 1
        assert data[0]["asin"] == "S1"

    def test_output_implies_quiet(self, mock_client, tmp_config):
        """When -o is set without -q, quiet should be implied (no table in stdout)."""
        products = [make_product(asin="S2", price=5.0)]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        out_file = tmp_config / "search_implied.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "search", "test", "--pages", "1", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        # Table should not appear; export message should appear
        assert 'Search: "test"' not in result.output
        assert "Exported" in result.output

    def test_output_with_explicit_quiet(self, mock_client, tmp_config):
        """Explicit -q with -o also suppresses table."""
        products = [make_product(asin="S3", price=5.0)]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        out_file = tmp_config / "search_explicit.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "search", "test", "--pages", "1", "--output", str(out_file), "-q",
        ])
        assert result.exit_code == 0, result.output
        assert 'Search: "test"' not in result.output


class TestDetailCommand:
    def test_detail_ok(self, mock_client, tmp_config):
        mock_client.get_product.return_value = make_product(asin="D1", title="Detail Test")

        runner = CliRunner()
        result = runner.invoke(cli, ["detail", "D1"])
        assert result.exit_code == 0, result.output
        assert "Detail Test" in result.output

    def test_detail_not_found(self, mock_client, tmp_config):
        mock_client.get_product.side_effect = ValueError("Product not found: BAD")

        runner = CliRunner()
        result = runner.invoke(cli, ["detail", "BAD"])
        assert result.exit_code != 0
        assert "Product not found" in result.output


class TestCompareCommand:
    def test_compare_ok(self, mock_client, tmp_config):
        mock_client.get_products_batch.return_value = [
            make_product(asin="C1", title="Book 1", price=5.0, length_minutes=600),
            make_product(asin="C2", title="Book 2", price=10.0, length_minutes=600),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["compare", "C1", "C2"])
        assert result.exit_code == 0, result.output
        assert "Book 1" in result.output
        assert "Book 2" in result.output
        assert "Best value" in result.output

    def test_compare_too_few(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["compare", "ONLY_ONE"])
        assert result.exit_code != 0
        assert "at least 2" in result.output

    def test_compare_with_missing(self, mock_client, tmp_config):
        mock_client.get_products_batch.return_value = [
            make_product(asin="C1", title="Book 1"),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["compare", "C1", "MISSING"])
        assert result.exit_code != 0
        assert "Not found: MISSING" in result.output


class TestWishlistCommands:
    def test_add_list_remove(self, mock_client, tmp_config):
        mock_client.get_product.return_value = make_product(asin="W1", title="Wish Book")

        runner = CliRunner()

        # Add
        result = runner.invoke(cli, ["wishlist", "add", "W1", "--max-price", "5"])
        assert result.exit_code == 0, result.output
        assert "Wish Book" in result.output
        assert "1 added" in result.output

        # List
        result = runner.invoke(cli, ["wishlist", "list"])
        assert result.exit_code == 0, result.output
        assert "W1" in result.output
        assert "$5.00" in result.output

        # Duplicate
        result = runner.invoke(cli, ["wishlist", "add", "W1"])
        assert "already on wishlist" in result.output

        # Remove
        result = runner.invoke(cli, ["wishlist", "remove", "W1"])
        assert result.exit_code == 0
        assert "1 removed" in result.output

        # Empty list
        result = runner.invoke(cli, ["wishlist", "list"])
        assert "empty" in result.output

    def test_add_not_found(self, mock_client, tmp_config):
        mock_client.get_product.side_effect = ValueError("not found")
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "add", "BAD"])
        assert "Not found" in result.output


class TestWishlistSyncCommand:
    def test_sync_adds_new_items(self, mock_client, tmp_config):
        """Items from Audible wishlist not in local are added."""
        mock_client.get_wishlist.return_value = [
            make_product(asin="WS1", title="Sync Book One"),
            make_product(asin="WS2", title="Sync Book Two"),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "sync"])
        assert result.exit_code == 0, result.output
        assert "Sync Book One" in result.output
        assert "Sync Book Two" in result.output
        assert "2 synced" in result.output
        assert "0 already tracked" in result.output

    def test_sync_skips_existing(self, mock_client, tmp_config):
        """Items already in local wishlist are counted as skipped, not re-added."""
        import audible_deals.cli as cli_mod
        cli_mod._save_wishlist([
            {"asin": "WS1", "title": "Already Here", "max_price": None, "added": ""},
        ])
        mock_client.get_wishlist.return_value = [
            make_product(asin="WS1", title="Already Here"),
            make_product(asin="WS2", title="New Book"),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "sync"])
        assert result.exit_code == 0, result.output
        assert "Already Here" not in result.output
        assert "New Book" in result.output
        assert "1 synced" in result.output
        assert "1 already tracked" in result.output

    def test_sync_empty_wishlist(self, mock_client, tmp_config):
        """Empty Audible wishlist syncs zero items."""
        mock_client.get_wishlist.return_value = []
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "sync"])
        assert result.exit_code == 0, result.output
        assert "0 synced" in result.output

    def test_sync_max_price_applied(self, mock_client, tmp_config):
        """--max-price sets the target price on all synced items."""
        mock_client.get_wishlist.return_value = [
            make_product(asin="WS3", title="Price Book"),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "sync", "--max-price", "7.99"])
        assert result.exit_code == 0, result.output

        # Verify the saved item has max_price set
        import audible_deals.cli as cli_mod
        items = cli_mod._load_wishlist()
        assert len(items) == 1
        assert items[0]["asin"] == "WS3"
        assert items[0]["max_price"] == 7.99

    def test_sync_persists_to_wishlist_file(self, mock_client, tmp_config):
        """Synced items are persisted so wishlist list can show them."""
        mock_client.get_wishlist.return_value = [
            make_product(asin="WS4", title="Persistent Book"),
        ]
        runner = CliRunner()
        sync_result = runner.invoke(cli, ["wishlist", "sync"])
        assert sync_result.exit_code == 0, sync_result.output

        result = runner.invoke(cli, ["wishlist", "list"])
        assert result.exit_code == 0, result.output
        assert "WS4" in result.output

    def test_sync_update_changes_existing_price(self, mock_client, tmp_config):
        """--update with --max-price updates target price for existing items."""
        import audible_deals.cli as cli_mod
        cli_mod._save_wishlist([
            {"asin": "WS1", "title": "Old Price Book", "max_price": 20.0, "added": ""},
        ])
        mock_client.get_wishlist.return_value = [
            make_product(asin="WS1", title="Old Price Book"),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "sync", "--max-price", "5", "--update"])
        assert result.exit_code == 0, result.output
        items = cli_mod._load_wishlist()
        assert items[0]["max_price"] == 5.0
        assert "1 updated" in result.output

    def test_sync_update_without_max_price_errors(self, mock_client, tmp_config):
        """--update without --max-price raises an error."""
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "sync", "--update"])
        assert result.exit_code == 2
        assert "requires --max-price" in result.output


class TestLibraryCommand:

    def test_library_basic(self, mock_client, tmp_config):
        products = [
            make_product(asin="LIB1", title="My Book One", price=10.0),
            make_product(asin="LIB2", title="My Book Two", price=15.0),
        ]
        _mock_library_pages(mock_client, products)
        out_file = tmp_config / "library.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["library", "-q", "-o", str(out_file)])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 2
        asins = {d["asin"] for d in data}
        assert asins == {"LIB1", "LIB2"}

    def test_library_json_export(self, mock_client, tmp_config):
        """--json with -o exports valid JSON to the file."""
        products = [make_product(asin="LIB3", title="JSON Book")]
        _mock_library_pages(mock_client, products)
        out_file = tmp_config / "library_json.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["library", "-q", "-o", str(out_file)])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 1
        assert data[0]["asin"] == "LIB3"
        assert data[0]["title"] == "JSON Book"

    def test_library_limit(self, mock_client, tmp_config):
        products = [make_product(asin=f"LL{i}", title=f"Book {i}") for i in range(10)]
        _mock_library_pages(mock_client, products)
        out_file = tmp_config / "library_limit.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["library", "-n", "3", "-q", "-o", str(out_file)])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 3

    def test_library_csv_export(self, mock_client, tmp_config):
        products = [make_product(asin="LCSV1", title="CSV Book")]
        _mock_library_pages(mock_client, products)
        out_file = tmp_config / "library.csv"
        runner = CliRunner()
        result = runner.invoke(cli, ["library", "-o", str(out_file)])
        assert result.exit_code == 0, result.output
        content = out_file.read_text()
        assert "LCSV1" in content
        assert "CSV Book" in content

    def test_library_empty(self, mock_client, tmp_config):
        _mock_library_pages(mock_client, [])
        runner = CliRunner()
        result = runner.invoke(cli, ["library"])
        assert result.exit_code == 0, result.output
        assert "0" in result.output


class TestLoadWishlistTypeValidation:
    def test_dict_returns_empty_list(self, tmp_config):
        """A wishlist.json containing {} instead of [] returns empty list."""
        import audible_deals.cli as cli_mod
        cli_mod.WISHLIST_FILE.write_text("{}")
        assert cli_mod._load_wishlist() == []

    def test_load_profiles_list_returns_empty_dict(self, tmp_config):
        """A profiles.json containing [] instead of {} returns empty dict."""
        import audible_deals.cli as cli_mod
        cli_mod.PROFILES_FILE.write_text("[]")
        assert cli_mod._load_profiles() == {}


class TestWatchCommand:
    def test_watch_empty(self, tmp_config, mock_client):
        runner = CliRunner()
        result = runner.invoke(cli, ["watch"])
        assert result.exit_code == 0, result.output
        assert "empty" in result.output

    def test_watch_with_items(self, mock_client, tmp_config):
        # Seed the wishlist
        import audible_deals.cli as cli_mod
        cli_mod._save_wishlist([
            {"asin": "W1", "title": "Book", "max_price": 10.0},
        ])
        mock_client.get_products_batch.return_value = [
            make_product(asin="W1", title="Book", price=5.0, list_price=20.0),
        ]

        runner = CliRunner()
        result = runner.invoke(cli, ["watch"])
        assert result.exit_code == 0, result.output
        assert "BUY" in result.output

    def test_watch_buy_only(self, mock_client, tmp_config):
        """--buy-only filters to only items at or below target."""
        import audible_deals.cli as cli_mod
        cli_mod._save_wishlist([
            {"asin": "W1", "title": "Cheap Book", "max_price": 10.0},
            {"asin": "W2", "title": "Expensive Book", "max_price": 3.0},
        ])
        mock_client.get_products_batch.return_value = [
            make_product(asin="W1", title="Cheap Book", price=5.0, list_price=20.0),
            make_product(asin="W2", title="Expensive Book", price=15.0, list_price=20.0),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["watch", "--buy-only"])
        assert result.exit_code == 0, result.output
        assert "Cheap Book" in result.output
        assert "Expensive Book" not in result.output

    def test_watch_sort_by_title(self, mock_client, tmp_config):
        """--sort title orders output alphabetically."""
        import audible_deals.cli as cli_mod
        cli_mod._save_wishlist([
            {"asin": "W1", "title": "Zebra Book", "max_price": 10.0},
            {"asin": "W2", "title": "Alpha Book", "max_price": 10.0},
        ])
        mock_client.get_products_batch.return_value = [
            make_product(asin="W1", title="Zebra Book", price=5.0),
            make_product(asin="W2", title="Alpha Book", price=5.0),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["watch", "--sort", "title"])
        assert result.exit_code == 0, result.output
        alpha_pos = result.output.index("Alpha Book")
        zebra_pos = result.output.index("Zebra Book")
        assert alpha_pos < zebra_pos

    def test_watch_show_url(self, mock_client, tmp_config):
        """--show-url adds URL column to output."""
        from io import StringIO
        from rich.console import Console
        import audible_deals.cli as cli_mod
        cli_mod._save_wishlist([
            {"asin": "W1", "title": "URL Book", "max_price": 10.0},
        ])
        mock_client.get_products_batch.return_value = [
            make_product(asin="W1", title="URL Book", price=5.0),
        ]
        # Patch the console to use a wide fixed-width instance so Rich
        # does not truncate the URL cell value in a narrow test environment
        import audible_deals.display as display_mod
        buf = StringIO()
        wide_console = Console(file=buf, width=200, highlight=False)
        original_cli = cli_mod.console
        original_display = display_mod.console
        cli_mod.console = wide_console
        display_mod.console = wide_console
        try:
            runner = CliRunner()
            result = runner.invoke(cli, ["watch", "--show-url"])
        finally:
            cli_mod.console = original_cli
            display_mod.console = original_display
        assert result.exit_code == 0, result.output
        captured = buf.getvalue()
        assert "URL" in captured
        assert "/pd/W1" in captured

    @pytest.mark.parametrize("sort_key", ["author", "asin"])
    def test_watch_sort_keys(self, mock_client, tmp_config, sort_key):
        """--sort author and --sort asin run without error."""
        import audible_deals.cli as cli_mod
        cli_mod._save_wishlist([
            {"asin": "W1", "title": "Book A", "max_price": 10.0},
            {"asin": "W2", "title": "Book B", "max_price": 10.0},
        ])
        mock_client.get_products_batch.return_value = [
            make_product(asin="W1", title="Book A", price=5.0, authors=["Zeta Author"]),
            make_product(asin="W2", title="Book B", price=5.0, authors=["Alpha Author"]),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["watch", "--sort", sort_key])
        assert result.exit_code == 0, result.output


class TestHistoryCommand:
    def test_no_history(self, tmp_config, mock_client):
        runner = CliRunner()
        result = runner.invoke(cli, ["history", "NOPE"])
        assert result.exit_code == 0, result.output
        assert "No price history" in result.output

    def test_history_after_recording(self, tmp_config, mock_client):
        from audible_deals.cli import _record_prices
        products = [make_product(asin="H1", price=5.99)]
        _record_prices(products)

        runner = CliRunner()
        result = runner.invoke(cli, ["history", "H1"])
        assert result.exit_code == 0, result.output
        assert "$5.99" in result.output

    def test_history_idempotent(self, tmp_config):
        from audible_deals.cli import _record_prices
        products = [make_product(asin="H2", price=3.00)]
        _record_prices(products)
        _record_prices(products)  # Same day

        hist_file = tmp_config / "history" / "H2.json"
        entries = json.loads(hist_file.read_text())
        assert len(entries) == 1


class TestCompletionsCommand:
    def test_completions_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["completions", "--help"])
        assert result.exit_code == 0
        assert "bash" in result.output

    def test_completions_no_shell_invocation(self, monkeypatch):
        """Verify subprocess.run is called directly, not via /bin/sh -c."""
        import subprocess as sp
        calls = []

        def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return sp.CompletedProcess(args[0], 0, stdout="# completion", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("shutil.which", lambda _: None)

        runner = CliRunner()
        result = runner.invoke(cli, ["completions", "bash"])
        assert result.exit_code == 0
        assert len(calls) == 1
        cmd = calls[0][0][0]
        assert "/bin/sh" not in cmd
        assert "env" in calls[0][1]
        assert "_DEALS_COMPLETE" in calls[0][1]["env"]


# ===================================================================
# ASIN validation in commands
# ===================================================================

class TestAsinValidationInCommands:
    def test_detail_rejects_path_traversal(self, tmp_config, mock_client):
        runner = CliRunner()
        result = runner.invoke(cli, ["detail", "../../../etc/passwd"])
        assert result.exit_code != 0
        assert "Invalid ASIN" in result.output

    def test_detail_accepts_valid_asin(self, mock_client, tmp_config):
        mock_client.get_product.return_value = make_product(asin="B00VALID")
        runner = CliRunner()
        result = runner.invoke(cli, ["detail", "B00VALID"])
        assert result.exit_code == 0

    def test_open_rejects_path_traversal(self, tmp_config, mock_client):
        runner = CliRunner()
        result = runner.invoke(cli, ["open", "../../etc/passwd"])
        assert result.exit_code != 0
        assert "Invalid ASIN" in result.output

    def test_compare_rejects_bad_asin(self, tmp_config, mock_client):
        runner = CliRunner()
        result = runner.invoke(cli, ["compare", "B00GOOD", "../bad"])
        assert result.exit_code != 0
        assert "Invalid ASIN" in result.output


# ===================================================================
# Webhook URL validation
# ===================================================================

class TestWebhookValidation:
    def test_rejects_non_http_scheme(self):
        with pytest.raises(click.BadParameter, match="http://"):
            _validate_webhook_url("ftp://example.com/hook")

    def test_rejects_no_host(self):
        with pytest.raises(click.BadParameter, match="host"):
            _validate_webhook_url("http://")

    def test_rejects_localhost(self):
        with pytest.raises(click.BadParameter, match="non-public"):
            _validate_webhook_url("http://localhost/hook")

    def test_rejects_127_0_0_1(self):
        with pytest.raises(click.BadParameter, match="non-public"):
            _validate_webhook_url("http://127.0.0.1/hook")

    def test_rejects_private_ip(self, monkeypatch):
        import socket
        monkeypatch.setattr(
            "audible_deals.utils.socket.getaddrinfo",
            lambda host, port: [(socket.AF_INET, 0, 0, "", ("10.0.0.1", 0))],
        )
        with pytest.raises(click.BadParameter, match="non-public"):
            _validate_webhook_url("https://internal.corp/hook")

    def test_rejects_link_local(self, monkeypatch):
        import socket
        monkeypatch.setattr(
            "audible_deals.utils.socket.getaddrinfo",
            lambda host, port: [(socket.AF_INET, 0, 0, "", ("169.254.169.254", 0))],
        )
        with pytest.raises(click.BadParameter, match="non-public"):
            _validate_webhook_url("https://metadata.internal/hook")

    def test_accepts_public_ip(self, monkeypatch):
        import socket
        monkeypatch.setattr(
            "audible_deals.utils.socket.getaddrinfo",
            lambda host, port: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
        )
        _validate_webhook_url("https://example.com/hook")  # should not raise

    def test_rejects_unresolvable_host(self, monkeypatch):
        import socket
        monkeypatch.setattr(
            "audible_deals.utils.socket.getaddrinfo",
            lambda host, port: (_ for _ in ()).throw(socket.gaierror("Name not resolved")),
        )
        with pytest.raises(click.BadParameter, match="Cannot resolve"):
            _validate_webhook_url("https://nonexistent.invalid/hook")


# ===================================================================
# Improvement #2: _deserialize_product, _resolve_last_references,
# deals last, --last flag
# ===================================================================

class TestDeserializeProduct:
    def test_round_trip(self):
        p = make_product(asin="RT1", price=4.99, list_price=12.99)
        d = _serialize_product(p)
        p2 = _deserialize_product(d)
        assert p2.asin == p.asin
        assert p2.price == p.price
        assert p2.title == p.title
        assert p2.authors == p.authors

    def test_extra_keys_ignored(self):
        """Extra keys from serialization (computed fields) are silently ignored."""
        p = make_product(asin="EK1")
        d = _serialize_product(p)
        # d has extra keys like full_title, hours, discount_pct, price_per_hour, url
        p2 = _deserialize_product(d)
        assert p2.asin == "EK1"

    def test_missing_optional_fields(self):
        """Minimal dict with only required fields works."""
        d = {
            "asin": "MIN1",
            "title": "Minimal",
            "subtitle": "",
            "authors": ["A"],
            "narrators": [],
            "publisher": "",
            "price": None,
            "list_price": None,
            "length_minutes": 0,
            "rating": 0.0,
            "num_ratings": 0,
            "categories": [],
            "category_ids": [],
            "series_name": "",
            "series_position": "",
            "language": "english",
            "release_date": "",
            "in_plus_catalog": False,
        }
        p = _deserialize_product(d)
        assert p.asin == "MIN1"

    def test_corrupt_dict_returns_none(self):
        """Dicts missing required fields return None instead of crashing."""
        assert _deserialize_product({}) is None
        assert _deserialize_product({"price": 5.0}) is None


class TestResolveLastReferences:
    def test_valid_reference(self, tmp_config):
        import audible_deals.cli as cli_mod
        p = make_product(asin="REF1")
        data = [_serialize_product(p)]
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps(data))
        results = _resolve_last_references((1,))
        assert len(results) == 1
        asin, desc = results[0]
        assert asin == "REF1"
        assert "REF1" in desc
        assert "Result #1" in desc

    def test_multiple_references(self, tmp_config):
        import audible_deals.cli as cli_mod
        products = [make_product(asin=f"R{i}") for i in range(1, 4)]
        data = [_serialize_product(p) for p in products]
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps(data))
        results = _resolve_last_references((1, 3))
        asins = [r[0] for r in results]
        assert asins == ["R1", "R3"]

    def test_out_of_range(self, tmp_config):
        import audible_deals.cli as cli_mod
        data = [_serialize_product(make_product(asin="ONLY1"))]
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps(data))
        with pytest.raises(click.ClickException, match="out of range"):
            _resolve_last_references((5,))

    def test_missing_file(self, tmp_config):
        with pytest.raises(click.ClickException, match="No cached results"):
            _resolve_last_references((1,))

    def test_corrupt_file(self, tmp_config):
        import audible_deals.cli as cli_mod
        cli_mod.LAST_RESULTS_FILE.write_text("not-json{{{{")
        with pytest.raises(click.ClickException, match="Could not read"):
            _resolve_last_references((1,))


class TestLastCommand:
    def _seed_cache(self, tmp_config, products):
        import audible_deals.cli as cli_mod
        data = [_serialize_product(p) for p in products]
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps(data))

    def test_no_cache(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["last"])
        assert result.exit_code != 0
        assert "No cached results" in result.output

    def test_last_basic(self, tmp_config):
        products = [
            make_product(asin="L1", price=3.0, series_name="", series_position=""),
            make_product(asin="L2", price=5.0, series_name="", series_position=""),
        ]
        self._seed_cache(tmp_config, products)
        runner = CliRunner()
        result = runner.invoke(cli, ["last"])
        assert result.exit_code == 0, result.output
        assert "Last results" in result.output

    def test_last_resort(self, tmp_config):
        """deals last --sort discount re-sorts without API call."""
        products = [
            make_product(asin="LS1", price=5.0, list_price=10.0,
                         series_name="", series_position=""),
            make_product(asin="LS2", price=3.0, list_price=3.0,
                         series_name="", series_position=""),  # 0% discount
        ]
        self._seed_cache(tmp_config, products)
        out_file = tmp_config / "last_sort.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--sort", "discount", "--output", str(out_file)])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        # LS1 has 50% discount, LS2 has 0%
        assert data[0]["asin"] == "LS1"

    def test_last_max_price_filter(self, tmp_config):
        """deals last --max-price filters the cached results."""
        products = [
            make_product(asin="LF1", price=2.0, series_name="", series_position=""),
            make_product(asin="LF2", price=8.0, series_name="", series_position=""),
        ]
        self._seed_cache(tmp_config, products)
        out_file = tmp_config / "last_filter.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--max-price", "5", "--output", str(out_file)])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LF1" in asins
        assert "LF2" not in asins

    def test_last_output_implies_quiet(self, tmp_config):
        products = [make_product(asin="LQ1", price=3.0, series_name="", series_position="")]
        self._seed_cache(tmp_config, products)
        out_file = tmp_config / "last_quiet.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--output", str(out_file)])
        assert result.exit_code == 0, result.output
        assert "Last results" not in result.output

    def test_last_does_not_overwrite_cache(self, tmp_config):
        """deals last should NOT shrink the cache when filtering."""
        products = [
            make_product(asin="NC1", price=2.0, series_name="", series_position=""),
            make_product(asin="NC2", price=8.0, series_name="", series_position=""),
        ]
        self._seed_cache(tmp_config, products)
        import audible_deals.cli as cli_mod
        original = cli_mod.LAST_RESULTS_FILE.read_text()
        runner = CliRunner()
        # Filter to only NC1 — cache should still have both
        result = runner.invoke(cli, ["last", "--max-price", "5"])
        assert result.exit_code == 0, result.output
        after = cli_mod.LAST_RESULTS_FILE.read_text()
        assert original == after


class TestDetailLastFlag:
    def test_detail_last(self, mock_client, tmp_config):
        products = [make_product(asin="DL1")]
        import audible_deals.cli as cli_mod
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps([_serialize_product(p) for p in products]))
        mock_client.get_product.return_value = make_product(asin="DL1", title="Detail Last")
        runner = CliRunner()
        result = runner.invoke(cli, ["detail", "--last", "1"])
        assert result.exit_code == 0, result.output
        mock_client.get_product.assert_called_once_with("DL1")

    def test_detail_no_asin_no_last(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["detail"])
        assert result.exit_code != 0
        assert "Provide an ASIN" in result.output


class TestCompareLastFlag:
    def test_compare_last(self, mock_client, tmp_config):
        products = [
            make_product(asin="CL1"),
            make_product(asin="CL2"),
        ]
        import audible_deals.cli as cli_mod
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps([_serialize_product(p) for p in products]))
        mock_client.get_products_batch.return_value = [
            make_product(asin="CL1", title="Book 1", price=5.0, length_minutes=600),
            make_product(asin="CL2", title="Book 2", price=8.0, length_minutes=600),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["compare", "--last", "1", "--last", "2"])
        assert result.exit_code == 0, result.output
        mock_client.get_products_batch.assert_called_once_with(["CL1", "CL2"])

    def test_compare_mixed(self, mock_client, tmp_config):
        """Mix positional ASIN with --last ref."""
        products = [make_product(asin="CM2")]
        import audible_deals.cli as cli_mod
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps([_serialize_product(p) for p in products]))
        mock_client.get_products_batch.return_value = [
            make_product(asin="CM1", title="Book 1", price=5.0, length_minutes=600),
            make_product(asin="CM2", title="Book 2", price=8.0, length_minutes=600),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["compare", "CM1", "--last", "1"])
        assert result.exit_code == 0, result.output


# ===================================================================
# Improvement #3: --skip-owned / --language / --interactive in profiles
# + --profile on search
# ===================================================================

class TestProfileSaveNewFlags:
    def test_skip_owned_in_profile(self, tmp_config):
        """profile save accepts --skip-owned and persists it."""
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "save", "myprofile", "--skip-owned"])
        assert result.exit_code == 0, result.output
        import audible_deals.cli as cli_mod
        profiles = cli_mod._load_profiles()
        assert profiles["myprofile"]["skip_owned"] is True

    def test_language_in_profile(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "save", "langprofile", "--language", "french"])
        assert result.exit_code == 0, result.output
        import audible_deals.cli as cli_mod
        profiles = cli_mod._load_profiles()
        assert profiles["langprofile"]["language"] == "french"

    def test_interactive_in_profile(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "save", "iprofile", "--interactive"])
        assert result.exit_code == 0, result.output
        import audible_deals.cli as cli_mod
        profiles = cli_mod._load_profiles()
        assert profiles["iprofile"]["interactive"] is True


class TestFindProfileSkipOwned:
    def test_find_profile_skip_owned(self, mock_client, tmp_config):
        """find --profile loads skip_owned from profile and calls get_library_asins."""
        import audible_deals.cli as cli_mod
        cli_mod._save_profiles({"myp": {"skip_owned": True}})
        mock_client.search_pages.return_value = iter([([], 1, 0)])
        mock_client.get_library_asins.return_value = set()

        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--profile", "myp", "--pages", "1"])
        assert result.exit_code == 0, result.output
        mock_client.get_library_asins.assert_called_once()

    def test_find_backward_compat(self, mock_client, tmp_config):
        """Old profiles without new keys still work fine."""
        import audible_deals.cli as cli_mod
        cli_mod._save_profiles({"oldp": {"max_price": 5.0}})
        mock_client.search_pages.return_value = iter([([], 1, 0)])

        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--profile", "oldp", "--pages", "1"])
        assert result.exit_code == 0, result.output


class TestSearchWithProfile:
    def test_search_profile_applies_settings(self, mock_client, tmp_config):
        """search --profile X applies profile settings."""
        import audible_deals.cli as cli_mod
        cli_mod._save_profiles({"stest": {"min_rating": 4.5}})
        products = [
            make_product(asin="SP1", price=5.0, rating=4.8),
            make_product(asin="SP2", price=5.0, rating=3.0),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "search_profile.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "search", "test", "--profile", "stest", "--pages", "1",
            "--all-languages", "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "SP1" in asins
        assert "SP2" not in asins

    def test_search_profile_not_found(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "test", "--profile", "noexist"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_search_profile_skip_owned(self, mock_client, tmp_config):
        """search --profile with skip_owned calls get_library_asins."""
        import audible_deals.cli as cli_mod
        cli_mod._save_profiles({"owned_profile": {"skip_owned": True}})
        mock_client.search_pages.return_value = iter([([], 1, 0)])
        mock_client.get_library_asins.return_value = set()

        runner = CliRunner()
        result = runner.invoke(cli, [
            "search", "test", "--profile", "owned_profile", "--pages", "1",
        ])
        assert result.exit_code == 0, result.output
        mock_client.get_library_asins.assert_called_once()


# ===================================================================
# Improvement #4: Global defaults config
# ===================================================================

class TestConfigCommands:
    def test_set_and_get(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "max-price", "5.0"])
        assert result.exit_code == 0, result.output
        assert "max_price" in result.output

        result = runner.invoke(cli, ["config", "get", "max-price"])
        assert result.exit_code == 0, result.output
        assert "5.0" in result.output

    def test_set_bool(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "skip-owned", "true"])
        assert result.exit_code == 0, result.output
        import audible_deals.cli as cli_mod
        cfg = cli_mod._load_config()
        assert cfg["skip_owned"] is True

    def test_set_invalid_bool(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "skip-owned", "maybe"])
        assert result.exit_code != 0
        assert "Invalid boolean" in result.output

    def test_set_invalid_key(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "nonexistent-key", "val"])
        assert result.exit_code != 0
        assert "Unknown config key" in result.output

    def test_list_empty(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "list"])
        assert result.exit_code == 0, result.output
        assert "No global defaults" in result.output

    def test_list_with_values(self, tmp_config):
        import audible_deals.cli as cli_mod
        cli_mod._save_config({"max_price": 5.0, "skip_owned": True})
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "list"])
        assert result.exit_code == 0, result.output
        assert "max_price" in result.output
        assert "skip_owned" in result.output

    def test_reset_key(self, tmp_config):
        import audible_deals.cli as cli_mod
        cli_mod._save_config({"max_price": 5.0, "min_rating": 4.0})
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "reset", "max-price"])
        assert result.exit_code == 0, result.output
        cfg = cli_mod._load_config()
        assert "max_price" not in cfg
        assert "min_rating" in cfg

    def test_reset_all(self, tmp_config):
        import audible_deals.cli as cli_mod
        cli_mod._save_config({"max_price": 5.0, "min_rating": 4.0})
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "reset"], input="y\n")
        assert result.exit_code == 0, result.output
        cfg = cli_mod._load_config()
        assert cfg == {}

    def test_reset_invalid_key(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "reset", "bad-key"])
        assert result.exit_code != 0
        assert "Unknown config key" in result.output

    def test_type_coercion_int(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "pages", "5"])
        assert result.exit_code == 0, result.output
        import audible_deals.cli as cli_mod
        cfg = cli_mod._load_config()
        assert cfg["pages"] == 5
        assert isinstance(cfg["pages"], int)

    def test_type_coercion_float(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "min-rating", "4.5"])
        assert result.exit_code == 0, result.output
        import audible_deals.cli as cli_mod
        cfg = cli_mod._load_config()
        assert cfg["min_rating"] == 4.5


class TestConfigAppliedToFind:
    def test_config_max_price_applies(self, mock_client, tmp_config):
        """Config max_price is applied when not passed on CLI."""
        import audible_deals.cli as cli_mod
        cli_mod._save_config({"max_price": 3.0})
        products = [
            make_product(asin="CF1", price=2.0, series_name="", series_position=""),
            make_product(asin="CF2", price=6.0, series_name="", series_position=""),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "cfg_find.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--pages", "1", "--all-languages", "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "CF1" in asins
        assert "CF2" not in asins

    def test_cli_flag_overrides_config(self, mock_client, tmp_config):
        """CLI --max-price overrides config max_price."""
        import audible_deals.cli as cli_mod
        cli_mod._save_config({"max_price": 2.0})
        products = [
            make_product(asin="CO1", price=4.0, series_name="", series_position=""),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        out_file = tmp_config / "cfg_override.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--max-price", "10", "--pages", "1",
            "--all-languages", "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        # With config max_price=2, CO1 at 4.0 would be excluded. CLI overrides to 10, so included.
        asins = [d["asin"] for d in data]
        assert "CO1" in asins

    def test_profile_overrides_config(self, mock_client, tmp_config):
        """Profile min_rating overrides config min_rating."""
        import audible_deals.cli as cli_mod
        cli_mod._save_config({"min_rating": 4.0})
        cli_mod._save_profiles({"p": {"min_rating": 3.0}})
        products = [
            make_product(asin="PO1", price=3.0, rating=3.5, series_name="", series_position=""),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        out_file = tmp_config / "cfg_prof.json"
        runner = CliRunner()
        # With config only, PO1 (3.5) would be excluded. Profile sets 3.0, so included.
        result = runner.invoke(cli, [
            "find", "--profile", "p", "--pages", "1",
            "--all-languages", "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "PO1" in asins


class TestConfigBooleanOverride:
    def test_config_bool_not_overridden_when_cli_explicit(self):
        """Config booleans must not override when the user explicitly passed the flag."""
        from unittest.mock import MagicMock
        from audible_deals.cli import _apply_config_defaults, _CL

        ctx = MagicMock()
        ctx.get_parameter_source.return_value = _CL  # Simulate CLI explicit
        ns = {"on_sale": False, "deep": False}
        cfg = {"on_sale": True, "deep": True}
        _apply_config_defaults(ctx, ns, cfg)
        assert ns["on_sale"] is False
        assert ns["deep"] is False

    def test_config_bool_applied_when_not_cli(self):
        """Config booleans should apply when user did NOT pass the flag."""
        from unittest.mock import MagicMock
        import click
        from audible_deals.cli import _apply_config_defaults

        ctx = MagicMock()
        ctx.get_parameter_source.return_value = click.core.ParameterSource.DEFAULT
        ns = {"on_sale": False, "deep": False}
        cfg = {"on_sale": True, "deep": True}
        _apply_config_defaults(ctx, ns, cfg)
        assert ns["on_sale"] is True
        assert ns["deep"] is True

    def test_config_bool_false_applied_when_not_cli(self):
        """Config with explicit False should set ns to False when source is DEFAULT."""
        from unittest.mock import MagicMock
        import click
        from audible_deals.cli import _apply_config_defaults

        ctx = MagicMock()
        ctx.get_parameter_source.return_value = click.core.ParameterSource.DEFAULT
        ns = {"on_sale": True, "deep": True}
        cfg = {"on_sale": False, "deep": False}
        _apply_config_defaults(ctx, ns, cfg)
        assert ns["on_sale"] is False
        assert ns["deep"] is False

    def test_profile_bool_not_overridden_when_cli_explicit(self):
        """Profile booleans must not override when the user explicitly passed the flag."""
        from unittest.mock import MagicMock
        from audible_deals.cli import _apply_profile_defaults, _CL

        ctx = MagicMock()
        ctx.get_parameter_source.return_value = _CL
        ns = {"on_sale": False, "deep": False}
        profile = {"on_sale": True, "deep": True}
        _apply_profile_defaults(ctx, ns, profile)
        assert ns["on_sale"] is False
        assert ns["deep"] is False

    def test_profile_bool_applied_when_not_cli(self):
        """Profile booleans should apply when user did NOT pass the flag."""
        from unittest.mock import MagicMock
        import click
        from audible_deals.cli import _apply_profile_defaults

        ctx = MagicMock()
        ctx.get_parameter_source.return_value = click.core.ParameterSource.DEFAULT
        ns = {"on_sale": False, "deep": False}
        profile = {"on_sale": True, "deep": True}
        _apply_profile_defaults(ctx, ns, profile)
        assert ns["on_sale"] is True
        assert ns["deep"] is True


# ===================================================================
# Improvement #5: --deep for search + _fetch_with_progress helper
# ===================================================================

class TestFetchWithProgress:
    def test_single_sort_no_dedup(self, mock_client, tmp_config):
        """Single sort order returns all products."""
        products = [
            make_product(asin="FP1"),
            make_product(asin="FP2"),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])

        result = _fetch_with_progress(
            mock_client,
            keywords="",
            category_id="",
            sort_orders=["BestSellers"],
            pages=1,
            description="Test",
        )
        assert {p.asin for p in result} == {"FP1", "FP2"}

    def test_multi_sort_deduplicates(self, mock_client, tmp_config):
        """Multiple sort orders deduplicate overlapping ASINs."""
        pass1 = [make_product(asin="MD1"), make_product(asin="MD2")]
        pass2 = [make_product(asin="MD2"), make_product(asin="MD3")]  # MD2 overlaps

        call_count = 0
        def fake_search_pages(**kwargs):
            nonlocal call_count
            data = [pass1, pass2][call_count]
            call_count += 1
            yield data, 1, len(data)

        mock_client.search_pages.side_effect = fake_search_pages

        result = _fetch_with_progress(
            mock_client,
            keywords="",
            category_id="",
            sort_orders=["BestSellers", "AvgRating"],
            pages=1,
            description="Test",
        )
        asins = [p.asin for p in result]
        assert sorted(asins) == ["MD1", "MD2", "MD3"]


class TestSearchDeepFlag:
    def test_search_deep_flag_exists(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "--help"])
        assert result.exit_code == 0
        assert "--deep" in result.output

    def test_search_deep_deduplicates(self, mock_client, tmp_config):
        """search --deep fetches 3 sort orders and deduplicates."""
        pass1 = [make_product(asin="SD1", price=3.0, series_name="", series_position=""),
                 make_product(asin="SD2", price=4.0, series_name="", series_position="")]
        pass2 = [make_product(asin="SD2", price=4.0, series_name="", series_position=""),
                 make_product(asin="SD3", price=5.0, series_name="", series_position="")]
        pass3 = [make_product(asin="SD1", price=3.0, series_name="", series_position=""),
                 make_product(asin="SD4", price=2.0, series_name="", series_position="")]

        call_count = 0
        def fake_search_pages(**kwargs):
            nonlocal call_count
            data = [pass1, pass2, pass3][call_count]
            call_count += 1
            yield data, 1, len(data)

        mock_client.search_pages.side_effect = fake_search_pages
        out_file = tmp_config / "search_deep.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "search", "test", "--deep", "--pages", "1", "--all-languages",
            "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = sorted(d["asin"] for d in data)
        assert asins == ["SD1", "SD2", "SD3", "SD4"]


# ===================================================================
# Improvement #6: --exclude-author flag
# ===================================================================

class TestExcludeAuthorFilter:
    def test_exclude_author_single(self):
        """_filter_products excludes products whose authors match the exclude substring."""
        products = [
            make_product(asin="EA1", authors=["Andy Weir"]),
            make_product(asin="EA2", authors=["Brandon Sanderson"]),
        ]
        filtered, breakdown = _filter_products(products, exclude_authors=("Andy Weir",))
        asins = [p.asin for p in filtered]
        assert "EA1" not in asins
        assert "EA2" in asins
        assert breakdown == {"excluded authors": 1}

    def test_exclude_author_multiple(self):
        """Multiple --exclude-author values are all applied."""
        products = [
            make_product(asin="EAM1", authors=["Andy Weir"]),
            make_product(asin="EAM2", authors=["Brandon Sanderson"]),
            make_product(asin="EAM3", authors=["Terry Pratchett"]),
        ]
        filtered, breakdown = _filter_products(
            products, exclude_authors=("andy", "sanderson")
        )
        asins = [p.asin for p in filtered]
        assert "EAM1" not in asins
        assert "EAM2" not in asins
        assert "EAM3" in asins
        assert breakdown == {"excluded authors": 2}

    def test_exclude_author_case_insensitive(self):
        products = [make_product(asin="EAC1", authors=["Andy Weir"])]
        filtered, _ = _filter_products(products, exclude_authors=("ANDY WEIR",))
        assert len(filtered) == 0

    def test_exclude_author_empty_tuple_no_filter(self):
        products = [make_product(asin="EAE1", authors=["Anyone"])]
        filtered, breakdown = _filter_products(products, exclude_authors=())
        assert len(filtered) == 1
        assert breakdown == {}

    def test_find_exclude_author_flag(self, mock_client, tmp_config):
        """deals find --exclude-author filters out matching authors."""
        products = [
            make_product(asin="FEA1", price=3.0, authors=["Andy Weir"],
                         series_name="", series_position=""),
            make_product(asin="FEA2", price=4.0, authors=["Brandon Sanderson"],
                         series_name="", series_position=""),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "find_excl_author.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--max-price", "10", "--pages", "1",
            "--exclude-author", "andy weir",
            "--all-languages", "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "FEA1" not in asins
        assert "FEA2" in asins

    def test_last_exclude_author_flag(self, tmp_config):
        """deals last --exclude-author filters from cache."""
        import audible_deals.cli as cli_mod
        products = [
            make_product(asin="LEA1", price=3.0, authors=["Andy Weir"],
                         series_name="", series_position=""),
            make_product(asin="LEA2", price=4.0, authors=["Brandon Sanderson"],
                         series_name="", series_position=""),
        ]
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps([_serialize_product(p) for p in products]))
        out_file = tmp_config / "last_excl_author.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "last", "--exclude-author", "weir", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LEA1" not in asins
        assert "LEA2" in asins

    def test_exclude_author_in_profile(self, tmp_config):
        """profile save --exclude-author persists the exclusion."""
        runner = CliRunner()
        result = runner.invoke(cli, [
            "profile", "save", "no-weir",
            "--exclude-author", "Andy Weir",
            "--exclude-author", "Brandon Sanderson",
        ])
        assert result.exit_code == 0, result.output
        import audible_deals.cli as cli_mod
        profiles = cli_mod._load_profiles()
        assert "no-weir" in profiles
        excluded = profiles["no-weir"]["exclude_authors"]
        assert "Andy Weir" in excluded
        assert "Brandon Sanderson" in excluded

    def test_find_profile_exclude_author_applied(self, mock_client, tmp_config):
        """find --profile with exclude_authors actually filters out the author."""
        import audible_deals.cli as cli_mod
        cli_mod._save_profiles({"no-weir": {"exclude_authors": ["Andy Weir"]}})
        products = [
            make_product(asin="EA1", price=3.0, authors=["Andy Weir"], series_name="", series_position=""),
            make_product(asin="EA2", price=3.0, authors=["Pierce Brown"], series_name="", series_position=""),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "profile_excl.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--profile", "no-weir", "--pages", "1",
            "--all-languages", "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "EA1" not in asins  # Andy Weir excluded
        assert "EA2" in asins      # Pierce Brown kept


# ===================================================================
# Improvement #5: --author filter
# ===================================================================

class TestAuthorFilter:
    def test_author_substring_match(self):
        """_filter_products filters by author substring (case-insensitive)."""
        products = [
            make_product(asin="A1", authors=["Andy Weir"]),
            make_product(asin="A2", authors=["Brandon Sanderson"]),
            make_product(asin="A3", authors=["andy waters"]),  # Different "andy"
        ]
        filtered, breakdown = _filter_products(products, author="andy")
        asins = [p.asin for p in filtered]
        assert "A1" in asins
        assert "A3" in asins
        assert "A2" not in asins
        assert breakdown == {"author": 1}

    def test_author_case_insensitive(self):
        products = [make_product(asin="CI1", authors=["Andy Weir"])]
        filtered, _ = _filter_products(products, author="ANDY WEIR")
        assert len(filtered) == 1

    def test_author_no_match(self):
        products = [make_product(asin="NM1", authors=["Brandon Sanderson"])]
        filtered, _ = _filter_products(products, author="tolkien")
        assert len(filtered) == 0

    def test_author_empty_string_no_filter(self):
        products = [make_product(asin="EF1", authors=["Anyone"])]
        filtered, breakdown = _filter_products(products, author="")
        assert len(filtered) == 1
        assert breakdown == {}

    def test_find_author_flag(self, mock_client, tmp_config):
        """deals find --author filters by author name."""
        products = [
            make_product(asin="FA1", price=3.0, authors=["Andy Weir"],
                         series_name="", series_position=""),
            make_product(asin="FA2", price=4.0, authors=["Brandon Sanderson"],
                         series_name="", series_position=""),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "find_author.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--max-price", "10", "--pages", "1", "--author", "weir",
            "--all-languages", "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "FA1" in asins
        assert "FA2" not in asins

    def test_last_author_filter(self, tmp_config):
        """deals last --author filters by author."""
        import audible_deals.cli as cli_mod
        products = [
            make_product(asin="LA1", price=3.0, authors=["Andy Weir"],
                         series_name="", series_position=""),
            make_product(asin="LA2", price=4.0, authors=["Brandon Sanderson"],
                         series_name="", series_position=""),
        ]
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps([_serialize_product(p) for p in products]))
        out_file = tmp_config / "last_author.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--author", "weir", "--output", str(out_file)])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LA1" in asins
        assert "LA2" not in asins

    def test_author_in_profile(self, tmp_config):
        """profile save --author persists the author filter."""
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "save", "weir-profile", "--author", "Andy Weir"])
        assert result.exit_code == 0, result.output
        import audible_deals.cli as cli_mod
        profiles = cli_mod._load_profiles()
        assert profiles["weir-profile"]["author"] == "Andy Weir"

    def test_author_in_config(self, tmp_config):
        """config set author saves and retrieves the author filter."""
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "author", "Andy Weir"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(cli, ["config", "get", "author"])
        assert result.exit_code == 0, result.output
        assert "Andy Weir" in result.output


# ===================================================================
# Improvement #4: deals last shows original query context
# ===================================================================

class TestLastQueryContext:
    def test_new_cache_format_stores_title(self, mock_client, tmp_config):
        """find writes new-format cache with title and results."""
        products = [make_product(asin="QC1", price=3.0, series_name="", series_position="")]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--max-price", "10", "--pages", "1", "-q",
        ])
        assert result.exit_code == 0, result.output
        import audible_deals.cli as cli_mod
        raw = json.loads(cli_mod.LAST_RESULTS_FILE.read_text())
        assert isinstance(raw, dict)
        assert "title" in raw
        assert "results" in raw
        assert isinstance(raw["results"], list)
        assert raw["title"] != ""

    def test_last_shows_original_title(self, mock_client, tmp_config):
        """deals last shows the title from the cached query."""
        import audible_deals.cli as cli_mod
        products = [make_product(asin="QT1", price=3.0, series_name="", series_position="")]
        cache_obj = {
            "title": "Deals under $5.00 in Sci-Fi",
            "results": [_serialize_product(p) for p in products],
        }
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))
        runner = CliRunner()
        result = runner.invoke(cli, ["last"])
        assert result.exit_code == 0, result.output
        assert "Deals under $5.00 in Sci-Fi" in result.output

    def test_backward_compat_plain_list(self, tmp_config):
        """deals last handles old plain-list cache format gracefully."""
        import audible_deals.cli as cli_mod
        products = [make_product(asin="BC1", price=3.0, series_name="", series_position="")]
        # Old format: plain list
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps([_serialize_product(p) for p in products]))
        runner = CliRunner()
        result = runner.invoke(cli, ["last"])
        assert result.exit_code == 0, result.output
        assert "Last results" in result.output

    def test_corrupt_cache_raises(self, tmp_config):
        """deals last raises ClickException for a corrupt (non-list, non-dict) cache."""
        import audible_deals.cli as cli_mod
        cli_mod.LAST_RESULTS_FILE.write_text('"just a string"')
        runner = CliRunner()
        result = runner.invoke(cli, ["last"])
        assert result.exit_code != 0
        assert "corrupt" in result.output.lower()

    def test_resolve_last_refs_with_new_format(self, tmp_config):
        """_resolve_last_references works with new cache format."""
        import audible_deals.cli as cli_mod
        p = make_product(asin="NF1")
        cache_obj = {"title": "Test", "results": [_serialize_product(p)]}
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))
        results = _resolve_last_references((1,))
        asin, desc = results[0]
        assert asin == "NF1"
        assert "NF1" in desc
        assert "Test" in desc


# ===================================================================
# Improvement #3: Missing filters on deals last
# ===================================================================

class TestLastFilters:
    def _seed_cache(self, tmp_config, products):
        import audible_deals.cli as cli_mod
        data = [_serialize_product(p) for p in products]
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps(data))

    def test_last_narrator_filter(self, tmp_config):
        """deals last --narrator filters by narrator substring match."""
        products = [
            make_product(asin="LN1", price=3.0, narrators=["R.C. Bray"],
                         series_name="", series_position=""),
            make_product(asin="LN2", price=4.0, narrators=["Scott Brick"],
                         series_name="", series_position=""),
        ]
        self._seed_cache(tmp_config, products)
        out_file = tmp_config / "last_narrator.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--narrator", "bray", "--output", str(out_file)])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LN1" in asins
        assert "LN2" not in asins

    def test_last_min_ratings_filter(self, tmp_config):
        """deals last --min-ratings filters by number of ratings."""
        products = [
            make_product(asin="LR1", price=3.0, num_ratings=500,
                         series_name="", series_position=""),
            make_product(asin="LR2", price=4.0, num_ratings=50,
                         series_name="", series_position=""),
        ]
        self._seed_cache(tmp_config, products)
        out_file = tmp_config / "last_ratings.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--min-ratings", "100", "--output", str(out_file)])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LR1" in asins
        assert "LR2" not in asins

    def test_last_language_filter(self, tmp_config):
        """deals last --language filters by language."""
        products = [
            make_product(asin="LL1", price=3.0, language="english",
                         series_name="", series_position=""),
            make_product(asin="LL2", price=4.0, language="french",
                         series_name="", series_position=""),
        ]
        self._seed_cache(tmp_config, products)
        out_file = tmp_config / "last_lang.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--language", "english", "--output", str(out_file)])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LL1" in asins
        assert "LL2" not in asins

    def test_last_help_shows_new_flags(self):
        """deals last --help should show new filter flags."""
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--help"])
        assert result.exit_code == 0
        assert "--min-ratings" in result.output
        assert "--narrator" in result.output
        assert "--language" in result.output


# ===================================================================
# Improvement #2: Recap shows titles
# ===================================================================

class TestRecapWithTitles:
    def _write_history(self, tmp_config, asin: str, entries: list[dict]) -> None:
        import audible_deals.cli as cli_mod
        hist_dir = cli_mod.HISTORY_DIR
        hist_dir.mkdir(parents=True, exist_ok=True)
        (hist_dir / f"{asin}.json").write_text(json.dumps(entries))

    def test_recap_shows_title_in_price_drop(self, tmp_config):
        """recap displays the book title alongside the ASIN for price drops."""
        import datetime
        today = datetime.date.today().isoformat()
        old_date = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        self._write_history(tmp_config, "DROPTITLE", [
            {"date": old_date, "price": 12.00, "title": "The Drop Book"},
            {"date": today, "price": 4.00, "title": "The Drop Book"},
        ])
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "7", "--show-new"])
        assert result.exit_code == 0, result.output
        assert "The Drop Book" in result.output
        assert "DROPTITLE" in result.output

    def test_recap_fallback_no_title(self, tmp_config):
        """recap gracefully shows just the ASIN when history entries lack a title."""
        import datetime
        today = datetime.date.today().isoformat()
        old_date = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        # Old-format entries without "title" key
        self._write_history(tmp_config, "NOTITLE1", [
            {"date": old_date, "price": 10.00},
            {"date": today, "price": 3.00},
        ])
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "7", "--show-new"])
        assert result.exit_code == 0, result.output
        assert "NOTITLE1" in result.output

    def test_recap_title_stored_in_record_prices(self, tmp_config):
        """_record_prices stores the title in history entries."""
        from audible_deals.cli import _record_prices
        p = make_product(asin="RC01", price=5.99, title="My Title Book")
        _record_prices([p])

        import audible_deals.cli as cli_mod
        hist_file = cli_mod.HISTORY_DIR / "RC01.json"
        entries = json.loads(hist_file.read_text())
        assert len(entries) == 1
        assert entries[0]["title"] == "My Title Book"

    def test_recap_shows_title_for_new_items(self, tmp_config):
        """recap displays title for newly tracked items when --show-new is passed."""
        import datetime
        today = datetime.date.today().isoformat()
        self._write_history(tmp_config, "NEWBOOK1", [
            {"date": today, "price": 4.99, "title": "Brand New Book"},
        ])
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "7", "--show-new"])
        assert result.exit_code == 0, result.output
        assert "Brand New Book" in result.output
        assert "NEWBOOK1" in result.output

    def test_recap_new_items_count_without_show_new(self, tmp_config):
        """recap shows count but not details for new items when --show-new is omitted."""
        import datetime
        today = datetime.date.today().isoformat()
        self._write_history(tmp_config, "NEWBOOK2", [
            {"date": today, "price": 4.99, "title": "Hidden New Book"},
        ])
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "7"])
        assert result.exit_code == 0, result.output
        assert "Newly tracked: 1" in result.output
        assert "Hidden New Book" not in result.output

    def test_recap_stable_price_not_classified_as_new(self, tmp_config):
        """Items with 2+ entries and no price drop should not appear as newly tracked."""
        import datetime
        today = datetime.date.today().isoformat()
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        self._write_history(tmp_config, "STABLE01", [
            {"date": yesterday, "price": 5.99, "title": "Stable Book"},
            {"date": today, "price": 5.99, "title": "Stable Book"},
        ])
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "7", "--show-new"])
        assert result.exit_code == 0, result.output
        assert "Stable Book" not in result.output
        assert "STABLE01" not in result.output

    def test_recap_price_increase_not_classified_as_new(self, tmp_config):
        """Items with 2+ entries and a price increase should not appear as newly tracked."""
        import datetime
        today = datetime.date.today().isoformat()
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        self._write_history(tmp_config, "PRICEUP1", [
            {"date": yesterday, "price": 5.00, "title": "Price Up Book"},
            {"date": today, "price": 10.00, "title": "Price Up Book"},
        ])
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "7", "--show-new"])
        assert result.exit_code == 0, result.output
        assert "Price Up Book" not in result.output
        assert "PRICEUP1" not in result.output


# ===================================================================
# _parse_interval + watch --every
# ===================================================================

class TestParseInterval:
    def test_minutes(self):
        assert _parse_interval("30m") == 1800

    def test_hours(self):
        assert _parse_interval("2h") == 7200

    def test_combined(self):
        assert _parse_interval("1h30m") == 5400

    def test_seconds(self):
        assert _parse_interval("90s") == 90

    def test_plain_number_treated_as_minutes(self):
        assert _parse_interval("5") == 300

    def test_invalid_raises(self):
        with pytest.raises(click.BadParameter, match="Cannot parse"):
            _parse_interval("abc")

    def test_whitespace_stripped(self):
        assert _parse_interval("  30m  ") == 1800

    def test_zero_raises(self):
        with pytest.raises(click.BadParameter, match="positive"):
            _parse_interval("0")

    def test_zero_minutes_raises(self):
        with pytest.raises(click.BadParameter, match="positive"):
            _parse_interval("0m")

    def test_negative_raises(self):
        with pytest.raises(click.BadParameter, match="Cannot parse"):
            _parse_interval("-5m")

    def test_trailing_garbage_raises(self):
        with pytest.raises(click.BadParameter, match="Cannot parse"):
            _parse_interval("10h15x")


class TestWatchEveryFlag:
    def test_watch_help_shows_every(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["watch", "--help"])
        assert "--every" in result.output

    def test_watch_without_every_runs_once(self, mock_client, tmp_config):
        """watch without --every does a single check and exits."""
        import audible_deals.cli as cli_mod
        cli_mod._save_wishlist([{"asin": "W1", "title": "Test", "max_price": 10.0, "added": ""}])
        mock_client.get_products_batch.return_value = [
            make_product(asin="W1", price=5.0, title="Test"),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["watch"])
        assert result.exit_code == 0, result.output
        assert "BUY" in result.output


# ===================================================================
# Change #1: --version flag
# ===================================================================

class TestVersionFlag:
    def test_version_flag(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "deals" in result.output
        # version string should be present (e.g. "deals, version X.Y.Z")
        assert "version" in result.output.lower()


# ===================================================================
# Change #2: bare invocation shows help + hint
# ===================================================================

class TestBareInvocation:
    def test_bare_invocation_exits_zero(self):
        runner = CliRunner()
        result = runner.invoke(cli, [])
        assert result.exit_code == 0

    def test_bare_invocation_shows_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, [])
        assert "find" in result.output
        assert "search" in result.output

    def test_bare_invocation_shows_hint(self):
        runner = CliRunner()
        result = runner.invoke(cli, [])
        assert "Quick start" in result.output


# ===================================================================
# Change #3: find title includes genre/category name
# ===================================================================

class TestFindTitleIncludesGenre:
    def test_find_title_with_genre(self, mock_client, tmp_config):
        """find --genre shows category name in the table title."""
        products = [make_product(asin="GT1", price=3.0, series_name="", series_position="")]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        mock_client.resolve_genre.return_value = ("cat42", "Science Fiction")

        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--genre", "sci-fi", "--max-price", "10", "--pages", "1",
            "--all-languages", "-n", "0",
        ])
        assert result.exit_code == 0, result.output
        assert "Science Fiction" in result.output

    def test_find_title_without_genre(self, mock_client, tmp_config):
        """find without --genre does not include a category in title."""
        products = [make_product(asin="NT1", price=3.0, series_name="", series_position="")]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])

        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--max-price", "10", "--pages", "1", "--all-languages", "-n", "0",
        ])
        assert result.exit_code == 0, result.output
        assert "Deals under $10.00" in result.output


# ===================================================================
# Change #4: --clear flag on last command
# ===================================================================

class TestLastClearFlag:
    def test_clear_existing_cache(self, tmp_config):
        import audible_deals.cli as cli_mod
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps([]))
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--clear"])
        assert result.exit_code == 0
        assert "cleared" in result.output.lower()
        assert not cli_mod.LAST_RESULTS_FILE.exists()

    def test_clear_no_cache(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--clear"])
        assert result.exit_code == 0
        assert "No cached results" in result.output

    def test_clear_exits_without_display(self, tmp_config):
        """--clear should not attempt to read or display any results."""
        import audible_deals.cli as cli_mod
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps([]))
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--clear"])
        assert result.exit_code == 0
        # Should show clear confirmation, not a product table
        assert "cleared" in result.output.lower()
        assert "deals found" not in result.output


# ===================================================================
# Change #6: find default limit=25 and -n 0 means unlimited
# ===================================================================

class TestFindDefaultLimit:
    def test_find_default_limit_25(self, mock_client, tmp_config):
        """find without --limit defaults to 25 results."""
        products = [
            make_product(asin=f"DL{i:02d}", price=float(i), series_name="", series_position="",
                         num_ratings=10)
            for i in range(1, 36)
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 35)])
        out_file = tmp_config / "default_limit.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--max-price", "100", "--pages", "1",
            "--all-languages", "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 25

    def test_find_limit_zero_means_unlimited(self, mock_client, tmp_config):
        """find -n 0 shows all results (unlimited)."""
        products = [
            make_product(asin=f"UL{i:02d}", price=float(i), series_name="", series_position="",
                         num_ratings=10)
            for i in range(1, 36)
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 35)])
        out_file = tmp_config / "unlimited.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--max-price", "100", "--pages", "1", "-n", "0",
            "--all-languages", "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 35

    def test_search_default_limit_25(self, mock_client, tmp_config):
        """search defaults to limit=25 (same as find)."""
        products = [
            make_product(asin=f"SL{i:02d}", price=float(i), series_name="", series_position="")
            for i in range(1, 36)
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 35)])
        out_file = tmp_config / "search_default_limit.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "search", "test", "--pages", "1",
            "--all-languages", "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 25

    def test_search_limit_zero_means_unlimited(self, mock_client, tmp_config):
        """search -n 0 shows all results (unlimited)."""
        products = [
            make_product(asin=f"SL{i:02d}", price=float(i), series_name="", series_position="")
            for i in range(1, 36)
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 35)])
        out_file = tmp_config / "search_unlimited.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "search", "test", "--pages", "1", "-n", "0",
            "--all-languages", "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 35


# ===================================================================
# Change #7: find default sort is price-per-hour, min-ratings=1
# ===================================================================

class TestFindDefaults:
    def test_find_default_sort_price_per_hour(self, mock_client, tmp_config):
        """find without --sort uses price-per-hour ordering."""
        products = [
            # A: $10 / 2hrs = $5/hr
            make_product(asin="PPH_A", price=10.0, length_minutes=120,
                         series_name="", series_position="", num_ratings=10),
            # B: $3 / 10hrs = $0.30/hr (better value)
            make_product(asin="PPH_B", price=3.0, length_minutes=600,
                         series_name="", series_position="", num_ratings=10),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "pph_sort.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--max-price", "100", "--pages", "1", "-n", "0",
            "--all-languages", "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        # PPH_B has lower price-per-hour and should appear first
        assert data[0]["asin"] == "PPH_B"
        assert data[1]["asin"] == "PPH_A"

    def test_find_default_min_ratings_filters_unreviewed(self, mock_client, tmp_config):
        """find with default min-ratings=1 filters out items with 0 ratings."""
        products = [
            make_product(asin="MR1", price=3.0, num_ratings=0,
                         series_name="", series_position=""),
            make_product(asin="MR2", price=3.0, num_ratings=5,
                         series_name="", series_position=""),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "min_ratings.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--max-price", "10", "--pages", "1", "-n", "0",
            "--all-languages", "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "MR1" not in asins
        assert "MR2" in asins


# ===================================================================
# Change #8: --exclude-narrator flag
# ===================================================================

class TestExcludeNarratorFilter:
    def test_exclude_narrator_single(self):
        products = [
            make_product(asin="EN1", narrators=["R.C. Bray"]),
            make_product(asin="EN2", narrators=["Scott Brick"]),
        ]
        filtered, breakdown = _filter_products(products, exclude_narrators=("R.C. Bray",))
        asins = [p.asin for p in filtered]
        assert "EN1" not in asins
        assert "EN2" in asins
        assert breakdown == {"excluded narrators": 1}

    def test_exclude_narrator_substring(self):
        products = [
            make_product(asin="ENS1", narrators=["R.C. Bray"]),
            make_product(asin="ENS2", narrators=["Scott Brick"]),
        ]
        filtered, _ = _filter_products(products, exclude_narrators=("bray",))
        asins = [p.asin for p in filtered]
        assert "ENS1" not in asins
        assert "ENS2" in asins

    def test_exclude_narrator_case_insensitive(self):
        products = [make_product(asin="ENC1", narrators=["R.C. Bray"])]
        filtered, _ = _filter_products(products, exclude_narrators=("BRAY",))
        assert len(filtered) == 0

    def test_exclude_narrator_multiple(self):
        products = [
            make_product(asin="ENM1", narrators=["R.C. Bray"]),
            make_product(asin="ENM2", narrators=["Scott Brick"]),
            make_product(asin="ENM3", narrators=["Kate Reading"]),
        ]
        filtered, breakdown = _filter_products(
            products, exclude_narrators=("bray", "brick")
        )
        asins = [p.asin for p in filtered]
        assert "ENM1" not in asins
        assert "ENM2" not in asins
        assert "ENM3" in asins
        assert breakdown == {"excluded narrators": 2}

    def test_exclude_narrator_empty_no_filter(self):
        products = [make_product(asin="ENE1", narrators=["Anyone"])]
        filtered, breakdown = _filter_products(products, exclude_narrators=())
        assert len(filtered) == 1
        assert breakdown == {}

    def test_find_exclude_narrator_flag(self, mock_client, tmp_config):
        products = [
            make_product(asin="FEN1", price=3.0, narrators=["R.C. Bray"],
                         series_name="", series_position="", num_ratings=10),
            make_product(asin="FEN2", price=3.0, narrators=["Scott Brick"],
                         series_name="", series_position="", num_ratings=10),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "find_excl_narrator.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "find", "--max-price", "10", "--pages", "1",
            "--exclude-narrator", "bray",
            "--all-languages", "-q", "-n", "0", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "FEN1" not in asins
        assert "FEN2" in asins

    def test_last_exclude_narrator_flag(self, tmp_config):
        import audible_deals.cli as cli_mod
        products = [
            make_product(asin="LEN1", price=3.0, narrators=["R.C. Bray"],
                         series_name="", series_position=""),
            make_product(asin="LEN2", price=3.0, narrators=["Scott Brick"],
                         series_name="", series_position=""),
        ]
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps([_serialize_product(p) for p in products]))
        out_file = tmp_config / "last_excl_narrator.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--exclude-narrator", "bray", "--output", str(out_file)])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LEN1" not in asins
        assert "LEN2" in asins

    def test_exclude_narrator_in_profile(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "profile", "save", "no-bray",
            "--exclude-narrator", "R.C. Bray",
        ])
        assert result.exit_code == 0, result.output
        import audible_deals.cli as cli_mod
        profiles = cli_mod._load_profiles()
        assert "no-bray" in profiles
        excluded = profiles["no-bray"]["exclude_narrators"]
        assert "R.C. Bray" in excluded


# ===================================================================
# Change #9: search QUERY optional
# ===================================================================

class TestSearchQueryOptional:
    def test_search_no_query_no_genre_raises(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["search"])
        assert result.exit_code != 0

    def test_search_with_genre_no_query(self, mock_client, tmp_config):
        products = [make_product(asin="SG1", price=5.0, series_name="", series_position="")]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        mock_client.resolve_genre.return_value = ("cat99", "Mystery")
        out_file = tmp_config / "search_genre.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "search", "--genre", "mystery", "--pages", "1",
            "--all-languages", "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 1

    def test_search_with_category_no_query(self, mock_client, tmp_config):
        products = [make_product(asin="SC1", price=5.0, series_name="", series_position="")]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        mock_client.get_category_name.return_value = "Thriller"
        out_file = tmp_config / "search_cat.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "search", "--category", "123456", "--pages", "1",
            "--all-languages", "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 1

    def test_search_with_query_still_works(self, mock_client, tmp_config):
        products = [make_product(asin="SQ1", price=5.0, series_name="", series_position="")]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        out_file = tmp_config / "search_q.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "search", "test query", "--pages", "1",
            "--all-languages", "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 1


# ===================================================================
# Change #10: profile show command
# ===================================================================

class TestProfileShow:
    def test_profile_show_displays_flags(self, tmp_config):
        import audible_deals.cli as cli_mod
        cli_mod._save_profiles({
            "myprofile": {
                "genre": "sci-fi",
                "max_price": 5.0,
                "min_rating": 4.0,
                "on_sale": True,
            }
        })
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "show", "myprofile"])
        assert result.exit_code == 0, result.output
        assert "myprofile" in result.output
        assert "sci-fi" in result.output
        assert "5.0" in result.output

    def test_profile_show_not_found(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "show", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_profile_show_list_values(self, tmp_config):
        import audible_deals.cli as cli_mod
        cli_mod._save_profiles({
            "multi": {
                "exclude_authors": ["Andy Weir", "Brandon Sanderson"],
            }
        })
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "show", "multi"])
        assert result.exit_code == 0, result.output
        assert "Andy Weir" in result.output
        assert "Brandon Sanderson" in result.output

    def test_profile_show_bool_true_displayed(self, tmp_config):
        """Boolean True values should show as --flag."""
        import audible_deals.cli as cli_mod
        cli_mod._save_profiles({
            "booltest": {
                "deep": True,
            }
        })
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "show", "booltest"])
        assert result.exit_code == 0, result.output
        assert "deep" in result.output

    def test_profile_show_bool_false_displayed_as_no_flag(self, tmp_config):
        """Boolean False values should display as --no-flag."""
        import audible_deals.cli as cli_mod
        cli_mod._save_profiles({
            "falsetest": {
                "deep": False,
                "on_sale": False,
            }
        })
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "show", "falsetest"])
        assert result.exit_code == 0, result.output
        assert "False" not in result.output
        assert "--no-deep" in result.output
        assert "--no-on-sale" in result.output


# ===================================================================
# Change #11: dynamic title column width in display_products
# ===================================================================

class TestDynamicTitleColumnWidth:
    def test_narrow_terminal_uses_minimum(self):
        """On a narrow terminal (e.g. 80 chars), title_max >= 30."""
        from io import StringIO
        from rich.console import Console
        import audible_deals.display as display_mod

        buf = StringIO()
        narrow_console = Console(file=buf, width=80, force_terminal=False)
        original = display_mod.console
        display_mod.console = narrow_console
        try:
            products = [make_product(asin="TW1", title="A Book", price=3.0)]
            display_mod.display_products(products, title="Test")
        finally:
            display_mod.console = original
        out = buf.getvalue()
        assert "A Book" in out

    def test_wide_terminal_uses_larger_width(self):
        """On a wide terminal (e.g. 200 chars), title column should be wider."""
        from io import StringIO
        from rich.console import Console
        import audible_deals.display as display_mod

        buf = StringIO()
        wide_console = Console(file=buf, width=200, force_terminal=False)
        original = display_mod.console
        display_mod.console = wide_console
        try:
            long_title = "A" * 70
            products = [make_product(asin="TW2", title=long_title, price=3.0)]
            display_mod.display_products(products, title="Test")
        finally:
            display_mod.console = original
        out = buf.getvalue()
        # The output should contain at least part of the long title
        assert "TW2" in out


# ===================================================================
# Fix #1: notify silent on empty wishlist
# ===================================================================

class TestNotifyEmptyWishlist:
    def test_notify_empty_wishlist(self, mock_client, tmp_config):
        """notify with an empty wishlist prints a helpful message."""
        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0, result.output
        assert "empty" in result.output.lower()
        assert "wishlist add" in result.output

    def test_notify_no_hits_outputs_empty_json(self, mock_client, tmp_config):
        """notify with items on wishlist but no hits outputs empty JSON object."""
        import json
        import audible_deals.cli as cli_mod
        cli_mod._save_wishlist([
            {"asin": "NT1", "title": "Some Book", "max_price": 5.0, "added": ""},
        ])
        mock_client.get_products_batch.return_value = [
            make_product(asin="NT1", price=10.0),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0, result.output
        assert "wishlist add" not in result.output
        parsed = json.loads(result.output)
        assert parsed == {"deals": [], "count": 0}


# ===================================================================
# Fix #2: search default limit 25
# ===================================================================

class TestSearchDefaultLimit:
    def test_search_default_limit_is_25(self):
        """search --help shows default 25 in help text."""
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "--help"])
        assert result.exit_code == 0
        assert "25" in result.output

    def test_search_returns_25_by_default(self, mock_client, tmp_config):
        """search without -n returns at most 25 results."""
        products = [
            make_product(asin=f"SQ{i:02d}", price=float(i), series_name="", series_position="")
            for i in range(1, 41)
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 40)])
        out_file = tmp_config / "search_def.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "search", "test", "--pages", "1",
            "--all-languages", "-q", "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 25


# ===================================================================
# Fix #3: profile show singular flag names
# ===================================================================

class TestProfileShowSingularFlags:
    def test_exclude_authors_shows_as_exclude_author(self, tmp_config):
        """profile show renders exclude_authors as --exclude-author (singular)."""
        import audible_deals.cli as cli_mod
        cli_mod._save_profiles({"myp": {"exclude_authors": ["Andy Weir", "Terry Brooks"]}})
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "show", "myp"])
        assert result.exit_code == 0, result.output
        assert "--exclude-author" in result.output
        assert "--exclude-authors" not in result.output

    def test_exclude_narrators_shows_as_exclude_narrator(self, tmp_config):
        """profile show renders exclude_narrators as --exclude-narrator (singular)."""
        import audible_deals.cli as cli_mod
        cli_mod._save_profiles({"myp2": {"exclude_narrators": ["R.C. Bray"]}})
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "show", "myp2"])
        assert result.exit_code == 0, result.output
        assert "--exclude-narrator" in result.output
        assert "--exclude-narrators" not in result.output

    def test_other_keys_still_hyphenated(self, tmp_config):
        """profile show still hyphenates other underscore keys correctly."""
        import audible_deals.cli as cli_mod
        cli_mod._save_profiles({"myp3": {"min_rating": 4.0, "first_in_series": True}})
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "show", "myp3"])
        assert result.exit_code == 0, result.output
        assert "--min-rating" in result.output
        assert "--first-in-series" in result.output


# ===================================================================
# Fix #4: --last N shows context description
# ===================================================================

class TestLastRefDescription:
    def test_detail_last_shows_description(self, mock_client, tmp_config):
        """detail --last N prints a dim description of the resolved result."""
        import audible_deals.cli as cli_mod
        p = make_product(asin="DESC1", title="The Martian")
        cache_obj = {"title": "Search: Andy Weir", "results": [_serialize_product(p)]}
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))
        mock_client.get_product.return_value = make_product(asin="DESC1", title="The Martian")
        runner = CliRunner()
        result = runner.invoke(cli, ["detail", "--last", "1"])
        assert result.exit_code == 0, result.output
        assert "Result #1" in result.output
        assert "The Martian" in result.output
        assert "DESC1" in result.output

    def test_open_last_shows_description(self, mock_client, tmp_config):
        """open --last N prints a dim description of the resolved result."""
        import audible_deals.cli as cli_mod
        p = make_product(asin="OPEN1", title="Some Book")
        cache_obj = {"title": "Search: test", "results": [_serialize_product(p)]}
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))
        runner = CliRunner()
        result = runner.invoke(cli, ["open", "--last", "1"])
        assert result.exit_code == 0, result.output
        assert "Result #1" in result.output
        assert "OPEN1" in result.output

    def test_compare_last_shows_description(self, mock_client, tmp_config):
        """compare --last N prints a dim description for each resolved result."""
        import audible_deals.cli as cli_mod
        products = [
            make_product(asin="CMP1", title="Book Alpha"),
            make_product(asin="CMP2", title="Book Beta"),
        ]
        cache_obj = {"title": "Search: test", "results": [_serialize_product(p) for p in products]}
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))
        mock_client.get_products_batch.return_value = [
            make_product(asin="CMP1", title="Book Alpha", price=5.0, length_minutes=600),
            make_product(asin="CMP2", title="Book Beta", price=8.0, length_minutes=600),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["compare", "--last", "1", "--last", "2"])
        assert result.exit_code == 0, result.output
        assert "Result #1" in result.output
        assert "Result #2" in result.output

    def test_wishlist_add_last_shows_description(self, mock_client, tmp_config):
        """wishlist add --last N prints a dim description of the resolved result."""
        import audible_deals.cli as cli_mod
        p = make_product(asin="WADD1", title="Wishlist Book")
        cache_obj = {"title": "Search: test", "results": [_serialize_product(p)]}
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))
        mock_client.get_product.return_value = make_product(asin="WADD1", title="Wishlist Book")
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "add", "--last", "1"])
        assert result.exit_code == 0, result.output
        assert "Result #1" in result.output
        assert "WADD1" in result.output


# ===================================================================
# Fix #5: --first-in-series strict (only Book 1 passes)
# ===================================================================

class TestFirstInSeriesStrict:
    def test_book3_only_gets_filtered_out(self):
        """A series with only Book 3 should be excluded (no Book 1)."""
        products = [
            make_product(asin="FIS1", series_name="Epic Series", series_position="3"),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 1
        assert len(result) == 0

    def test_prequel_at_half_passes(self):
        """Position 0.5 (prequel) is <= 1.0 so it passes through."""
        products = [
            make_product(asin="FIS2", series_name="Epic Series", series_position="0.5"),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 0
        assert len(result) == 1
        assert result[0].asin == "FIS2"

    def test_position_one_point_zero_passes(self):
        """Position '1.0' is exactly <= 1.0 so it passes."""
        products = [
            make_product(asin="FIS3", series_name="Epic Series", series_position="1.0"),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 0
        assert len(result) == 1
        assert result[0].asin == "FIS3"

    def test_book1_in_series_passes(self):
        """Position '1' passes through."""
        products = [
            make_product(asin="FIS4", series_name="A Series", series_position="1"),
            make_product(asin="FIS5", series_name="A Series", series_position="2"),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 1
        assert result[0].asin == "FIS4"

    def test_non_series_pass_through_unchanged(self):
        """Non-series items are never affected by the strict check."""
        products = [
            make_product(asin="FIS6", series_name=""),
            make_product(asin="FIS7", series_name=""),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 0
        assert len(result) == 2

    def test_mixed_book1_and_no_book1(self):
        """Series with Book 1 keeps it; series without Book 1 is excluded."""
        products = [
            make_product(asin="FIS8", series_name="HasBook1", series_position="1"),
            make_product(asin="FIS9", series_name="NoBook1", series_position="3"),
        ]
        result, collapsed = _first_in_series(products)
        asins = [p.asin for p in result]
        assert "FIS8" in asins
        assert "FIS9" not in asins
        assert collapsed == 1


# ===================================================================
# Fix #6: search suggests --author for person name queries
# ===================================================================

class TestLooksLikePersonName:
    def test_two_words_title_case(self):
        assert _looks_like_person_name("Andy Weir") is True

    def test_three_words_title_case(self):
        assert _looks_like_person_name("Brandon Scott Sanderson") is True

    def test_one_word_not_a_name(self):
        assert _looks_like_person_name("Dune") is False

    def test_four_words_not_a_name(self):
        assert _looks_like_person_name("One Two Three Four") is False

    def test_lowercase_not_a_name(self):
        assert _looks_like_person_name("andy weir") is False

    def test_mixed_case_not_all_upper(self):
        assert _looks_like_person_name("Andy weir") is False

    def test_empty_string(self):
        assert _looks_like_person_name("") is False


class TestSearchAuthorHint:
    def test_hint_shown_for_person_name_query(self, mock_client, tmp_config):
        """search shows --author tip when query looks like a person name."""
        products = [make_product(asin="AH1", price=5.0, series_name="", series_position="")]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        runner = CliRunner()
        result = runner.invoke(cli, [
            "search", "Andy Weir", "--pages", "1", "--all-languages", "-n", "0",
        ])
        assert result.exit_code == 0, result.output
        assert "--author" in result.output
        assert "Andy Weir" in result.output

    def test_hint_not_shown_when_author_already_set(self, mock_client, tmp_config):
        """search does NOT show tip when --author is already used."""
        products = [make_product(asin="AH2", price=5.0, series_name="", series_position="")]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        runner = CliRunner()
        result = runner.invoke(cli, [
            "search", "Andy Weir", "--author", "Andy Weir", "--pages", "1",
            "--all-languages", "-n", "0",
        ])
        assert result.exit_code == 0, result.output
        assert "Tip:" not in result.output

    def test_hint_not_shown_for_non_name_query(self, mock_client, tmp_config):
        """search does NOT show tip for a single-word query."""
        products = [make_product(asin="AH3", price=5.0, series_name="", series_position="")]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        runner = CliRunner()
        result = runner.invoke(cli, [
            "search", "Dune", "--pages", "1", "--all-languages", "-n", "0",
        ])
        assert result.exit_code == 0, result.output
        assert "Tip:" not in result.output

    def test_hint_not_shown_with_quiet(self, mock_client, tmp_config):
        """search does NOT show tip in quiet mode."""
        products = [make_product(asin="AH4", price=5.0, series_name="", series_position="")]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        runner = CliRunner()
        result = runner.invoke(cli, [
            "search", "Andy Weir", "--pages", "1", "--all-languages", "-q",
        ])
        assert result.exit_code == 0, result.output
        assert "Tip:" not in result.output


# ===================================================================
# Fix #7: library filters
# ===================================================================

class TestLibraryFilters:

    def test_library_author_filter(self, mock_client, tmp_config):
        """library --author filters by author substring."""
        products = [
            make_product(asin="LA1", authors=["Andy Weir"]),
            make_product(asin="LA2", authors=["Brandon Sanderson"]),
        ]
        _mock_library_pages(mock_client, products)
        out_file = tmp_config / "lib_auth.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["library", "--author", "weir", "-q", "-o", str(out_file)])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LA1" in asins
        assert "LA2" not in asins

    def test_library_narrator_filter(self, mock_client, tmp_config):
        """library --narrator filters by narrator substring."""
        products = [
            make_product(asin="LN1", narrators=["R.C. Bray"]),
            make_product(asin="LN2", narrators=["Scott Brick"]),
        ]
        _mock_library_pages(mock_client, products)
        out_file = tmp_config / "lib_narr.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["library", "--narrator", "bray", "-q", "-o", str(out_file)])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LN1" in asins
        assert "LN2" not in asins

    def test_library_genre_filter(self, mock_client, tmp_config):
        """library --genre filters by category substring."""
        products = [
            make_product(asin="LG1", categories=["Science Fiction & Fantasy", "Fantasy"]),
            make_product(asin="LG2", categories=["Mystery, Thriller & Suspense"]),
        ]
        _mock_library_pages(mock_client, products)
        out_file = tmp_config / "lib_genre.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["library", "--genre", "science fiction", "-q", "-o", str(out_file)])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LG1" in asins
        assert "LG2" not in asins

    def test_library_min_rating_filter(self, mock_client, tmp_config):
        """library --min-rating filters by rating."""
        products = [
            make_product(asin="LR1", rating=4.5),
            make_product(asin="LR2", rating=3.0),
        ]
        _mock_library_pages(mock_client, products)
        out_file = tmp_config / "lib_rating.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["library", "--min-rating", "4.0", "-q", "-o", str(out_file)])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LR1" in asins
        assert "LR2" not in asins

    def test_library_min_ratings_filter(self, mock_client, tmp_config):
        """library --min-ratings filters by number of ratings."""
        products = [
            make_product(asin="LC1", num_ratings=500),
            make_product(asin="LC2", num_ratings=20),
        ]
        _mock_library_pages(mock_client, products)
        out_file = tmp_config / "lib_count.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["library", "--min-ratings", "100", "-q", "-o", str(out_file)])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LC1" in asins
        assert "LC2" not in asins

    def test_library_min_hours_filter(self, mock_client, tmp_config):
        """library --min-hours filters by length."""
        products = [
            make_product(asin="LH1", length_minutes=600),  # 10hrs
            make_product(asin="LH2", length_minutes=60),   # 1hr
        ]
        _mock_library_pages(mock_client, products)
        out_file = tmp_config / "lib_hours.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["library", "--min-hours", "5", "-q", "-o", str(out_file)])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LH1" in asins
        assert "LH2" not in asins


# ===================================================================
# Fix #8: library page-level progress (get_library_pages)
# ===================================================================

class TestLibraryPages:
    def test_get_library_pages_multi_page(self, mock_client, tmp_config):
        """library accumulates products across multiple pages."""
        page1 = [make_product(asin=f"MP{i}") for i in range(1, 4)]
        page2 = [make_product(asin=f"MP{i}") for i in range(4, 7)]
        mock_client.get_library_pages.return_value = iter([(page1, 1), (page2, 2)])
        out_file = tmp_config / "lib_pages.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["library", "-q", "-o", str(out_file)])
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 6


# ===================================================================
# Fix #9: narrator help text note in find, search, last
# ===================================================================

class TestNarratorHelpText:
    def test_find_narrator_help_says_client_side(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--help"])
        assert result.exit_code == 0
        assert "client-side" in result.output

    def test_search_narrator_help_says_client_side(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "--help"])
        assert result.exit_code == 0
        assert "client-side" in result.output

    def test_last_narrator_help_says_client_side(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--help"])
        assert result.exit_code == 0
        assert "client-side" in result.output


# ===================================================================
# UX fixes: config reset confirmation, recap --days validation,
# wishlist remove --last, history --last
# ===================================================================

class TestConfigResetConfirmation:
    def test_reset_all_confirmed(self, tmp_config):
        """config reset with no key clears config when user confirms."""
        import audible_deals.cli as cli_mod
        cli_mod._save_config({"max_price": 5.0, "min_rating": 4.0})
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "reset"], input="y\n")
        assert result.exit_code == 0, result.output
        assert "All global defaults cleared" in result.output
        assert cli_mod._load_config() == {}

    def test_reset_all_cancelled(self, tmp_config):
        """config reset with no key leaves config intact when user cancels."""
        import audible_deals.cli as cli_mod
        cli_mod._save_config({"max_price": 5.0})
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "reset"], input="n\n")
        assert result.exit_code == 0, result.output
        assert "Cancelled" in result.output
        assert cli_mod._load_config() == {"max_price": 5.0}

    def test_reset_key_no_confirmation_needed(self, tmp_config):
        """config reset KEY skips the confirmation prompt."""
        import audible_deals.cli as cli_mod
        cli_mod._save_config({"max_price": 5.0, "min_rating": 4.0})
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "reset", "max-price"])
        assert result.exit_code == 0, result.output
        cfg = cli_mod._load_config()
        assert "max_price" not in cfg
        assert "min_rating" in cfg


class TestRecapDaysValidation:
    def test_days_zero_rejected(self, tmp_config):
        """recap --days 0 is rejected as out of range."""
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "0"])
        assert result.exit_code != 0

    def test_days_negative_rejected(self, tmp_config):
        """recap --days -1 is rejected as out of range."""
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "-1"])
        assert result.exit_code != 0

    def test_days_one_accepted(self, tmp_config):
        """recap --days 1 is accepted (minimum valid value)."""
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "1"])
        assert result.exit_code == 0, result.output

    def test_days_default_accepted(self, tmp_config):
        """recap with no --days uses default of 7 and succeeds."""
        runner = CliRunner()
        result = runner.invoke(cli, ["recap"])
        assert result.exit_code == 0, result.output


class TestWishlistRemoveLast:
    def _seed_cache(self, tmp_config, products):
        import audible_deals.cli as cli_mod
        cache_obj = {"title": "Search: test", "results": [_serialize_product(p) for p in products]}
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))

    def test_remove_last_resolves_from_cache(self, tmp_config):
        """wishlist remove --last N resolves the ASIN from the last results cache."""
        import audible_deals.cli as cli_mod
        p = make_product(asin="WRL1", title="Remove Me")
        self._seed_cache(tmp_config, [p])
        cli_mod._save_wishlist([{"asin": "WRL1", "title": "Remove Me", "max_price": None, "added": ""}])

        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "remove", "--last", "1"])
        assert result.exit_code == 0, result.output
        assert "Result #1" in result.output
        assert "1 removed" in result.output

    def test_remove_last_and_asin_combined(self, tmp_config):
        """wishlist remove supports mixing positional ASINs and --last refs."""
        import audible_deals.cli as cli_mod
        p = make_product(asin="WRL2", title="Cache Book")
        self._seed_cache(tmp_config, [p])
        cli_mod._save_wishlist([
            {"asin": "WRL2", "title": "Cache Book", "max_price": None, "added": ""},
            {"asin": "WRL3", "title": "Direct Book", "max_price": None, "added": ""},
        ])

        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "remove", "WRL3", "--last", "1"])
        assert result.exit_code == 0, result.output
        assert "2 removed" in result.output

    def test_remove_no_args_raises_usage_error(self, tmp_config):
        """wishlist remove with no arguments and no --last raises a UsageError."""
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "remove"])
        assert result.exit_code != 0
        assert "ASIN" in result.output or "Usage" in result.output


class TestHistoryLast:
    def _seed_cache(self, tmp_config, products):
        import audible_deals.cli as cli_mod
        cache_obj = {"title": "Search: test", "results": [_serialize_product(p) for p in products]}
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))

    def test_history_last_resolves_from_cache(self, tmp_config):
        """history --last N resolves the ASIN from the last results cache."""
        import audible_deals.cli as cli_mod
        from audible_deals.cli import _record_prices
        p = make_product(asin="HL1", price=4.99, title="Cache History Book")
        self._seed_cache(tmp_config, [p])
        _record_prices([p])

        runner = CliRunner()
        result = runner.invoke(cli, ["history", "--last", "1"])
        assert result.exit_code == 0, result.output
        assert "Result #1" in result.output
        assert "HL1" in result.output

    def test_history_no_asin_no_last_raises(self, tmp_config):
        """history with no ASIN and no --last raises a UsageError."""
        runner = CliRunner()
        result = runner.invoke(cli, ["history"])
        assert result.exit_code != 0


# ===================================================================
# Feature: --max-price-per-hour filter
# ===================================================================

class TestMaxPricePerHour:
    def test_filters_high_pph(self):
        products = [
            make_product(asin="CHEAP", price=2.0, length_minutes=600),  # $0.20/hr
            make_product(asin="EXPENSIVE", price=10.0, length_minutes=60),  # $10/hr
        ]
        filtered, breakdown = _filter_products(products, max_pph=1.0)
        assert len(filtered) == 1
        assert filtered[0].asin == "CHEAP"
        assert "max $/hr" in breakdown

    def test_no_filter_when_none(self):
        products = [make_product(price=10.0, length_minutes=60)]
        filtered, breakdown = _filter_products(products, max_pph=None)
        assert len(filtered) == 1

    def test_excludes_items_with_no_price(self):
        products = [
            make_product(asin="NOPRICE", price=None, length_minutes=600),
            make_product(asin="PRICED", price=2.0, length_minutes=600),
        ]
        filtered, breakdown = _filter_products(products, max_pph=1.0)
        assert len(filtered) == 1
        assert filtered[0].asin == "PRICED"

    def test_excludes_items_with_zero_hours(self):
        products = [
            make_product(asin="ZEROHRS", price=1.0, length_minutes=0),
            make_product(asin="PRICED", price=2.0, length_minutes=600),
        ]
        filtered, breakdown = _filter_products(products, max_pph=1.0)
        assert len(filtered) == 1
        assert filtered[0].asin == "PRICED"


# ===================================================================
# Feature: --sort value (composite sort)
# ===================================================================

class TestValueSort:
    def test_value_sort(self):
        high_value = make_product(asin="HV", price=2.0, length_minutes=1200, rating=4.8)
        # score = (4.8 * 20) / 2 = 48
        low_value = make_product(asin="LV", price=10.0, length_minutes=60, rating=3.0)
        # score = (3.0 * 1) / 10 = 0.3
        result = _sort_local([low_value, high_value], "value")
        assert result[0].asin == "HV"
        assert result[1].asin == "LV"

    def test_value_score_zero_price(self):
        p = make_product(price=0.0, length_minutes=600, rating=4.5)
        assert _value_score(p) == 0.0

    def test_value_score_none_price(self):
        p = make_product(price=None, length_minutes=600, rating=4.5)
        assert _value_score(p) == 0.0

    def test_value_score_zero_hours(self):
        p = make_product(price=5.0, length_minutes=0, rating=4.5)
        assert _value_score(p) == 0.0

    def test_value_score_zero_rating(self):
        p = make_product(price=5.0, length_minutes=600, rating=0.0)
        assert _value_score(p) == 0.0

    def test_value_score_positive(self):
        p = make_product(price=2.0, length_minutes=600, rating=4.0)
        # (4.0 * 10) / 2 = 20
        assert _value_score(p) == pytest.approx(20.0)


# ===================================================================
# Feature: _load_seen_asins
# ===================================================================

class TestLoadSeenAsins:
    def test_loads_from_seen_file(self, tmp_config):
        import audible_deals.cli as cli_mod
        cli_mod.SEEN_ASINS_FILE.write_text(json.dumps(["A1", "A2"]))
        seen = _load_seen_asins()
        assert seen == {"A1", "A2"}

    def test_empty_when_no_file(self, tmp_config):
        seen = _load_seen_asins()
        assert seen == set()

    def test_returns_set_from_list(self, tmp_config):
        import audible_deals.cli as cli_mod
        cli_mod.SEEN_ASINS_FILE.write_text(json.dumps(["B1", "B2", "B1"]))
        seen = _load_seen_asins()
        assert seen == {"B1", "B2"}

    def test_empty_on_corrupt_file(self, tmp_config):
        import audible_deals.cli as cli_mod
        cli_mod.SEEN_ASINS_FILE.write_text("not valid json")
        seen = _load_seen_asins()
        assert seen == set()


# ===================================================================
# Fix: --min-discount filter
# ===================================================================

class TestMinDiscount:
    def test_filters_low_discount(self):
        products = [
            make_product(asin="HIGH", price=3.0, list_price=20.0),   # 85% off
            make_product(asin="LOW", price=8.0, list_price=10.0),    # 20% off
            make_product(asin="NONE", price=5.0, list_price=5.0),    # 0% off
        ]
        filtered, breakdown = _filter_products(products, min_discount=50)
        assert len(filtered) == 1
        assert filtered[0].asin == "HIGH"
        assert "min discount" in breakdown

    def test_no_filter_when_zero(self):
        products = [make_product(price=5.0, list_price=5.0)]
        filtered, breakdown = _filter_products(products, min_discount=0)
        assert len(filtered) == 1


# ===================================================================
# Fix: value sort tiebreaker
# ===================================================================

class TestValueSortTiebreaker:
    def test_tiebreaker_by_rating(self):
        """Items with same value score should sort by rating."""
        # score 0.0 because rating == 0
        unrated = make_product(asin="UNRATED", price=5.0, length_minutes=600, rating=0.0)
        # score 0.0 because hours == 0
        zero_hrs = make_product(asin="ZEROHRS", price=5.0, length_minutes=0, rating=4.5)
        result = _sort_local([unrated, zero_hrs], "value")
        # zero_hrs has rating 4.5 > 0.0, so it should come first
        assert result[0].asin == "ZEROHRS"
        assert result[1].asin == "UNRATED"


# ===================================================================
# Fix: cumulative seen ASINs
# ===================================================================

class TestCumulativeSeenAsins:
    def test_save_and_load(self, tmp_config):
        _save_seen_asins({"A1", "A2"})
        assert _load_seen_asins() == {"A1", "A2"}

    def test_cumulative_append(self, tmp_config):
        _save_seen_asins({"A1", "A2"})
        _save_seen_asins({"A3", "A4"})
        assert _load_seen_asins() == {"A1", "A2", "A3", "A4"}

    def test_no_duplicates(self, tmp_config):
        import audible_deals.cli as cli_mod
        _save_seen_asins({"A1", "A2"})
        _save_seen_asins({"A2", "A3"})
        seen = _load_seen_asins()
        assert seen == {"A1", "A2", "A3"}
        # Verify file is a clean sorted list
        data = json.loads(cli_mod.SEEN_ASINS_FILE.read_text())
        assert data == sorted(data)

    def test_empty_when_no_file(self, tmp_config):
        assert _load_seen_asins() == set()

    def test_clear_seen_command(self, tmp_config, mock_client):
        _save_seen_asins({"A1", "A2"})
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--clear-seen"])
        assert result.exit_code == 0
        assert "cleared" in result.output.lower()
        assert _load_seen_asins() == set()


# ===================================================================
# Fix 2: Profile save preserves falsy values (0, False)
# ===================================================================

class TestProfileSaveFalsy:
    def test_profile_save_preserves_zero_max_price(self, tmp_config, monkeypatch):
        """profile save preserves max_price=0.0 (falsy but valid)."""
        import audible_deals.cli as cli_mod
        monkeypatch.setattr(cli_mod, "PROFILES_FILE", tmp_config / "profiles.json")
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "save", "zeroprofile", "--max-price", "0"])
        assert result.exit_code == 0, result.output
        profiles = cli_mod._load_profiles()
        assert "max_price" in profiles["zeroprofile"]
        assert profiles["zeroprofile"]["max_price"] == 0.0

    def test_profile_save_drops_false_flags(self, tmp_config, monkeypatch):
        """profile save drops False boolean flags (they are always defaults, not explicit choices)."""
        import audible_deals.cli as cli_mod
        monkeypatch.setattr(cli_mod, "PROFILES_FILE", tmp_config / "profiles.json")
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "save", "falseprofile", "--genre", "sci-fi"])
        assert result.exit_code == 0, result.output
        profiles = cli_mod._load_profiles()
        # on_sale=False is NOT stored — profile save's is_flag options only capture True
        assert "on_sale" not in profiles["falseprofile"]

    def test_profile_save_drops_empty(self, tmp_config, monkeypatch):
        """profile save drops empty strings and empty tuples but not zero."""
        import audible_deals.cli as cli_mod
        monkeypatch.setattr(cli_mod, "PROFILES_FILE", tmp_config / "profiles.json")
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "save", "emptyprofile", "--max-price", "0"])
        assert result.exit_code == 0, result.output
        profiles = cli_mod._load_profiles()
        # Empty string fields like genre, author etc. should not be saved
        assert "genre" not in profiles["emptyprofile"]
        assert "author" not in profiles["emptyprofile"]
        assert "narrator" not in profiles["emptyprofile"]
        # But max_price=0.0 should be saved
        assert profiles["emptyprofile"]["max_price"] == 0.0


class TestProfileSaveZeroDefaults:
    def test_profile_save_omits_zero_defaults(self, tmp_config):
        """profile save --genre sci-fi must NOT save min_rating=0.0 etc."""
        from audible_deals.state import _load_profiles
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "save", "zerotest", "--genre", "sci-fi"])
        assert result.exit_code == 0, result.output
        profiles = _load_profiles()
        assert "genre" in profiles["zerotest"]
        assert profiles["zerotest"]["genre"] == "sci-fi"
        assert "min_rating" not in profiles["zerotest"]
        assert "min_ratings" not in profiles["zerotest"]
        assert "min_hours" not in profiles["zerotest"]

    def test_profile_save_preserves_explicit_zero(self, tmp_config):
        """profile save --max-price 0 must keep max_price=0.0."""
        from audible_deals.state import _load_profiles
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "save", "zeroexplicit", "--max-price", "0"])
        assert result.exit_code == 0, result.output
        profiles = _load_profiles()
        assert profiles["zeroexplicit"]["max_price"] == 0.0


# ===================================================================
# Fix 5: Notify outputs empty JSON when no alerts
# ===================================================================

class TestNotifyEmpty:
    def test_notify_no_hits_prints_empty_json(self, mock_client, tmp_config):
        """notify with wishlist items above target prints '[]' to stdout."""
        import audible_deals.cli as cli_mod
        # Add a wishlist item with a low target (price above target = no hit)
        cli_mod._save_wishlist([
            {"asin": "NE01", "title": "Pricey Book", "max_price": 1.0, "added": "2024-01-01"},
        ])
        # Mock get_products_batch to return product with price above target
        mock_client.get_products_batch.return_value = [
            make_product(asin="NE01", price=9.99),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0, result.output
        assert "[]" in result.output

    def test_notify_no_hits_with_webhook_shows_feedback(self, mock_client, tmp_config, monkeypatch):
        """notify with no hits and a webhook prints feedback but does not POST."""
        import audible_deals.cli as cli_mod
        cli_mod._save_wishlist([
            {"asin": "NE02", "title": "Pricey Book", "max_price": 1.0, "added": "2024-01-01"},
        ])
        mock_client.get_products_batch.return_value = [
            make_product(asin="NE02", price=9.99),
        ]
        # Use a valid-looking but unreachable webhook; should never be called
        monkeypatch.setattr("audible_deals.cli._validate_webhook_url", lambda url: None)
        runner = CliRunner()
        result = runner.invoke(cli, ["notify", "--webhook", "https://example.com/hook"])
        assert result.exit_code == 0, result.output
        assert "[]" not in result.output
        assert "Nothing sent to webhook" in result.output


# ===================================================================
# Bug fix: notify $0 target + profile save missing options
# ===================================================================


class TestNotifyZeroTarget:
    def test_notify_zero_target_fires(self, mock_client, tmp_config):
        """notify must fire when max_price=0 and product price is 0 (was falsy bug)."""
        import audible_deals.cli as cli_mod
        cli_mod._save_wishlist([
            {"asin": "Z001", "title": "Free Book", "max_price": 0, "added": "2024-01-01"},
        ])
        mock_client.get_products_batch.return_value = [
            make_product(asin="Z001", price=0.0),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["count"] == 1
        assert data["deals"][0]["asin"] == "Z001"


class TestProfileSaveNewOptions:
    def test_profile_save_min_discount(self, tmp_config):
        """profile save --min-discount should persist."""
        from audible_deals.state import _load_profiles
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "save", "disctest", "--min-discount", "50"])
        assert result.exit_code == 0, result.output
        profiles = _load_profiles()
        assert profiles["disctest"]["min_discount"] == 50

    def test_profile_save_max_pph(self, tmp_config):
        """profile save --max-price-per-hour should persist."""
        from audible_deals.state import _load_profiles
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "save", "pphtest", "--max-price-per-hour", "0.5"])
        assert result.exit_code == 0, result.output
        profiles = _load_profiles()
        assert profiles["pphtest"]["max_pph"] == 0.5

    def test_profile_save_publisher(self, tmp_config):
        """profile save --publisher should persist."""
        from audible_deals.state import _load_profiles
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "save", "pubtest", "--publisher", "Penguin"])
        assert result.exit_code == 0, result.output
        profiles = _load_profiles()
        assert profiles["pubtest"]["publisher"] == "Penguin"


# ===================================================================
# Fix 7: --dry-run for find and search
# ===================================================================

class TestDryRunFind:
    def test_find_dry_run_shows_summary(self, mock_client, tmp_config):
        """find --dry-run prints scan summary and does not call search_pages."""
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--dry-run", "--pages", "5"])
        assert result.exit_code == 0, result.output
        assert "Dry run" in result.output
        assert "Sort orders" in result.output
        assert "Pages per sort" in result.output
        assert "API calls" in result.output
        mock_client.search_pages.assert_not_called()

    def test_find_dry_run_shows_category(self, mock_client, tmp_config):
        """find --dry-run with genre resolved shows category name."""
        mock_client._categories_cache = [{"id": "cat1", "name": "Mystery, Thriller & Suspense"}]
        mock_client.resolve_genre.return_value = ("cat1", "Mystery, Thriller & Suspense")
        mock_client.__enter__ = lambda s: s
        mock_client.__exit__ = lambda s, *a: False

        # Bypass real genre resolution by using --category
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--dry-run", "--pages", "2", "--category", "cat1"])
        assert result.exit_code == 0, result.output
        assert "Dry run" in result.output
        mock_client.search_pages.assert_not_called()


class TestDryRunSearch:
    def test_search_dry_run_shows_summary(self, mock_client, tmp_config):
        """search --dry-run prints scan summary and does not call search_pages."""
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "fantasy", "--dry-run", "--pages", "3"])
        assert result.exit_code == 0, result.output
        assert "Dry run" in result.output
        assert "Query: fantasy" in result.output
        assert "Sort orders" in result.output
        assert "API calls" in result.output
        mock_client.search_pages.assert_not_called()

    def test_search_dry_run_does_not_call_catalog(self, mock_client, tmp_config):
        """search --dry-run does not call search_catalog."""
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "test", "--dry-run"])
        assert result.exit_code == 0, result.output
        mock_client.search_catalog.assert_not_called()


# ===================================================================
# Fix 8: deals last --count
# ===================================================================

class TestLastCount:
    def _write_cache(self, tmp_config, products):
        """Write a mock last results cache."""
        import audible_deals.cli as cli_mod
        data = [cli_mod._serialize_product(p) for p in products]
        payload = json.dumps({"title": "Test Results", "results": data})
        cli_mod.LAST_RESULTS_FILE.write_text(payload)

    def test_last_count_outputs_integer(self, tmp_config):
        """deals last --count prints the number of cached results."""
        products = [make_product(asin=f"LC{i:02d}", price=float(i)) for i in range(1, 8)]
        self._write_cache(tmp_config, products)
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--count"])
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "7"

    def test_last_count_zero_when_empty_cache(self, tmp_config):
        """deals last --count returns 0 for an empty result cache."""
        import audible_deals.cli as cli_mod
        cli_mod.LAST_RESULTS_FILE.write_text(json.dumps({"title": "Empty", "results": []}))
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--count"])
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "0"


# ===================================================================
# Fix 9: --series filter
# ===================================================================

class TestFilterSeries:
    def test_filter_series_match(self):
        """Products with series_name containing the search string are kept."""
        products = [
            make_product(asin="S1", series_name="The Stormlight Archive"),
            make_product(asin="S2", series_name="Mistborn"),
            make_product(asin="S3", series_name="Stormlight Chronicles"),
        ]
        filtered, breakdown = _filter_products(products, series="stormlight")
        assert len(filtered) == 2
        assert all(p.asin in ("S1", "S3") for p in filtered)

    def test_filter_series_no_match(self):
        """Products without matching series are excluded."""
        products = [
            make_product(asin="S1", series_name="Mistborn"),
            make_product(asin="S2", series_name="The Way of Kings"),
        ]
        filtered, breakdown = _filter_products(products, series="wheel of time")
        assert len(filtered) == 0
        assert breakdown.get("series") == 2

    def test_filter_series_case_insensitive(self):
        """Series filter is case-insensitive."""
        products = [
            make_product(asin="S1", series_name="The Dresden Files"),
        ]
        filtered, _ = _filter_products(products, series="DRESDEN")
        assert len(filtered) == 1

    def test_filter_series_empty_no_filter(self):
        """Empty series string does not filter anything."""
        products = [
            make_product(asin="S1", series_name="Some Series"),
            make_product(asin="S2", series_name=""),
        ]
        filtered, breakdown = _filter_products(products, series="")
        assert len(filtered) == 2
        assert "series" not in breakdown


# ===================================================================
# series command
# ===================================================================

class TestSeriesCommand:
    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        monkeypatch.setattr("audible_deals.cli.time.sleep", lambda _: None)

    def test_series_direct_lookup(self, tmp_config, mock_client):
        """With series_asin, uses direct lookup via get_series_products."""
        lib = [
            make_product(asin="A1", title="Alpha Book 1", series_name="Alpha Series", series_asin="SER_ALPHA"),
            make_product(asin="A2", title="Alpha Book 2", series_name="Alpha Series", series_asin="SER_ALPHA"),
        ]
        mock_client.get_library.return_value = lib

        unowned = make_product(asin="A3", title="Alpha Book 3", series_name="Alpha Series")
        mock_client.get_series_products.return_value = [lib[0], lib[1], unowned]

        runner = CliRunner()
        result = runner.invoke(cli, ["series"])
        assert result.exit_code == 0, result.output
        assert "Alpha Book 3" in result.output
        mock_client.get_series_products.assert_called_once_with("SER_ALPHA")
        mock_client.search_pages.assert_not_called()

    def test_series_keyword_fallback(self, tmp_config, mock_client):
        """Without series_asin, falls back to keyword search."""
        lib = [
            make_product(asin="A1", title="Alpha Book 1", series_name="Alpha Series", series_asin=""),
            make_product(asin="A2", title="Alpha Book 2", series_name="Alpha Series", series_asin=""),
        ]
        mock_client.get_library.return_value = lib

        unowned = make_product(asin="A3", title="Alpha Book 3", series_name="Alpha Series")
        mock_client.search_pages.return_value = iter([([unowned], 1, 1)])

        runner = CliRunner()
        result = runner.invoke(cli, ["series"])
        assert result.exit_code == 0, result.output
        assert "Alpha Book 3" in result.output
        mock_client.get_series_products.assert_not_called()
        assert mock_client.search_pages.call_count == 1

    def test_series_min_books_filters(self, tmp_config, mock_client):
        """Library has only 1 book with a series name; should report no invested series."""
        lib = [
            make_product(asin="B1", title="Beta Book 1", series_name="Beta Series"),
        ]
        mock_client.get_library.return_value = lib

        runner = CliRunner()
        result = runner.invoke(cli, ["series"])
        assert result.exit_code == 0, result.output
        assert "No series with 2+ owned books" in result.output

    def test_series_filter_by_name(self, tmp_config, mock_client):
        """--series Alpha filters to only Alpha Series."""
        lib = [
            make_product(asin="A1", title="Alpha Book 1", series_name="Alpha Series", series_asin="SER_ALPHA"),
            make_product(asin="A2", title="Alpha Book 2", series_name="Alpha Series", series_asin="SER_ALPHA"),
            make_product(asin="B1", title="Beta Book 1", series_name="Beta Series", series_asin="SER_BETA"),
            make_product(asin="B2", title="Beta Book 2", series_name="Beta Series", series_asin="SER_BETA"),
        ]
        mock_client.get_library.return_value = lib

        unowned_alpha = make_product(asin="A3", title="Alpha Book 3", series_name="Alpha Series")
        mock_client.get_series_products.return_value = [lib[0], lib[1], unowned_alpha]

        runner = CliRunner()
        result = runner.invoke(cli, ["series", "--series", "Alpha"])
        assert result.exit_code == 0, result.output

        mock_client.get_series_products.assert_called_once_with("SER_ALPHA")
        assert "Alpha Book 3" in result.output

    def test_series_skips_owned(self, tmp_config, mock_client):
        """Owned books from series lookup are excluded from output."""
        lib = [
            make_product(asin="A1", title="Alpha Book 1", series_name="Alpha Series", series_asin="SER_ALPHA"),
            make_product(asin="A2", title="Alpha Book 2", series_name="Alpha Series", series_asin="SER_ALPHA"),
        ]
        mock_client.get_library.return_value = lib

        a1 = make_product(asin="A1", title="Alpha Book 1", series_name="Alpha Series")
        a2 = make_product(asin="A2", title="Alpha Book 2", series_name="Alpha Series")
        a3 = make_product(asin="A3", title="Alpha Book 3", series_name="Alpha Series")
        mock_client.get_series_products.return_value = [a1, a2, a3]

        runner = CliRunner()
        result = runner.invoke(cli, ["series"])
        assert result.exit_code == 0, result.output
        assert "Alpha Book 3" in result.output
        assert "Alpha Book 1" not in result.output
        assert "Alpha Book 2" not in result.output

    def test_series_min_books_custom_threshold(self, tmp_config, mock_client):
        """--min-books 3 requires 3+ owned; 2 owned should report nothing."""
        lib = [
            make_product(asin="A1", title="Alpha 1", series_name="Alpha Series"),
            make_product(asin="A2", title="Alpha 2", series_name="Alpha Series"),
        ]
        mock_client.get_library.return_value = lib

        runner = CliRunner()
        result = runner.invoke(cli, ["series", "--min-books", "3"])
        assert result.exit_code == 0, result.output
        assert "No series with 3+ owned books" in result.output

    def test_series_empty_library(self, tmp_config, mock_client):
        """Empty library reports no invested series."""
        mock_client.get_library.return_value = []

        runner = CliRunner()
        result = runner.invoke(cli, ["series"])
        assert result.exit_code == 0, result.output
        assert "No series with 2+ owned books" in result.output

    def test_series_json_output(self, tmp_config, mock_client):
        """--json flag outputs valid JSON list to stdout."""
        lib = [
            make_product(asin="A1", title="Alpha Book 1", series_name="Alpha Series", series_asin="SER_ALPHA"),
            make_product(asin="A2", title="Alpha Book 2", series_name="Alpha Series", series_asin="SER_ALPHA"),
        ]
        mock_client.get_library.return_value = lib

        unowned = make_product(asin="A3", title="Alpha Book 3", series_name="Alpha Series")
        mock_client.get_series_products.return_value = [lib[0], lib[1], unowned]

        runner = CliRunner()
        result = runner.invoke(cli, ["series", "--json"])
        assert result.exit_code == 0, result.output
        # Progress bar may leak into stdout in test; extract JSON portion
        json_start = result.output.index("[")
        data = json.loads(result.output[json_start:])
        assert isinstance(data, list)
        assert any(item["asin"] == "A3" for item in data)


# ===================================================================
# Fix: Config/Profile string-key precedence
# ===================================================================

class TestStringKeyPrecedence:
    """Profile string keys must override config string keys (CLI > Profile > Config)."""

    def test_profile_string_overrides_config(self):
        """When config set a string, profile must override it."""
        from unittest.mock import MagicMock
        import click
        from audible_deals.cli import _apply_config_defaults, _apply_profile_defaults

        ctx = MagicMock()
        ctx.get_parameter_source.return_value = click.core.ParameterSource.DEFAULT
        ns = {"language": "", "narrator": "", "author": "", "series": "", "publisher": ""}
        cfg = {"language": "english", "narrator": "Alice"}
        profile = {"language": "french", "narrator": "Bob"}

        _apply_config_defaults(ctx, ns, cfg)
        assert ns["language"] == "english"
        assert ns["narrator"] == "Alice"

        _apply_profile_defaults(ctx, ns, profile)
        assert ns["language"] == "french"
        assert ns["narrator"] == "Bob"

    def test_config_string_applied_when_no_profile(self):
        """Config string fills ns when CLI absent and no profile override."""
        from unittest.mock import MagicMock
        import click
        from audible_deals.cli import _apply_config_defaults

        ctx = MagicMock()
        ctx.get_parameter_source.return_value = click.core.ParameterSource.DEFAULT
        ns = {"language": "", "narrator": ""}
        cfg = {"language": "french", "narrator": "Alice"}
        _apply_config_defaults(ctx, ns, cfg)
        assert ns["language"] == "french"
        assert ns["narrator"] == "Alice"

    def test_cli_string_overrides_both(self):
        """CLI-supplied string must not be overridden by config or profile."""
        from unittest.mock import MagicMock
        from audible_deals.cli import _apply_config_defaults, _apply_profile_defaults, _CL

        ctx = MagicMock()
        ctx.get_parameter_source.return_value = _CL
        ns = {"language": "spanish", "narrator": "Carlos"}
        cfg = {"language": "english", "narrator": "Alice"}
        profile = {"language": "french", "narrator": "Bob"}

        _apply_config_defaults(ctx, ns, cfg)
        assert ns["language"] == "spanish"
        assert ns["narrator"] == "Carlos"

        _apply_profile_defaults(ctx, ns, profile)
        assert ns["language"] == "spanish"
        assert ns["narrator"] == "Carlos"

    def test_profile_only_keys_applied(self):
        """Profile-only string keys (genre, keywords) are applied when CLI absent."""
        from unittest.mock import MagicMock
        import click
        from audible_deals.cli import _apply_profile_defaults

        ctx = MagicMock()
        ctx.get_parameter_source.return_value = click.core.ParameterSource.DEFAULT
        ns = {"genre": "", "keywords": "", "exclude_genre": (), "exclude_authors": ()}
        profile = {"genre": "mystery", "keywords": "thriller"}
        _apply_profile_defaults(ctx, ns, profile)
        assert ns["genre"] == "mystery"
        assert ns["keywords"] == "thriller"


# ===================================================================
# Fix: watch/notify record prices to history
# ===================================================================

class TestWatchRecordsPrices:
    """watch command must persist fetched prices to history."""

    def test_watch_records_prices(self, mock_client, tmp_config):
        """After running watch, history should contain an entry for the watched ASIN."""
        import audible_deals.cli as cli_mod
        from audible_deals.state import _load_price_history

        cli_mod._save_wishlist([
            {"asin": "WR1", "title": "Record Me", "max_price": 10.0, "added": ""},
        ])
        mock_client.get_products_batch.return_value = [
            make_product(asin="WR1", price=7.99, title="Record Me"),
        ]

        runner = CliRunner()
        result = runner.invoke(cli, ["watch"])
        assert result.exit_code == 0, result.output

        history = _load_price_history("WR1")
        assert len(history) == 1
        assert history[0]["price"] == 7.99


class TestNotifyRecordsPrices:
    """notify command must persist fetched prices to history."""

    def test_notify_records_prices(self, mock_client, tmp_config):
        """notify records prices for fetched items."""
        import audible_deals.cli as cli_mod
        from audible_deals.state import _load_price_history

        cli_mod._save_wishlist([
            {"asin": "NR1", "title": "Deal Book", "max_price": 5.0, "added": ""},
        ])
        mock_client.get_products_batch.return_value = [
            make_product(asin="NR1", price=3.99, title="Deal Book"),
        ]

        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0, result.output

        history = _load_price_history("NR1")
        assert len(history) == 1
        assert history[0]["price"] == 3.99
