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
    _export_products,
    _filter_products,
    _first_in_series,
    _price_per_hour,
    _serialize_product,
    _sort_local,
    cli,
)
from audible_deals.client import Product
from tests.conftest import make_product


# ===================================================================
# _filter_products
# ===================================================================

class TestFilterProducts:
    def test_max_price(self, products_for_filtering):
        filtered, excluded = _filter_products(products_for_filtering, max_price=5.0)
        assert all(p.price is not None and p.price <= 5.0 for p in filtered)
        assert excluded > 0

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
        assert all(p.discount_pct is not None and p.discount_pct > 0 for p in filtered)

    def test_skip_asins(self, products_for_filtering):
        filtered, _ = _filter_products(products_for_filtering, skip_asins={"CHEAP1", "CHEAP2"})
        assert not any(p.asin in {"CHEAP1", "CHEAP2"} for p in filtered)

    def test_exclude_category_ids(self, products_for_filtering):
        filtered, _ = _filter_products(
            products_for_filtering, exclude_category_ids={"cat_erotica"}
        )
        assert not any(p.asin == "EROTICA" for p in filtered)

    def test_no_filters(self, products_for_filtering):
        filtered, excluded = _filter_products(products_for_filtering)
        assert len(filtered) == len(products_for_filtering)
        assert excluded == 0

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
        products = [
            make_product(asin="A", series_name="S1", series_position="2"),
            make_product(asin="B", series_name="S2", series_position="3"),
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
            "audible_deals.cli.socket.getaddrinfo",
            lambda host, port: [(socket.AF_INET, 0, 0, "", ("10.0.0.1", 0))],
        )
        with pytest.raises(click.BadParameter, match="non-public"):
            _validate_webhook_url("https://internal.corp/hook")

    def test_rejects_link_local(self, monkeypatch):
        import socket
        monkeypatch.setattr(
            "audible_deals.cli.socket.getaddrinfo",
            lambda host, port: [(socket.AF_INET, 0, 0, "", ("169.254.169.254", 0))],
        )
        with pytest.raises(click.BadParameter, match="non-public"):
            _validate_webhook_url("https://metadata.internal/hook")

    def test_accepts_public_ip(self, monkeypatch):
        import socket
        monkeypatch.setattr(
            "audible_deals.cli.socket.getaddrinfo",
            lambda host, port: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
        )
        _validate_webhook_url("https://example.com/hook")  # should not raise

    def test_rejects_unresolvable_host(self, monkeypatch):
        import socket
        monkeypatch.setattr(
            "audible_deals.cli.socket.getaddrinfo",
            lambda host, port: (_ for _ in ()).throw(socket.gaierror("Name not resolved")),
        )
        with pytest.raises(click.BadParameter, match="Cannot resolve"):
            _validate_webhook_url("https://nonexistent.invalid/hook")
