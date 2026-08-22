"""Tests for library, series, and personalized catalog commands."""

from __future__ import annotations

import datetime
import json

from click.testing import CliRunner

import audible_deals.constants as constants_mod
from audible_deals.client import SeriesProductsBatch
from audible_deals.cli import cli
from tests.conftest import make_product


def _mock_library_pages(mock_client, products):
    """Set up get_library_pages mock yielding a single page."""
    mock_client.get_library_pages.return_value = iter([(products, 1)])


def _routes_setup_search_mock(mock_client, products):
    """Configure mock_client.search_pages to yield a single page of products."""
    mock_client.search_pages.return_value = iter([(products, 1, len(products))])


def _routes_setup_library_mock(mock_client, products):
    """Configure mock_client.get_library_pages to yield a single page."""
    mock_client.get_library_pages.return_value = iter([(products, 1)])


def _seed_profile_cache():
    profile = {
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "library_size": 10,
        "owned_asins": ["B00OWNED01"],
        "authors": [{"name": "Fav Author", "count": 4}],
        "narrators": [{"name": "Fav Narrator", "count": 3}],
        "genres": [{"id": "G1", "name": "Science Fiction", "count": 8}],
        "series": [{"name": "Bobiverse", "owned": 3, "series_asin": "SERIESA01"}],
    }
    constants_mod.TASTE_CACHE_FILE.write_text(json.dumps(profile))
    return profile


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
        result = runner.invoke(
            cli, ["library", "--author", "weir", "-q", "-o", str(out_file)]
        )
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
        result = runner.invoke(
            cli, ["library", "--narrator", "bray", "-q", "-o", str(out_file)]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LN1" in asins
        assert "LN2" not in asins

    def test_library_genre_filter(self, mock_client, tmp_config):
        """library --genre filters by category substring."""
        products = [
            make_product(
                asin="LG1", categories=["Science Fiction & Fantasy", "Fantasy"]
            ),
            make_product(asin="LG2", categories=["Mystery, Thriller & Suspense"]),
        ]
        _mock_library_pages(mock_client, products)
        out_file = tmp_config / "lib_genre.json"
        runner = CliRunner()
        result = runner.invoke(
            cli, ["library", "--genre", "science fiction", "-q", "-o", str(out_file)]
        )
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
        result = runner.invoke(
            cli, ["library", "--min-rating", "4.0", "-q", "-o", str(out_file)]
        )
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
        result = runner.invoke(
            cli, ["library", "--min-ratings", "100", "-q", "-o", str(out_file)]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LC1" in asins
        assert "LC2" not in asins

    def test_library_min_hours_filter(self, mock_client, tmp_config):
        """library --min-hours filters by length."""
        products = [
            make_product(asin="LH1", length_minutes=600),  # 10hrs
            make_product(asin="LH2", length_minutes=60),  # 1hr
        ]
        _mock_library_pages(mock_client, products)
        out_file = tmp_config / "lib_hours.json"
        runner = CliRunner()
        result = runner.invoke(
            cli, ["library", "--min-hours", "5", "-q", "-o", str(out_file)]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LH1" in asins
        assert "LH2" not in asins


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


class TestSeriesCommand:
    def test_series_direct_lookup(self, tmp_config, mock_client):
        """With series_asin, uses direct lookup via get_series_products."""
        lib = [
            make_product(
                asin="A1",
                title="Alpha Book 1",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
            ),
            make_product(
                asin="A2",
                title="Alpha Book 2",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
            ),
        ]
        mock_client.get_library.return_value = lib

        unowned = make_product(
            asin="A3", title="Alpha Book 3", series_name="Alpha Series"
        )
        mock_client.get_series_products.return_value = [lib[0], lib[1], unowned]

        runner = CliRunner()
        result = runner.invoke(cli, ["series"])
        assert result.exit_code == 0, result.output
        assert "Alpha Book 3" in result.output
        mock_client.get_series_products.assert_called_once_with("SER_ALPHA")
        mock_client.search_pages.assert_not_called()

    def test_series_reports_partial_lookup_failures(self, tmp_config, mock_client):
        lib = [
            make_product(
                asin="A1",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
            ),
            make_product(
                asin="A2",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
            ),
            make_product(
                asin="B1",
                series_name="Beta Series",
                series_asin="SER_BETA",
            ),
            make_product(
                asin="B2",
                series_name="Beta Series",
                series_asin="SER_BETA",
            ),
        ]
        beta = make_product(asin="B3", title="Beta Book 3", series_name="Beta Series")
        mock_client.get_library.return_value = lib
        mock_client.get_series_products.side_effect = [
            RuntimeError("temporary failure"),
            [lib[2], lib[3], beta],
        ]

        result = CliRunner().invoke(cli, ["series"])

        assert result.exit_code == 0, result.output
        assert "Partial results: 1/2 series scanned; 1 failed" in result.output
        assert "Alpha Series: RuntimeError: temporary failure" in result.output
        assert "Beta Book 3" in result.output

    def test_series_reports_missing_child_products(self, tmp_config, mock_client):
        lib = [
            make_product(
                asin="A1", series_name="Alpha Series", series_asin="SER_ALPHA"
            ),
            make_product(
                asin="A2", series_name="Alpha Series", series_asin="SER_ALPHA"
            ),
        ]
        mock_client.get_library.return_value = lib
        mock_client.get_series_products_many.side_effect = None
        mock_client.get_series_products_many.return_value = SeriesProductsBatch(
            products={"SER_ALPHA": tuple(lib)},
            failures={},
            missing_asins={"SER_ALPHA": ("A3",)},
        )

        result = CliRunner().invoke(cli, ["series"])

        assert result.exit_code == 0, result.output
        assert "1/1 series scanned; 1 incomplete" in result.output
        assert "Alpha Series: 1 product(s) unavailable" in result.output

    def test_series_keyword_fallback(self, tmp_config, mock_client):
        """Without series_asin, falls back to keyword search."""
        lib = [
            make_product(
                asin="A1",
                title="Alpha Book 1",
                series_name="Alpha Series",
                series_asin="",
            ),
            make_product(
                asin="A2",
                title="Alpha Book 2",
                series_name="Alpha Series",
                series_asin="",
            ),
        ]
        mock_client.get_library.return_value = lib

        unowned = make_product(
            asin="A3", title="Alpha Book 3", series_name="Alpha Series"
        )
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
            make_product(
                asin="A1",
                title="Alpha Book 1",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
            ),
            make_product(
                asin="A2",
                title="Alpha Book 2",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
            ),
            make_product(
                asin="B1",
                title="Beta Book 1",
                series_name="Beta Series",
                series_asin="SER_BETA",
            ),
            make_product(
                asin="B2",
                title="Beta Book 2",
                series_name="Beta Series",
                series_asin="SER_BETA",
            ),
        ]
        mock_client.get_library.return_value = lib

        unowned_alpha = make_product(
            asin="A3", title="Alpha Book 3", series_name="Alpha Series"
        )
        mock_client.get_series_products.return_value = [lib[0], lib[1], unowned_alpha]

        runner = CliRunner()
        result = runner.invoke(cli, ["series", "--series", "Alpha"])
        assert result.exit_code == 0, result.output

        mock_client.get_series_products.assert_called_once_with("SER_ALPHA")
        assert "Alpha Book 3" in result.output

    def test_series_skips_owned(self, tmp_config, mock_client):
        """Owned books from series lookup are excluded from output."""
        lib = [
            make_product(
                asin="A1",
                title="Alpha Book 1",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
            ),
            make_product(
                asin="A2",
                title="Alpha Book 2",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
            ),
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
            make_product(
                asin="A1",
                title="Alpha Book 1",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
            ),
            make_product(
                asin="A2",
                title="Alpha Book 2",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
            ),
        ]
        mock_client.get_library.return_value = lib

        unowned = make_product(
            asin="A3", title="Alpha Book 3", series_name="Alpha Series"
        )
        mock_client.get_series_products.return_value = [lib[0], lib[1], unowned]

        runner = CliRunner()
        result = runner.invoke(cli, ["series", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert any(item["asin"] == "A3" for item in data)

    def test_gaps_with_limit_is_error(self, tmp_config, mock_client):
        """series --gaps --limit/-n raises UsageError."""
        mock_client.get_library.return_value = []
        runner = CliRunner()
        result = runner.invoke(cli, ["series", "--gaps", "-n", "10"])
        assert result.exit_code != 0
        assert (
            "--limit" in result.output
            or "-n" in result.output
            or "ignored" in result.output
        )

    def test_gaps_with_sort_is_error(self, tmp_config, mock_client):
        """series --gaps --sort raises UsageError."""
        mock_client.get_library.return_value = []
        runner = CliRunner()
        result = runner.invoke(cli, ["series", "--gaps", "--sort", "price"])
        assert result.exit_code != 0
        assert "--sort" in result.output or "ignored" in result.output


class TestForMeDryRunNoSideEffects:
    def test_dry_run_no_cache_does_not_fetch_or_write(self, mock_client, tmp_config):
        # No cached profile: --dry-run must not hit the library API nor write
        # the taste cache; it should error telling the user to build first.
        runner = CliRunner()
        result = runner.invoke(cli, ["for-me", "--dry-run"])
        assert result.exit_code != 0
        assert "deals for-me" in result.output
        mock_client.get_library_pages.assert_not_called()
        assert not constants_mod.TASTE_CACHE_FILE.exists()

    def test_refresh_dry_run_does_not_fetch_or_overwrite(self, mock_client, tmp_config):
        # --refresh forces profile=None; combined with --dry-run it must still
        # make no API calls and must not overwrite the existing cache.
        seeded = _seed_profile_cache()
        runner = CliRunner()
        result = runner.invoke(cli, ["for-me", "--refresh", "--dry-run"])
        assert result.exit_code != 0
        mock_client.get_library_pages.assert_not_called()
        # The on-disk cache is untouched.
        assert json.loads(constants_mod.TASTE_CACHE_FILE.read_text()) == seeded

    def test_dry_run_with_cache_still_prints_plan(self, mock_client, tmp_config):
        _seed_profile_cache()
        runner = CliRunner()
        result = runner.invoke(cli, ["for-me", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "Bobiverse" in result.output
        mock_client.get_library_pages.assert_not_called()
        mock_client.search_pages.assert_not_called()

    def test_dry_run_json_reports_additional_series_batches(
        self, mock_client, tmp_config
    ):
        _seed_profile_cache()

        result = CliRunner().invoke(cli, ["for-me", "--dry-run", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["known_api_calls"] == 5
        assert payload["series"] == ["Bobiverse"]
        assert "additional" in payload["series_product_batches"]
        mock_client.get_library_pages.assert_not_called()


class TestProfileGenreCategoryOverride:
    def test_find_profile_genre_with_category_override(self, tmp_config, mock_client):
        """--category overrides a profile's genre instead of erroring out."""
        (tmp_config / "profiles.json").write_text(
            json.dumps({"scifi": {"genre": "sci-fi", "max_price": 5.0}})
        )
        _routes_setup_search_mock(mock_client, [make_product(asin="OV1", price=2.99)])
        mock_client.get_category_name.return_value = "Fantasy"
        result = CliRunner().invoke(
            cli,
            ["find", "--profile", "scifi", "--category", "18580606011"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        # The explicit category was resolved; the profile genre did not win.
        mock_client.get_category_name.assert_called_once_with("18580606011")
        mock_client.resolve_genre.assert_not_called()

    def test_search_profile_genre_with_category_override(self, tmp_config, mock_client):
        (tmp_config / "profiles.json").write_text(
            json.dumps({"scifi": {"genre": "sci-fi"}})
        )
        _routes_setup_search_mock(mock_client, [make_product(asin="OV2", price=2.99)])
        mock_client.get_category_name.return_value = "Fantasy"
        result = CliRunner().invoke(
            cli,
            ["search", "robots", "--profile", "scifi", "--category", "18580606011"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        mock_client.get_category_name.assert_called_once_with("18580606011")
        mock_client.resolve_genre.assert_not_called()

    def test_find_cli_genre_with_category_still_conflicts(
        self, tmp_config, mock_client
    ):
        """An explicit --genre on the CLI plus --category still errors out."""
        result = CliRunner().invoke(
            cli, ["find", "--genre", "sci-fi", "--category", "cat1"]
        )
        assert result.exit_code != 0
        assert "not both" in result.output

    def test_search_cli_genre_with_category_still_conflicts(
        self, tmp_config, mock_client
    ):
        result = CliRunner().invoke(
            cli, ["search", "test", "--genre", "sci-fi", "--category", "cat1"]
        )
        assert result.exit_code != 0
        assert "not both" in result.output


class TestLibraryJsonStats:
    def test_library_json_stats_emits_stats_object(self, tmp_config, mock_client):
        products = [
            make_product(
                asin="JS1",
                authors=["Jane Author"],
                narrators=["Nick Narrator"],
                categories=["Science Fiction"],
                length_minutes=600,
                rating=4.5,
            ),
            make_product(
                asin="JS2",
                authors=["Jane Author"],
                narrators=["Nick Narrator"],
                categories=["Science Fiction"],
                length_minutes=300,
                rating=4.0,
            ),
        ]
        _routes_setup_library_mock(mock_client, products)
        result = CliRunner().invoke(
            cli, ["library", "--json", "--stats"], catch_exceptions=False
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        # Stats object, not a product array.
        assert isinstance(payload, dict)
        assert payload["total_books"] == 2
        assert payload["total_hours"] == 15.0
        assert payload["top_authors"][0]["name"] == "Jane Author"
        assert payload["top_authors"][0]["count"] == 2

    def test_library_json_without_stats_still_emits_products(
        self, tmp_config, mock_client
    ):
        """--json alone keeps emitting the product list (no regression)."""
        products = [make_product(asin="JS3"), make_product(asin="JS4")]
        _routes_setup_library_mock(mock_client, products)
        result = CliRunner().invoke(cli, ["library", "--json"], catch_exceptions=False)
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert isinstance(payload, list)
        assert {p["asin"] for p in payload} == {"JS3", "JS4"}
