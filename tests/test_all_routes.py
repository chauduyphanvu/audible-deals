"""Empirical verification of every CLI command and route after refactoring.

Tests every command through the full Click invocation path to ensure the
module decomposition didn't break any imports, wiring, or behavior.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from click.testing import CliRunner

from audible_deals.cli import cli
from tests.conftest import make_product


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(runner, args, **kwargs):
    """Invoke the CLI and return the result; fail on unexpected errors."""
    result = runner.invoke(cli, args, catch_exceptions=False, **kwargs)
    return result


def _setup_search_mock(mock_client, products):
    """Configure mock_client.search_pages to yield a single page of products."""
    mock_client.search_pages.return_value = iter([(products, 1, len(products))])


def _setup_library_mock(mock_client, products):
    """Configure mock_client.get_library_pages to yield a single page."""
    mock_client.get_library_pages.return_value = iter([(products, 1)])


def _seed_last_results(tmp_config, products):
    """Write a last_results.json cache file."""
    from audible_deals.serialization import serialize_product as _serialize_product

    data = {
        "title": "Test Results",
        "results": [_serialize_product(p) for p in products],
    }
    (tmp_config / "last_results.json").write_text(json.dumps(data))


# ===========================================================================
# 1. Root group
# ===========================================================================


class TestRootGroup:
    def test_help(self, tmp_config):
        result = _run(CliRunner(), ["--help"])
        assert result.exit_code == 0
        assert "Audible deal finder" in result.output

    def test_version(self, tmp_config):
        result = _run(CliRunner(), ["--version"])
        assert result.exit_code == 0

    def test_no_subcommand_shows_dashboard(self, tmp_config):
        result = _run(CliRunner(), [])
        assert result.exit_code == 0
        assert "Authentication is not set up" in result.output
        assert "deals login" in result.output


# ===========================================================================
# 2. login / import-auth (auth commands — test help only, no real auth)
# ===========================================================================


class TestAuthCommands:
    def test_login_help(self, tmp_config):
        result = _run(CliRunner(), ["login", "--help"])
        assert result.exit_code == 0
        assert "--external" in result.output

    def test_import_auth_help(self, tmp_config):
        result = _run(CliRunner(), ["import-auth", "--help"])
        assert result.exit_code == 0
        assert "audible-cli" in result.output


# ===========================================================================
# 3. categories
# ===========================================================================


class TestCategoriesCommand:
    def test_categories_top_level(self, tmp_config, mock_client):
        mock_client.get_categories.return_value = [
            {"id": "cat1", "name": "Science Fiction & Fantasy"},
            {"id": "cat2", "name": "Mystery, Thriller & Suspense"},
        ]
        result = _run(CliRunner(), ["categories"])
        assert result.exit_code == 0
        assert "Science Fiction" in result.output

    def test_categories_with_parent(self, tmp_config, mock_client):
        mock_client.get_categories.return_value = [
            {"id": "sub1", "name": "Hard Science Fiction"},
        ]
        result = _run(CliRunner(), ["categories", "--parent", "cat1"])
        assert result.exit_code == 0
        assert "Hard Science Fiction" in result.output


# ===========================================================================
# 4. search
# ===========================================================================


class TestSearchCommand:
    def test_search_basic(self, tmp_config, mock_client):
        products = [make_product(asin="B001", title="Found Book", price=4.99)]
        _setup_search_mock(mock_client, products)
        mock_client.resolve_genre.return_value = ("cat1", "Science Fiction")
        result = _run(CliRunner(), ["search", "test query"])
        assert result.exit_code == 0
        assert "Found Book" in result.output

    def test_search_with_genre(self, tmp_config, mock_client):
        products = [make_product(asin="B002", title="Sci-Fi Book", price=3.99)]
        _setup_search_mock(mock_client, products)
        mock_client.resolve_genre.return_value = ("cat1", "Science Fiction")
        result = _run(CliRunner(), ["search", "query", "--genre", "sci-fi"])
        assert result.exit_code == 0

    def test_search_with_category(self, tmp_config, mock_client):
        products = [make_product(asin="B003", title="Cat Book", price=2.99)]
        _setup_search_mock(mock_client, products)
        mock_client.get_category_name.return_value = "Mystery"
        result = _run(CliRunner(), ["search", "query", "--category", "cat1"])
        assert result.exit_code == 0

    def test_search_json_output(self, tmp_config, mock_client):
        products = [make_product(asin="B004", title="JSON Book", price=5.99)]
        _setup_search_mock(mock_client, products)
        result = _run(CliRunner(), ["search", "test", "--json", "--quiet"])
        assert result.exit_code == 0
        # JSON output goes to stdout; progress bar goes to stderr via console redirect
        # Extract the JSON portion from output
        json_start = result.output.index("[")
        data = json.loads(result.output[json_start:])
        assert len(data) > 0

    def test_search_quiet(self, tmp_config, mock_client):
        products = [make_product(asin="B005", price=1.99)]
        _setup_search_mock(mock_client, products)
        result = _run(CliRunner(), ["search", "test", "--quiet"])
        assert result.exit_code == 0

    def test_search_csv_export(self, tmp_config, mock_client):
        products = [make_product(asin="B006", price=2.99)]
        _setup_search_mock(mock_client, products)
        out_path = tmp_config / "out.csv"
        result = _run(CliRunner(), ["search", "test", "-o", str(out_path)])
        assert result.exit_code == 0
        assert out_path.exists()

    def test_search_json_export(self, tmp_config, mock_client):
        products = [make_product(asin="B007", price=2.99)]
        _setup_search_mock(mock_client, products)
        out_path = tmp_config / "out.json"
        result = _run(CliRunner(), ["search", "test", "-o", str(out_path)])
        assert result.exit_code == 0
        assert out_path.exists()
        data = json.loads(out_path.read_text())
        assert len(data) > 0

    def test_search_deep(self, tmp_config, mock_client):
        products = [make_product(asin="B008", price=1.99)]
        _setup_search_mock(mock_client, products)
        result = _run(CliRunner(), ["search", "test", "--deep"])
        assert result.exit_code == 0

    def test_search_or_queries(self, tmp_config, mock_client):
        products = [make_product(asin="B009", price=3.99)]
        _setup_search_mock(mock_client, products)
        result = _run(CliRunner(), ["search", "query1 | query2"])
        assert result.exit_code == 0

    def test_search_dry_run(self, tmp_config, mock_client):
        result = _run(CliRunner(), ["search", "test", "--dry-run"])
        assert result.exit_code == 0
        assert "Dry run" in result.output

    def test_search_skip_owned(self, tmp_config, mock_client):
        products = [make_product(asin="B010", price=4.99)]
        _setup_search_mock(mock_client, products)
        mock_client.get_library_asins.return_value = set()
        result = _run(CliRunner(), ["search", "test", "--skip-owned"])
        assert result.exit_code == 0

    def test_search_exclude_seen(self, tmp_config, mock_client):
        products = [make_product(asin="B011", price=4.99)]
        _setup_search_mock(mock_client, products)
        result = _run(CliRunner(), ["search", "test", "--exclude-seen"])
        assert result.exit_code == 0

    def test_search_all_filters(self, tmp_config, mock_client):
        products = [
            make_product(
                asin="B012",
                price=2.99,
                rating=4.5,
                num_ratings=500,
                length_minutes=600,
                language="english",
            )
        ]
        _setup_search_mock(mock_client, products)
        result = _run(
            CliRunner(),
            [
                "search",
                "test",
                "--max-price",
                "5",
                "--min-rating",
                "4.0",
                "--min-ratings",
                "100",
                "--min-hours",
                "1",
                "--on-sale",
                "--min-discount",
                "10",
                "--sort",
                "price",
                "--limit",
                "10",
                "--show-url",
                "--first-in-series",
            ],
        )
        assert result.exit_code == 0

    def test_search_exclude_genre(self, tmp_config, mock_client):
        products = [make_product(asin="B013", price=2.99)]
        _setup_search_mock(mock_client, products)
        mock_client.resolve_genre.return_value = ("cat_exc", "Erotica")
        result = _run(CliRunner(), ["search", "test", "--exclude-genre", "erotica"])
        assert result.exit_code == 0

    def test_search_exclude_author(self, tmp_config, mock_client):
        products = [make_product(asin="B014", price=2.99, authors=["Good Author"])]
        _setup_search_mock(mock_client, products)
        result = _run(CliRunner(), ["search", "test", "--exclude-author", "Bad Author"])
        assert result.exit_code == 0

    def test_search_with_profile(self, tmp_config, mock_client):
        # Save a profile first
        profiles_file = tmp_config / "profiles.json"
        profiles_file.write_text(
            json.dumps({"test-profile": {"max_price": 5.0, "genre": "sci-fi"}})
        )
        products = [make_product(asin="B015", price=3.99)]
        _setup_search_mock(mock_client, products)
        mock_client.resolve_genre.return_value = ("cat1", "Science Fiction")
        result = _run(CliRunner(), ["search", "test", "--profile", "test-profile"])
        assert result.exit_code == 0

    def test_search_no_query_error(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["search"])
        assert result.exit_code != 0

    def test_search_max_pph(self, tmp_config, mock_client):
        products = [make_product(asin="B016", price=2.99, length_minutes=600)]
        _setup_search_mock(mock_client, products)
        result = _run(CliRunner(), ["search", "test", "--max-price-per-hour", "1.0"])
        assert result.exit_code == 0

    def test_search_author_filter(self, tmp_config, mock_client):
        products = [make_product(asin="B017", price=2.99, authors=["Andy Weir"])]
        _setup_search_mock(mock_client, products)
        result = _run(CliRunner(), ["search", "test", "--author", "Andy"])
        assert result.exit_code == 0

    def test_search_narrator_filter(self, tmp_config, mock_client):
        products = [make_product(asin="B018", price=2.99, narrators=["R.C. Bray"])]
        _setup_search_mock(mock_client, products)
        result = _run(CliRunner(), ["search", "test", "--narrator", "Bray"])
        assert result.exit_code == 0

    def test_search_series_filter(self, tmp_config, mock_client):
        products = [make_product(asin="B019", price=2.99, series_name="Expanse")]
        _setup_search_mock(mock_client, products)
        result = _run(CliRunner(), ["search", "test", "--series", "Expanse"])
        assert result.exit_code == 0


# ===========================================================================
# 5. find
# ===========================================================================


class TestFindCommand:
    def test_find_basic(self, tmp_config, mock_client):
        products = [make_product(asin="F001", title="Deal Book", price=3.99)]
        _setup_search_mock(mock_client, products)
        result = _run(CliRunner(), ["find"])
        assert result.exit_code == 0

    def test_find_with_genre(self, tmp_config, mock_client):
        products = [make_product(asin="F002", price=2.99)]
        _setup_search_mock(mock_client, products)
        mock_client.resolve_genre.return_value = ("cat1", "Science Fiction")
        result = _run(CliRunner(), ["find", "--genre", "sci-fi"])
        assert result.exit_code == 0

    def test_find_with_category(self, tmp_config, mock_client):
        products = [make_product(asin="F003", price=2.99)]
        _setup_search_mock(mock_client, products)
        mock_client.get_category_name.return_value = "Fantasy"
        result = _run(CliRunner(), ["find", "--category", "cat1"])
        assert result.exit_code == 0

    def test_find_with_keywords(self, tmp_config, mock_client):
        products = [make_product(asin="F004", price=2.99)]
        _setup_search_mock(mock_client, products)
        result = _run(CliRunner(), ["find", "--keywords", "space"])
        assert result.exit_code == 0

    def test_find_deep(self, tmp_config, mock_client):
        products = [make_product(asin="F005", price=1.99)]
        _setup_search_mock(mock_client, products)
        result = _run(CliRunner(), ["find", "--deep"])
        assert result.exit_code == 0

    def test_find_dry_run(self, tmp_config, mock_client):
        result = _run(CliRunner(), ["find", "--dry-run"])
        assert result.exit_code == 0
        assert "Dry run" in result.output

    def test_find_json_output(self, tmp_config, mock_client):
        products = [make_product(asin="F006", price=2.99)]
        _setup_search_mock(mock_client, products)
        result = _run(CliRunner(), ["find", "--json", "--quiet"])
        assert result.exit_code == 0
        json_start = result.output.index("[")
        data = json.loads(result.output[json_start:])
        assert isinstance(data, list)

    def test_find_all_filters(self, tmp_config, mock_client):
        products = [
            make_product(
                asin="F007", price=2.99, rating=4.5, num_ratings=200, length_minutes=480
            )
        ]
        _setup_search_mock(mock_client, products)
        result = _run(
            CliRunner(),
            [
                "find",
                "--max-price",
                "5",
                "--min-rating",
                "4.0",
                "--min-ratings",
                "50",
                "--min-hours",
                "1",
                "--sort",
                "discount",
                "--limit",
                "10",
                "--on-sale",
                "--first-in-series",
                "--show-url",
            ],
        )
        assert result.exit_code == 0

    def test_find_skip_owned(self, tmp_config, mock_client):
        products = [make_product(asin="F008", price=2.99)]
        _setup_search_mock(mock_client, products)
        mock_client.get_library_asins.return_value = set()
        result = _run(CliRunner(), ["find", "--skip-owned"])
        assert result.exit_code == 0

    def test_find_with_profile(self, tmp_config, mock_client):
        profiles_file = tmp_config / "profiles.json"
        profiles_file.write_text(
            json.dumps({"scifi": {"genre": "sci-fi", "max_price": 5.0}})
        )
        products = [make_product(asin="F009", price=3.99)]
        _setup_search_mock(mock_client, products)
        mock_client.resolve_genre.return_value = ("cat1", "Science Fiction")
        result = _run(CliRunner(), ["find", "--profile", "scifi"])
        assert result.exit_code == 0

    def test_find_csv_export(self, tmp_config, mock_client):
        products = [make_product(asin="F010", price=2.99)]
        _setup_search_mock(mock_client, products)
        out_path = tmp_config / "find_out.csv"
        result = _run(CliRunner(), ["find", "-o", str(out_path)])
        assert result.exit_code == 0
        assert out_path.exists()

    def test_find_exclude_genre(self, tmp_config, mock_client):
        products = [make_product(asin="F011", price=2.99)]
        _setup_search_mock(mock_client, products)
        mock_client.resolve_genre.return_value = ("cat_exc", "Erotica")
        result = _run(CliRunner(), ["find", "--exclude-genre", "erotica"])
        assert result.exit_code == 0


# ===========================================================================
# 6. detail
# ===========================================================================


class TestDetailCommand:
    def test_detail_by_asin(self, tmp_config, mock_client):
        p = make_product(asin="D001", title="Detail Book")
        mock_client.get_product.return_value = p
        result = _run(CliRunner(), ["detail", "D001"])
        assert result.exit_code == 0
        assert "Detail Book" in result.output

    def test_detail_by_last_ref(self, tmp_config, mock_client):
        products = [make_product(asin="D002", title="Last Ref Book")]
        _seed_last_results(tmp_config, products)
        mock_client.get_product.return_value = products[0]
        result = _run(CliRunner(), ["detail", "--last", "1"])
        assert result.exit_code == 0
        assert "Last Ref Book" in result.output

    def test_detail_no_asin_error(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["detail"])
        assert result.exit_code != 0


# ===========================================================================
# 7. open
# ===========================================================================


class TestOpenCommand:
    def test_open_by_asin(self, tmp_config, mock_client):
        with patch("audible_deals.cli.misc.click.launch") as mock_launch:
            result = _run(CliRunner(), ["open", "B00OPEN01"])
            assert result.exit_code == 0
            mock_launch.assert_called_once()
            assert "audible.com" in mock_launch.call_args[0][0]

    def test_open_by_last_ref(self, tmp_config, mock_client):
        products = [make_product(asin="B00OPEN02")]
        _seed_last_results(tmp_config, products)
        with patch("audible_deals.cli.misc.click.launch") as mock_launch:
            result = _run(CliRunner(), ["open", "--last", "1"])
            assert result.exit_code == 0
            mock_launch.assert_called_once()


# ===========================================================================
# 8. compare
# ===========================================================================


class TestCompareCommand:
    def test_compare_two_asins(self, tmp_config, mock_client):
        products = [
            make_product(asin="C001", title="Book A", price=5.99),
            make_product(asin="C002", title="Book B", price=3.99),
        ]
        mock_client.get_products_batch.return_value = products
        result = _run(CliRunner(), ["compare", "C001", "C002"])
        assert result.exit_code == 0
        assert "Book A" in result.output
        assert "Book B" in result.output

    def test_compare_with_last_refs(self, tmp_config, mock_client):
        products = [
            make_product(asin="C003", title="Ref A", price=5.99),
            make_product(asin="C004", title="Ref B", price=3.99),
        ]
        _seed_last_results(tmp_config, products)
        mock_client.get_products_batch.return_value = products
        result = _run(CliRunner(), ["compare", "--last", "1", "--last", "2"])
        assert result.exit_code == 0

    def test_compare_too_few_error(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["compare", "C005"])
        assert result.exit_code != 0


# ===========================================================================
# 9. library
# ===========================================================================


class TestLibraryCommand:
    def test_library_basic(self, tmp_config, mock_client):
        products = [make_product(asin="L001", title="My Book", price=14.99)]
        _setup_library_mock(mock_client, products)
        result = _run(CliRunner(), ["library"])
        assert result.exit_code == 0
        assert "My Book" in result.output

    def test_library_json(self, tmp_config, mock_client):
        products = [make_product(asin="L002", price=9.99)]
        _setup_library_mock(mock_client, products)
        result = _run(CliRunner(), ["library", "--json", "--quiet"])
        assert result.exit_code == 0
        json_start = result.output.index("[")
        data = json.loads(result.output[json_start:])
        assert len(data) == 1

    def test_library_with_filters(self, tmp_config, mock_client):
        products = [
            make_product(asin="L003", price=9.99, rating=4.5, authors=["Andy Weir"]),
            make_product(asin="L004", price=5.99, rating=3.0, authors=["Other"]),
        ]
        _setup_library_mock(mock_client, products)
        result = _run(
            CliRunner(), ["library", "--author", "Andy", "--min-rating", "4.0"]
        )
        assert result.exit_code == 0

    def test_library_export(self, tmp_config, mock_client):
        products = [make_product(asin="L005", price=9.99)]
        _setup_library_mock(mock_client, products)
        out_path = tmp_config / "library.json"
        result = _run(CliRunner(), ["library", "-o", str(out_path)])
        assert result.exit_code == 0
        assert out_path.exists()

    def test_library_sort(self, tmp_config, mock_client):
        products = [
            make_product(asin="L006", price=9.99, rating=4.0),
            make_product(asin="L007", price=5.99, rating=4.8),
        ]
        _setup_library_mock(mock_client, products)
        result = _run(CliRunner(), ["library", "--sort", "rating", "-n", "1"])
        assert result.exit_code == 0


# ===========================================================================
# 10. last
# ===========================================================================


class TestLastCommand:
    def test_last_basic(self, tmp_config, mock_client):
        products = [make_product(asin="LA01", title="Cached Book", price=4.99)]
        _seed_last_results(tmp_config, products)
        result = _run(CliRunner(), ["last"])
        assert result.exit_code == 0
        assert "Cached Book" in result.output

    def test_last_with_resort(self, tmp_config, mock_client):
        products = [
            make_product(asin="LA02", price=2.99),
            make_product(asin="LA03", price=1.99),
        ]
        _seed_last_results(tmp_config, products)
        result = _run(CliRunner(), ["last", "--sort", "price"])
        assert result.exit_code == 0

    def test_last_with_filters(self, tmp_config, mock_client):
        products = [
            make_product(asin="LA04", price=2.99, rating=4.5),
            make_product(asin="LA05", price=12.99, rating=3.0),
        ]
        _seed_last_results(tmp_config, products)
        result = _run(CliRunner(), ["last", "--max-price", "5", "--min-rating", "4.0"])
        assert result.exit_code == 0

    def test_last_json(self, tmp_config, mock_client):
        products = [make_product(asin="LA06", price=4.99)]
        _seed_last_results(tmp_config, products)
        result = _run(CliRunner(), ["last", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) > 0

    def test_last_count(self, tmp_config, mock_client):
        products = [make_product(asin="LA07"), make_product(asin="LA08")]
        _seed_last_results(tmp_config, products)
        result = _run(CliRunner(), ["last", "--count"])
        assert result.exit_code == 0
        assert "2" in result.output

    def test_last_clear(self, tmp_config, mock_client):
        products = [make_product(asin="LA09")]
        _seed_last_results(tmp_config, products)
        result = _run(CliRunner(), ["last", "--clear"])
        assert result.exit_code == 0
        assert not (tmp_config / "last_results.json").exists()

    def test_last_clear_seen(self, tmp_config, mock_client):
        (tmp_config / "seen_asins.json").write_text(json.dumps(["A1", "A2"]))
        result = _run(CliRunner(), ["last", "--clear-seen"])
        assert result.exit_code == 0

    def test_last_no_cache_error(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["last"])
        assert result.exit_code != 0

    def test_last_export(self, tmp_config, mock_client):
        products = [make_product(asin="LA10", price=3.99)]
        _seed_last_results(tmp_config, products)
        out_path = tmp_config / "last_out.json"
        result = _run(CliRunner(), ["last", "-o", str(out_path)])
        assert result.exit_code == 0
        assert out_path.exists()


# ===========================================================================
# 11. wishlist add / remove / list / sync
# ===========================================================================


class TestWishlistCommands:
    def test_wishlist_add(self, tmp_config, mock_client):
        p = make_product(asin="W001", title="Wish Book")
        mock_client.get_product.return_value = p
        result = _run(CliRunner(), ["wishlist", "add", "W001", "--max-price", "5"])
        assert result.exit_code == 0
        assert "Wish Book" in result.output

    def test_wishlist_add_with_last(self, tmp_config, mock_client):
        products = [make_product(asin="W002", title="Last Wish")]
        _seed_last_results(tmp_config, products)
        mock_client.get_product.return_value = products[0]
        result = _run(CliRunner(), ["wishlist", "add", "--last", "1"])
        assert result.exit_code == 0

    def test_wishlist_list_empty(self, tmp_config, mock_client):
        result = _run(CliRunner(), ["wishlist", "list"])
        assert result.exit_code == 0
        assert "empty" in result.output.lower()

    def test_wishlist_list_with_items(self, tmp_config, mock_client):
        wl = [{"asin": "W003", "title": "Listed Book", "max_price": 5.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        result = _run(CliRunner(), ["wishlist", "list"])
        assert result.exit_code == 0
        assert "Listed Book" in result.output

    def test_wishlist_remove(self, tmp_config, mock_client):
        wl = [{"asin": "W004", "title": "Remove Me", "max_price": None, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        result = _run(CliRunner(), ["wishlist", "remove", "W004"])
        assert result.exit_code == 0
        assert "1" in result.output  # "1 removed"

    def test_wishlist_sync(self, tmp_config, mock_client):
        mock_client.get_wishlist.return_value = [
            make_product(asin="W005", title="Synced Book"),
        ]
        result = _run(CliRunner(), ["wishlist", "sync"])
        assert result.exit_code == 0
        assert "Synced Book" in result.output

    def test_wishlist_sync_with_max_price(self, tmp_config, mock_client):
        mock_client.get_wishlist.return_value = [
            make_product(asin="W006", title="Priced Sync"),
        ]
        result = _run(CliRunner(), ["wishlist", "sync", "--max-price", "5"])
        assert result.exit_code == 0

    def test_wishlist_sync_update(self, tmp_config, mock_client):
        wl = [{"asin": "W007", "title": "Existing", "max_price": 10.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_wishlist.return_value = [
            make_product(asin="W007", title="Existing"),
        ]
        result = _run(CliRunner(), ["wishlist", "sync", "--max-price", "5", "--update"])
        assert result.exit_code == 0
        assert "1 updated" in result.output


# ===========================================================================
# 12. watch
# ===========================================================================


class TestWatchCommand:
    def test_watch_empty_wishlist(self, tmp_config, mock_client):
        result = _run(CliRunner(), ["watch"])
        assert result.exit_code == 0
        assert "empty" in result.output.lower()

    def test_watch_with_items(self, tmp_config, mock_client):
        wl = [{"asin": "WA01", "title": "Watch Book", "max_price": 5.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="WA01", title="Watch Book", price=3.99),
        ]
        result = _run(CliRunner(), ["watch"])
        assert result.exit_code == 0
        assert "BUY" in result.output

    def test_watch_buy_only(self, tmp_config, mock_client):
        wl = [
            {"asin": "WA02", "title": "Cheap", "max_price": 5.0, "added": ""},
            {"asin": "WA03", "title": "Expensive", "max_price": 2.0, "added": ""},
        ]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="WA02", title="Cheap", price=3.99),
            make_product(asin="WA03", title="Expensive", price=9.99),
        ]
        result = _run(CliRunner(), ["watch", "--buy-only"])
        assert result.exit_code == 0

    def test_watch_with_sort(self, tmp_config, mock_client):
        wl = [{"asin": "WA04", "title": "Sort Book", "max_price": 10.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="WA04", title="Sort Book", price=5.99),
        ]
        result = _run(CliRunner(), ["watch", "--sort", "title"])
        assert result.exit_code == 0

    def test_watch_show_url(self, tmp_config, mock_client):
        wl = [{"asin": "WA05", "title": "URL Book", "max_price": 10.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="WA05", title="URL Book", price=5.99),
        ]
        result = _run(CliRunner(), ["watch", "--show-url"])
        assert result.exit_code == 0


# ===========================================================================
# 13. profile save / list / delete / show
# ===========================================================================


class TestProfileCommands:
    def test_profile_save(self, tmp_config, mock_client):
        result = _run(
            CliRunner(),
            [
                "profile",
                "save",
                "my-scifi",
                "--genre",
                "sci-fi",
                "--max-price",
                "5",
                "--first-in-series",
            ],
        )
        assert result.exit_code == 0
        assert "my-scifi" in result.output

    def test_profile_list(self, tmp_config, mock_client):
        profiles = {"test-prof": {"genre": "sci-fi", "max_price": 5.0}}
        (tmp_config / "profiles.json").write_text(json.dumps(profiles))
        result = _run(CliRunner(), ["profile", "list"])
        assert result.exit_code == 0
        assert "test-prof" in result.output

    def test_profile_show(self, tmp_config, mock_client):
        profiles = {"show-prof": {"genre": "mystery", "on_sale": True}}
        (tmp_config / "profiles.json").write_text(json.dumps(profiles))
        result = _run(CliRunner(), ["profile", "show", "show-prof"])
        assert result.exit_code == 0
        assert "mystery" in result.output

    def test_profile_delete(self, tmp_config, mock_client):
        profiles = {"del-prof": {"genre": "romance"}}
        (tmp_config / "profiles.json").write_text(json.dumps(profiles))
        result = _run(CliRunner(), ["profile", "delete", "del-prof"])
        assert result.exit_code == 0
        assert "deleted" in result.output.lower()

    def test_profile_list_empty(self, tmp_config, mock_client):
        result = _run(CliRunner(), ["profile", "list"])
        assert result.exit_code == 0

    def test_profile_delete_nonexistent(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["profile", "delete", "nope"])
        assert result.exit_code != 0


# ===========================================================================
# 14. config set / get / list / reset
# ===========================================================================


class TestConfigCommands:
    def test_config_set(self, tmp_config, mock_client):
        result = _run(CliRunner(), ["config", "set", "max-price", "5"])
        assert result.exit_code == 0
        assert "max_price" in result.output

    def test_config_get(self, tmp_config, mock_client):
        cfg = {"max_price": 5.0}
        (tmp_config / "config.json").write_text(json.dumps(cfg))
        result = _run(CliRunner(), ["config", "get", "max-price"])
        assert result.exit_code == 0
        assert "5.0" in result.output

    def test_config_get_unset(self, tmp_config, mock_client):
        result = _run(CliRunner(), ["config", "get", "max-price"])
        assert result.exit_code == 0
        assert "not set" in result.output

    def test_config_list(self, tmp_config, mock_client):
        cfg = {"max_price": 5.0, "skip_owned": True}
        (tmp_config / "config.json").write_text(json.dumps(cfg))
        result = _run(CliRunner(), ["config", "list"])
        assert result.exit_code == 0
        assert "max_price" in result.output

    def test_config_list_empty(self, tmp_config, mock_client):
        result = _run(CliRunner(), ["config", "list"])
        assert result.exit_code == 0

    def test_config_reset_key(self, tmp_config, mock_client):
        cfg = {"max_price": 5.0}
        (tmp_config / "config.json").write_text(json.dumps(cfg))
        result = _run(CliRunner(), ["config", "reset", "max-price"])
        assert result.exit_code == 0
        assert "removed" in result.output.lower()

    def test_config_set_bool(self, tmp_config, mock_client):
        result = _run(CliRunner(), ["config", "set", "skip-owned", "true"])
        assert result.exit_code == 0

    def test_config_set_invalid_key(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["config", "set", "bad-key", "5"])
        assert result.exit_code != 0


# ===========================================================================
# 15. history
# ===========================================================================


class TestHistoryCommand:
    def test_history_with_data(self, tmp_config, mock_client):
        hist_dir = tmp_config / "history"
        hist_dir.mkdir()
        entries = [
            {"date": "2024-01-01", "price": 9.99, "title": "Hist Book"},
            {"date": "2024-01-15", "price": 4.99, "title": "Hist Book"},
        ]
        (hist_dir / "H001.json").write_text(
            json.dumps({"marketplaces": {"us": entries}})
        )
        result = _run(CliRunner(), ["history", "H001"])
        assert result.exit_code == 0
        assert "9.99" in result.output
        assert "4.99" in result.output

    def test_history_no_data(self, tmp_config, mock_client):
        result = _run(CliRunner(), ["history", "NODATA01"])
        assert result.exit_code == 0
        assert "No price history" in result.output

    def test_history_with_last_ref(self, tmp_config, mock_client):
        products = [make_product(asin="H002")]
        _seed_last_results(tmp_config, products)
        result = _run(CliRunner(), ["history", "--last", "1"])
        assert result.exit_code == 0

    def test_history_no_asin_error(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["history"])
        assert result.exit_code != 0

    def test_history_json_flag_emits_json(self, tmp_config, mock_client):
        hist_dir = tmp_config / "history"
        hist_dir.mkdir()
        entries = [{"date": "2024-01-01", "price": 9.99, "title": "Book"}]
        (hist_dir / "H003.json").write_text(
            json.dumps({"marketplaces": {"us": entries}})
        )
        result = _run(CliRunner(), ["history", "H003", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]["price"] == 9.99

    def test_history_json_empty_emits_empty_list(self, tmp_config, mock_client):
        result = _run(CliRunner(), ["history", "NODATA02", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output) == []

    def test_history_all_json_emits_all(self, tmp_config, mock_client):
        hist_dir = tmp_config / "history"
        hist_dir.mkdir()
        e1 = [{"date": "2024-01-01", "price": 1.0, "title": "A"}]
        e2 = [{"date": "2024-01-02", "price": 2.0, "title": "B"}]
        (hist_dir / "H004.json").write_text(json.dumps({"marketplaces": {"us": e1}}))
        (hist_dir / "H005.json").write_text(json.dumps({"marketplaces": {"us": e2}}))
        result = _run(CliRunner(), ["history", "--all", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, dict)
        assert "H004" in data and "H005" in data

    def test_history_all_without_json_errors(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["history", "--all"])
        assert result.exit_code != 0
        assert "--json" in result.output

    def test_history_all_with_asin_errors(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["history", "H006", "--all", "--json"])
        assert result.exit_code != 0

    def test_history_purge_dry_run(self, tmp_config, mock_client):
        import datetime as dt

        hist_dir = tmp_config / "history"
        hist_dir.mkdir()
        old_date = (dt.date.today() - dt.timedelta(days=200)).isoformat()
        entries = [{"date": old_date, "price": 5.0, "title": "Old"}]
        (hist_dir / "POLD01.json").write_text(
            json.dumps({"marketplaces": {"us": entries}})
        )
        result = _run(CliRunner(), ["history", "--purge-older-than", "90", "--dry-run"])
        assert result.exit_code == 0
        assert "Would remove" in result.output
        assert (hist_dir / "POLD01.json").exists()

    def test_history_purge_with_yes(self, tmp_config, mock_client):
        import datetime as dt

        hist_dir = tmp_config / "history"
        hist_dir.mkdir()
        old_date = (dt.date.today() - dt.timedelta(days=200)).isoformat()
        entries = [{"date": old_date, "price": 5.0, "title": "Old"}]
        (hist_dir / "POLD02.json").write_text(
            json.dumps({"marketplaces": {"us": entries}})
        )
        result = _run(CliRunner(), ["history", "--purge-older-than", "90", "--yes"])
        assert result.exit_code == 0
        assert "Removed" in result.output
        assert not (hist_dir / "POLD02.json").exists()

    def test_history_purge_confirmation_rechecks_before_deleting(
        self, tmp_config, mock_client, monkeypatch
    ):
        import datetime as dt
        import audible_deals.cli.history as history_cli_mod
        import audible_deals.price_history as history_mod

        hist_dir = tmp_config / "history"
        hist_dir.mkdir()
        old_date = (dt.date.today() - dt.timedelta(days=200)).isoformat()
        (hist_dir / "PRACE01.json").write_text(
            json.dumps({"marketplaces": {"us": [{"date": old_date, "price": 5.0}]}})
        )
        real_purge = history_cli_mod.purge_stale_history

        def _freshen_after_confirmation(days, dry_run=False, locale="us", asins=None):
            if asins is not None:
                history_mod.record_prices(
                    [make_product(asin="PRACE01", locale="us", price=4.0)]
                )
            return real_purge(days, dry_run=dry_run, locale=locale, asins=asins)

        monkeypatch.setattr(
            history_cli_mod, "purge_stale_history", _freshen_after_confirmation
        )

        result = _run(CliRunner(), ["history", "--purge-older-than", "90"], input="y\n")

        assert result.exit_code == 0
        assert "Removed 0" in result.output
        assert (
            history_mod.load_price_history("PRACE01", "us")[-1]["date"]
            == dt.date.today().isoformat()
        )

    def test_history_purge_combined_with_json_errors(self, tmp_config, mock_client):
        result = CliRunner().invoke(
            cli, ["history", "--purge-older-than", "90", "--json"]
        )
        assert result.exit_code != 0

    def test_history_purge_combined_with_asin_errors(self, tmp_config, mock_client):
        result = CliRunner().invoke(
            cli, ["history", "H007", "--purge-older-than", "90"]
        )
        assert result.exit_code != 0

    def test_history_purge_nothing_stale(self, tmp_config, mock_client):
        hist_dir = tmp_config / "history"
        hist_dir.mkdir()
        import datetime as dt

        fresh = dt.date.today().isoformat()
        entries = [{"date": fresh, "price": 5.0, "title": "New"}]
        (hist_dir / "FRESH02.json").write_text(
            json.dumps({"marketplaces": {"us": entries}})
        )
        result = _run(CliRunner(), ["history", "--purge-older-than", "90", "--dry-run"])
        assert result.exit_code == 0
        assert "No history" in result.output


# ===========================================================================
# 16. recap
# ===========================================================================


class TestRecapCommand:
    def test_recap_no_history(self, tmp_config, mock_client):
        result = _run(CliRunner(), ["recap"])
        assert result.exit_code == 0
        assert "No price history" in result.output

    def test_recap_with_data(self, tmp_config, mock_client):
        hist_dir = tmp_config / "history"
        hist_dir.mkdir()
        import datetime

        today = datetime.date.today().isoformat()
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        entries = [
            {"date": yesterday, "price": 9.99, "title": "Recap Book"},
            {"date": today, "price": 4.99, "title": "Recap Book"},
        ]
        (hist_dir / "R001.json").write_text(
            json.dumps({"marketplaces": {"us": entries}})
        )
        result = _run(CliRunner(), ["recap"])
        assert result.exit_code == 0

    def test_recap_with_days(self, tmp_config, mock_client):
        result = _run(CliRunner(), ["recap", "--days", "30"])
        assert result.exit_code == 0

    def test_recap_show_new(self, tmp_config, mock_client):
        hist_dir = tmp_config / "history"
        hist_dir.mkdir()
        import datetime

        today = datetime.date.today().isoformat()
        entries = [{"date": today, "price": 4.99, "title": "New Item"}]
        (hist_dir / "R002.json").write_text(
            json.dumps({"marketplaces": {"us": entries}})
        )
        result = _run(CliRunner(), ["recap", "--show-new"])
        assert result.exit_code == 0


# ===========================================================================
# 17. notify
# ===========================================================================


class TestNotifyCommand:
    def test_notify_empty_wishlist(self, tmp_config, mock_client):
        result = _run(CliRunner(), ["notify"])
        assert result.exit_code == 0
        assert "empty" in result.output.lower()

    def test_notify_no_hits(self, tmp_config, mock_client):
        wl = [{"asin": "N001", "title": "Notify Book", "max_price": 2.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="N001", price=9.99),
        ]
        result = _run(CliRunner(), ["notify"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 0

    def test_notify_with_hits(self, tmp_config, mock_client):
        wl = [{"asin": "N002", "title": "Deal Book", "max_price": 5.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="N002", price=3.99),
        ]
        result = _run(CliRunner(), ["notify"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 1


# ===========================================================================
# 18. completions
# ===========================================================================


class TestCompletionsCommand:
    def test_completions_bash(self, tmp_config):
        result = _run(CliRunner(), ["completions", "bash"])
        assert result.exit_code == 0

    def test_completions_zsh(self, tmp_config):
        result = _run(CliRunner(), ["completions", "zsh"])
        assert result.exit_code == 0

    def test_completions_fish(self, tmp_config):
        result = _run(CliRunner(), ["completions", "fish"])
        assert result.exit_code == 0


# ===========================================================================
# 19. Locale support
# ===========================================================================


class TestLocaleSupport:
    def test_locale_uk(self, tmp_config, mock_client):
        products = [make_product(asin="UK01", price=3.99)]
        _setup_search_mock(mock_client, products)
        result = _run(CliRunner(), ["--locale", "uk", "search", "test"])
        assert result.exit_code == 0

    def test_locale_de(self, tmp_config, mock_client):
        products = [make_product(asin="DE01", price=3.99)]
        _setup_search_mock(mock_client, products)
        result = _run(CliRunner(), ["--locale", "de", "search", "test"])
        assert result.exit_code == 0


# ===========================================================================
# 20. Error paths
# ===========================================================================


class TestErrorPaths:
    def test_search_genre_and_category_conflict(self, tmp_config, mock_client):
        result = CliRunner().invoke(
            cli, ["search", "test", "--genre", "sci-fi", "--category", "cat1"]
        )
        assert result.exit_code != 0

    def test_find_genre_and_category_conflict(self, tmp_config, mock_client):
        result = CliRunner().invoke(
            cli, ["find", "--genre", "sci-fi", "--category", "cat1"]
        )
        assert result.exit_code != 0

    def test_invalid_asin_in_detail(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["detail", "../../../etc/passwd"])
        assert result.exit_code != 0

    def test_invalid_asin_in_history(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["history", "../../bad"])
        assert result.exit_code != 0

    def test_wishlist_sync_update_without_max_price(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["wishlist", "sync", "--update"])
        assert result.exit_code != 0

    def test_wishlist_add_no_asins(self, tmp_config, mock_client):
        """wishlist add with no ASINs or --last should error."""
        result = CliRunner().invoke(cli, ["wishlist", "add"])
        assert result.exit_code != 0


# ===========================================================================
# 21. Zero-length item filtering (Feature C)
# ===========================================================================


class TestZeroLengthFiltering:
    def test_zero_length_excluded_from_find(self, tmp_config, mock_client):
        """find drops products with length_minutes==0 and records 'no runtime' breakdown."""
        products = [
            make_product(asin="NR1", length_minutes=0, price=3.99),
            make_product(asin="NR2", length_minutes=600, price=3.99),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "zero_length.json"
        result = _run(
            CliRunner(),
            [
                "find",
                "--pages",
                "1",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "NR2" in asins
        assert "NR1" not in asins

    def test_zero_length_breakdown_label(self, tmp_config, mock_client):
        """The breakdown for zero-length items uses the label 'no runtime'."""
        from audible_deals.cli.pipeline import _apply_filters

        products = [
            make_product(asin="NR3", length_minutes=0, price=3.99),
            make_product(asin="NR4", length_minutes=300, price=3.99),
        ]
        _, breakdown, _, _, _ = _apply_filters(
            products,
            max_price=None,
            min_rating=0.0,
            min_hours=0.0,
            language="",
            on_sale=False,
            skip_asins=None,
            exclude_category_ids=set(),
            first_in_series_only=False,
            sort="price",
        )
        assert breakdown.get("no runtime") == 1

    def test_library_keeps_zero_length(self, tmp_config, mock_client):
        """library does NOT drop zero-length items — they are kept as-is."""
        products = [
            make_product(asin="LZ1", length_minutes=0, price=0.0),
            make_product(asin="LZ2", length_minutes=600, price=0.0),
        ]
        _setup_library_mock(mock_client, products)
        result = _run(
            CliRunner(),
            ["library", "--json", "--quiet"],
        )
        assert result.exit_code == 0, result.output
        json_start = result.output.index("[")
        data = json.loads(result.output[json_start:])
        asins = [d["asin"] for d in data]
        assert "LZ1" in asins
        assert "LZ2" in asins

    def test_series_keeps_zero_length_preorder(self, tmp_config, mock_client):
        """series route keeps zero-length (pre-order) products; find still drops them."""
        # Two library books in the same series so the user is "invested"
        owned1 = make_product(
            asin="SO1",
            series_name="Epic Arc",
            series_asin="SARC1",
            length_minutes=600,
            price=0.0,
        )
        owned2 = make_product(
            asin="SO2",
            series_name="Epic Arc",
            series_asin="SARC1",
            length_minutes=600,
            price=0.0,
        )
        # A pre-order with length_minutes==0 that should survive the series pipeline
        preorder = make_product(
            asin="SPRE1",
            series_name="Epic Arc",
            series_asin="SARC1",
            length_minutes=0,
            price=14.99,
        )
        mock_client.get_library.return_value = [owned1, owned2]
        mock_client.get_series_products.return_value = [preorder]

        result = _run(
            CliRunner(),
            ["series", "--min-books", "2", "--json", "--quiet", "--limit", "0"],
        )
        assert result.exit_code == 0, result.output
        json_start = result.output.index("[")
        data = json.loads(result.output[json_start:])
        asins = [d["asin"] for d in data]
        assert "SPRE1" in asins, "pre-order should not be dropped by series command"

    def test_find_still_drops_zero_length(self, tmp_config, mock_client):
        """find route continues to drop zero-length products after drop_zero_length param added."""
        products = [
            make_product(asin="FNR1", length_minutes=0, price=3.99),
            make_product(asin="FNR2", length_minutes=600, price=3.99),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "find_zero.json"
        result = _run(
            CliRunner(),
            [
                "find",
                "--pages",
                "1",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "FNR2" in asins
        assert "FNR1" not in asins


# ===========================================================================
# Feature D: --exit-code on notify and watch
# ===========================================================================


class TestNotifyExitCode:
    def test_no_hits_with_flag_exits_1(self, tmp_config, mock_client):
        wl = [{"asin": "EC01", "title": "Book", "max_price": 2.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="EC01", price=9.99),
        ]
        result = CliRunner().invoke(cli, ["notify", "--exit-code"])
        assert result.exit_code == 1

    def test_with_hit_and_flag_exits_0(self, tmp_config, mock_client):
        wl = [{"asin": "EC02", "title": "Book", "max_price": 5.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="EC02", price=3.99),
        ]
        result = CliRunner().invoke(cli, ["notify", "--exit-code"])
        assert result.exit_code == 0

    def test_no_flag_no_hits_exits_0(self, tmp_config, mock_client):
        wl = [{"asin": "EC03", "title": "Book", "max_price": 2.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="EC03", price=9.99),
        ]
        result = CliRunner().invoke(cli, ["notify"])
        assert result.exit_code == 0


class TestWatchExitCode:
    def test_no_hits_with_flag_exits_1(self, tmp_config, mock_client):
        wl = [{"asin": "WEC1", "title": "Book", "max_price": 2.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="WEC1", price=9.99),
        ]
        result = CliRunner().invoke(cli, ["watch", "--exit-code"])
        assert result.exit_code == 1

    def test_with_hit_and_flag_exits_0(self, tmp_config, mock_client):
        wl = [{"asin": "WEC2", "title": "Book", "max_price": 5.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="WEC2", price=3.99),
        ]
        result = CliRunner().invoke(cli, ["watch", "--exit-code"])
        assert result.exit_code == 0

    def test_exit_code_with_every_is_usage_error(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["watch", "--exit-code", "--every", "5m"])
        assert result.exit_code != 0
        assert "exit-code" in result.output.lower() or "every" in result.output.lower()


# ===========================================================================
# Feature E: interactive c/h verbs
# ===========================================================================


class TestInteractiveBrowseVerbs:
    def _setup(self, tmp_config, monkeypatch):
        import audible_deals.display as display_mod
        from io import StringIO
        from rich.console import Console

        test_console = Console(file=StringIO(), force_terminal=False)
        monkeypatch.setattr(display_mod, "console", test_console)
        import audible_deals.cli.interactive as interactive_mod

        monkeypatch.setattr(interactive_mod, "console", test_console)
        return test_console

    def test_compare_shows_both_titles(self, tmp_config, monkeypatch):
        from audible_deals.cli.interactive import _interactive_browse

        p1 = make_product(asin="IA01", title="Alpha Book", price=5.0)
        p2 = make_product(asin="IA02", title="Beta Book", price=8.0)
        inputs = iter(["c 1 2", "q"])
        monkeypatch.setattr("click.prompt", lambda *a, **kw: next(inputs))
        console = self._setup(tmp_config, monkeypatch)
        _interactive_browse([p1, p2])
        out = console.file.getvalue()
        assert "Alpha Book" in out
        assert "Beta Book" in out

    def test_history_no_entries_prints_message(self, tmp_config, monkeypatch):
        from audible_deals.cli.interactive import _interactive_browse
        import audible_deals.cli.interactive as interactive_mod

        monkeypatch.setattr(
            interactive_mod, "load_price_history", lambda asin, locale: []
        )
        p = make_product(asin="IH01", title="History Book", price=5.0)
        inputs = iter(["h 1", "q"])
        monkeypatch.setattr("click.prompt", lambda *a, **kw: next(inputs))
        console = self._setup(tmp_config, monkeypatch)
        _interactive_browse([p])
        out = console.file.getvalue()
        assert "IH01" in out
        assert "No price history" in out

    def test_compare_missing_second_index_prints_error(self, tmp_config, monkeypatch):
        from audible_deals.cli.interactive import _interactive_browse

        p = make_product(asin="IC01", title="Only Book", price=5.0)
        inputs = iter(["c 1", "q"])
        monkeypatch.setattr("click.prompt", lambda *a, **kw: next(inputs))
        console = self._setup(tmp_config, monkeypatch)
        _interactive_browse([p])
        out = console.file.getvalue()
        assert "Invalid input" in out


# ===========================================================================
# Feature F: library --stats
# ===========================================================================


class TestLibraryStats:
    def test_library_stats_shows_headline(self, tmp_config, mock_client):
        products = [
            make_product(
                asin="LS01",
                title="Stats Book",
                authors=["Jane Author"],
                length_minutes=600,
                rating=4.5,
            ),
            make_product(
                asin="LS02",
                title="Another Book",
                authors=["Jane Author"],
                length_minutes=300,
                rating=4.0,
            ),
        ]
        _setup_library_mock(mock_client, products)
        result = _run(CliRunner(), ["library", "--stats"])
        assert result.exit_code == 0
        assert "2" in result.output  # total books
        assert "15" in result.output  # total hours (600+300 = 900 min = 15 h)

    def test_library_stats_shows_top_author(self, tmp_config, mock_client):
        products = [
            make_product(asin="LS03", authors=["Repeated Author"], length_minutes=300),
            make_product(asin="LS04", authors=["Repeated Author"], length_minutes=300),
            make_product(asin="LS05", authors=["Other Author"], length_minutes=300),
        ]
        _setup_library_mock(mock_client, products)
        result = _run(CliRunner(), ["library", "--stats"])
        assert result.exit_code == 0
        assert "Repeated Author" in result.output


# ===========================================================================
# Feature: series --gaps
# ===========================================================================


class TestParseSeriesPosition:
    """Unit tests for the parse_series_position helper in utils."""

    def test_plain_integer(self):
        from audible_deals.parsing import parse_series_position

        assert parse_series_position("2") == 2.0

    def test_decimal(self):
        from audible_deals.parsing import parse_series_position

        assert parse_series_position("2.5") == 2.5

    def test_range_picks_first(self):
        from audible_deals.parsing import parse_series_position

        assert parse_series_position("1-3") == 1.0

    def test_prefixed(self):
        from audible_deals.parsing import parse_series_position

        assert parse_series_position("Book 2") == 2.0

    def test_empty_string_goes_last(self):
        from audible_deals.parsing import parse_series_position

        assert parse_series_position("") == float("inf")

    def test_unparseable_goes_last(self):
        from audible_deals.parsing import parse_series_position

        assert parse_series_position("Prequel") == float("inf")

    def test_sort_order(self):
        from audible_deals.parsing import parse_series_position

        positions = ["3", "1-3", "2.5", "Book 10", "", "Prequel"]
        sorted_pos = sorted(positions, key=parse_series_position)
        assert sorted_pos[:4] == ["1-3", "2.5", "3", "Book 10"]
        # "" and "Prequel" go last (both inf), order between them doesn't matter
        assert set(sorted_pos[4:]) == {"", "Prequel"}


class TestSeriesGapsMode:
    """CLI-level tests for series --gaps."""

    def _make_library(self):
        """Two books owned in 'The Expanse' series."""
        owned1 = make_product(
            asin="EXP001",
            title="Leviathan Wakes",
            series_name="The Expanse",
            series_asin="EXPANSE1",
            series_position="1",
            price=0.0,
            length_minutes=900,
        )
        owned2 = make_product(
            asin="EXP003",
            title="Abaddon's Gate",
            series_name="The Expanse",
            series_asin="EXPANSE1",
            series_position="3",
            price=0.0,
            length_minutes=900,
        )
        return [owned1, owned2]

    def _make_catalog(self):
        """Catalog returns book 2 (not owned) and the two owned (should be excluded)."""
        owned1 = make_product(
            asin="EXP001",
            series_name="The Expanse",
            series_position="1",
            price=0.0,
        )
        missing = make_product(
            asin="EXP002",
            title="Caliban's War",
            series_name="The Expanse",
            series_position="2",
            price=24.95,
            length_minutes=900,
        )
        owned2 = make_product(
            asin="EXP003",
            series_name="The Expanse",
            series_position="3",
            price=0.0,
        )
        return [owned1, missing, owned2]

    def test_gaps_basic_terminal_output(self, tmp_config, mock_client):
        """Gaps mode runs without error; missing book appears in JSON for verification."""
        mock_client.get_library.return_value = self._make_library()
        mock_client.get_series_products.return_value = self._make_catalog()

        # Terminal display goes through Rich console (not captured by CliRunner).
        # Verify the command succeeds and produces correct JSON in a parallel call.
        result = _run(
            CliRunner(),
            ["series", "--gaps", "--min-books", "2"],
        )
        assert result.exit_code == 0, result.output

        # JSON confirms the gap report content
        result_json = _run(
            CliRunner(),
            ["series", "--gaps", "--json", "--quiet", "--min-books", "2"],
        )
        assert result_json.exit_code == 0, result_json.output
        json_start = result_json.output.index("[")
        data = json.loads(result_json.output[json_start:])
        titles = [m["title"] for entry in data for m in entry["missing"]]
        assert "Caliban's War" in titles

    def test_gaps_json_shape(self, tmp_config, mock_client):
        """--gaps --json emits correct per-series structure."""
        mock_client.get_library.return_value = self._make_library()
        mock_client.get_series_products.return_value = self._make_catalog()

        result = _run(
            CliRunner(),
            ["series", "--gaps", "--json", "--quiet", "--min-books", "2"],
        )
        assert result.exit_code == 0, result.output
        # JSON output starts with '['
        json_start = result.output.index("[")
        data = json.loads(result.output[json_start:])
        assert len(data) == 1
        entry = data[0]
        assert entry["series"] == "The Expanse"
        assert entry["owned"] == 2
        assert entry["total_known"] == 3  # 2 owned + 1 missing
        assert len(entry["missing"]) == 1
        m = entry["missing"][0]
        assert m["asin"] == "EXP002"
        assert m["title"] == "Caliban's War"
        assert m["position"] == "2"
        assert m["price"] == 24.95

    def test_gaps_with_output_raises_usage_error(self, tmp_config, mock_client):
        """--gaps + --output is a UsageError."""
        result = CliRunner().invoke(
            cli,
            ["series", "--gaps", "--output", str(tmp_config / "out.json")],
        )
        assert result.exit_code != 0
        assert (
            "not compatible" in result.output.lower()
            or "usage" in result.output.lower()
        )

    def test_gaps_with_interactive_raises_usage_error(self, tmp_config, mock_client):
        """--gaps + --interactive is a UsageError."""
        result = CliRunner().invoke(
            cli,
            ["series", "--gaps", "--interactive"],
        )
        assert result.exit_code != 0
        assert (
            "not compatible" in result.output.lower()
            or "usage" in result.output.lower()
        )

    def test_gaps_series_with_all_owned_omitted(self, tmp_config, mock_client):
        """Series where catalog == owned books has no missing and is omitted."""
        # Own 2 books; catalog returns only the 2 owned books (no new candidates)
        owned1 = make_product(
            asin="OM001",
            series_name="Complete Series",
            series_asin="CSER1",
            series_position="1",
            price=0.0,
            length_minutes=600,
        )
        owned2 = make_product(
            asin="OM002",
            series_name="Complete Series",
            series_asin="CSER1",
            series_position="2",
            price=0.0,
            length_minutes=600,
        )
        mock_client.get_library.return_value = [owned1, owned2]
        # Catalog has the same books already owned — no new candidates
        mock_client.get_series_products.return_value = [owned1, owned2]

        result = _run(
            CliRunner(),
            ["series", "--gaps", "--json", "--quiet", "--min-books", "2"],
        )
        assert result.exit_code == 0, result.output
        json_start = result.output.index("[")
        data = json.loads(result.output[json_start:])
        assert data == [], "All-owned series should be omitted from gaps output"

    def test_gaps_missing_sorted_by_position(self, tmp_config, mock_client):
        """Missing books within a series are sorted by numeric position."""
        owned = make_product(
            asin="SRT001",
            series_name="Sort Series",
            series_asin="SORT1",
            series_position="1",
            price=0.0,
            length_minutes=600,
        )
        owned2 = make_product(
            asin="SRT002",
            series_name="Sort Series",
            series_asin="SORT1",
            series_position="2",
            price=0.0,
            length_minutes=600,
        )
        miss5 = make_product(
            asin="SRTM5",
            title="Book Five",
            series_name="Sort Series",
            series_position="5",
            price=9.99,
            length_minutes=600,
        )
        miss3 = make_product(
            asin="SRTM3",
            title="Book Three",
            series_name="Sort Series",
            series_position="3",
            price=7.99,
            length_minutes=600,
        )
        mock_client.get_library.return_value = [owned, owned2]
        mock_client.get_series_products.return_value = [owned, owned2, miss5, miss3]

        result = _run(
            CliRunner(),
            ["series", "--gaps", "--json", "--quiet", "--min-books", "2"],
        )
        assert result.exit_code == 0, result.output
        json_start = result.output.index("[")
        data = json.loads(result.output[json_start:])
        assert len(data) == 1
        missing = data[0]["missing"]
        assert len(missing) == 2
        assert missing[0]["asin"] == "SRTM3"  # position 3 sorts before 5
        assert missing[1]["asin"] == "SRTM5"
