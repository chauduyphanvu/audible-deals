"""Tests for library, series, and personalized catalog commands."""

from __future__ import annotations

import datetime
import json

from click.testing import CliRunner

import audible_deals.constants as constants_mod
from audible_deals.client import SeriesProductsBatch
from audible_deals.cli import cli
from audible_deals.results_cache import load_result_session, save_dismissed_asins
from audible_deals.series_identity import (
    group_series_books,
    series_book_identity,
    series_identity,
)
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
        "version": 2,
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "library_size": 10,
        "owned_asins": ["B00OWNED01"],
        "authors": [{"name": "Fav Author", "count": 4}],
        "narrators": [{"name": "Fav Narrator", "count": 3}],
        "genres": [{"id": "G1", "name": "Science Fiction", "count": 8}],
        "series": [
            {
                "name": "Bobiverse",
                "owned": 3,
                "series_asin": "SERIESA01",
                "books": [
                    {"asin": "B00OWNED01", "title": "Bobiverse 1", "position": "1"},
                    {"asin": "B00OWNED02", "title": "Bobiverse 2", "position": "2"},
                    {"asin": "B00OWNED03", "title": "Bobiverse 3", "position": "3"},
                ],
            }
        ],
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


class TestSeriesIdentity:
    def test_series_identity_prefers_normalized_asin_then_name(self):
        with_asin = make_product(
            series_asin="  SER\uff3fALPHA  ", series_name="Ignored Name"
        )
        by_name = make_product(series_asin="", series_name="  Alpha\u3000 Series  ")
        without_series = make_product(series_asin="", series_name="")

        assert series_identity(with_asin) == "ser_alpha"
        assert series_identity(by_name) == "alpha series"
        assert series_identity(without_series) is None

    def test_numeric_positions_share_an_identity(self):
        identities = {
            series_book_identity(make_product(series_position=position))
            for position in ("1", "1.0", "Book 1")
        }

        assert identities == {"1"}

    def test_missing_and_ambiguous_positions_fall_back_to_normalized_title(self):
        variants = [
            make_product(title="  The\u3000Omnibus  ", series_position=""),
            make_product(title="the omnibus", series_position="1-3"),
            make_product(title="THE OMNIBUS", series_position="Books 1 & 3"),
        ]

        assert {series_book_identity(product) for product in variants} == {
            "the omnibus"
        }

    def test_group_series_books_deduplicates_and_preserves_first_edition(self):
        first = make_product(
            asin="FIRST",
            series_asin="SER_ALPHA",
            series_position="Book 1",
        )
        alternate = make_product(
            asin="ALTERNATE",
            series_asin="ser_alpha",
            series_position="1.0",
        )
        second = make_product(
            asin="SECOND",
            series_asin="SER_ALPHA",
            series_position="2",
        )
        by_name = make_product(
            asin="BY_NAME",
            series_asin="",
            series_name=" Name\u3000Fallback ",
            series_position="1",
        )

        groups = group_series_books([first, alternate, second, by_name])

        assert groups == {
            "ser_alpha": [first, second],
            "name fallback": [by_name],
        }


class TestSeriesCommand:
    def test_series_excludes_dismissed_from_flat_and_retains_cached_candidate(
        self, tmp_config, mock_client
    ):
        lib = [
            make_product(
                asin="SDOWN1",
                series_name="Dismissed Series",
                series_position="1",
                series_asin="SER_DISMISSED",
            ),
            make_product(
                asin="SDOWN2",
                series_name="Dismissed Series",
                series_position="2",
                series_asin="SER_DISMISSED",
            ),
        ]
        dismissed = make_product(
            asin="SDISMISS",
            title="Dismiss Me",
            series_name="Dismissed Series",
            series_position="3",
            series_asin="SER_DISMISSED",
        )
        visible = make_product(
            asin="SKEEP",
            title="Keep Me",
            series_name="Dismissed Series",
            series_position="4",
            series_asin="SER_DISMISSED",
        )
        mock_client.get_library.return_value = lib
        mock_client.get_series_products.return_value = [dismissed, visible]
        save_dismissed_asins({dismissed.asin})

        result = CliRunner().invoke(cli, ["series", "--json"])

        assert result.exit_code == 0, result.output
        assert [item["asin"] for item in json.loads(result.stdout)] == [visible.asin]
        session = load_result_session()
        assert {item["asin"] for item in session.candidates} == {
            dismissed.asin,
            visible.asin,
        }
        assert session.constraints["always_skip_asins"] == [dismissed.asin]

    def test_series_gaps_excludes_dismissed(self, tmp_config, mock_client):
        lib = [
            make_product(
                asin=f"GDOWN{number}",
                series_name="Gap Series",
                series_position=str(number),
                series_asin="SER_GAPS",
            )
            for number in (1, 2)
        ]
        dismissed = make_product(
            asin="GDISMISS",
            title="Dismissed Gap",
            series_name="Gap Series",
            series_position="3",
            series_asin="SER_GAPS",
        )
        mock_client.get_library.return_value = lib
        mock_client.get_series_products.return_value = [dismissed]
        save_dismissed_asins({dismissed.asin})

        result = CliRunner().invoke(cli, ["series", "--gaps", "--json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == []

    def test_series_direct_lookup(self, tmp_config, mock_client):
        """With series_asin, uses direct lookup via get_series_products."""
        lib = [
            make_product(
                asin="A1",
                title="Alpha Book 1",
                series_name="Alpha Series",
                series_position="1",
                series_asin="SER_ALPHA",
            ),
            make_product(
                asin="A2",
                title="Alpha Book 2",
                series_name="Alpha Series",
                series_position="2",
                series_asin="SER_ALPHA",
            ),
        ]
        mock_client.get_library.return_value = lib

        unowned = make_product(
            asin="A3",
            title="Alpha Book 3",
            series_name="Alpha Series",
            series_position="3",
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
                series_position="1",
                series_asin="SER_ALPHA",
            ),
            make_product(
                asin="A2",
                series_name="Alpha Series",
                series_position="2",
                series_asin="SER_ALPHA",
            ),
            make_product(
                asin="B1",
                series_name="Beta Series",
                series_position="1",
                series_asin="SER_BETA",
            ),
            make_product(
                asin="B2",
                series_name="Beta Series",
                series_position="2",
                series_asin="SER_BETA",
            ),
        ]
        beta = make_product(
            asin="B3",
            title="Beta Book 3",
            series_name="Beta Series",
            series_position="3",
        )
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
                asin="A1",
                series_name="Alpha Series",
                series_position="1",
                series_asin="SER_ALPHA",
            ),
            make_product(
                asin="A2",
                series_name="Alpha Series",
                series_position="2",
                series_asin="SER_ALPHA",
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
                series_position="1",
                series_asin="",
            ),
            make_product(
                asin="A2",
                title="Alpha Book 2",
                series_name="Alpha Series",
                series_position="2",
                series_asin="",
            ),
        ]
        mock_client.get_library.return_value = lib

        unowned = make_product(
            asin="A3",
            title="Alpha Book 3",
            series_name="Alpha Series",
            series_position="3",
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
                series_position="1",
                series_asin="SER_ALPHA",
            ),
            make_product(
                asin="A2",
                title="Alpha Book 2",
                series_name="Alpha Series",
                series_position="2",
                series_asin="SER_ALPHA",
            ),
            make_product(
                asin="B1",
                title="Beta Book 1",
                series_name="Beta Series",
                series_position="1",
                series_asin="SER_BETA",
            ),
            make_product(
                asin="B2",
                title="Beta Book 2",
                series_name="Beta Series",
                series_position="2",
                series_asin="SER_BETA",
            ),
        ]
        mock_client.get_library.return_value = lib

        unowned_alpha = make_product(
            asin="A3",
            title="Alpha Book 3",
            series_name="Alpha Series",
            series_position="3",
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
                series_position="1",
                series_asin="SER_ALPHA",
            ),
            make_product(
                asin="A2",
                title="Alpha Book 2",
                series_name="Alpha Series",
                series_position="2",
                series_asin="SER_ALPHA",
            ),
        ]
        mock_client.get_library.return_value = lib

        a1 = make_product(
            asin="A1",
            title="Alpha Book 1",
            series_name="Alpha Series",
            series_position="1",
        )
        a2 = make_product(
            asin="A2",
            title="Alpha Book 2",
            series_name="Alpha Series",
            series_position="2",
        )
        a3 = make_product(
            asin="A3",
            title="Alpha Book 3",
            series_name="Alpha Series",
            series_position="3",
        )
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
            make_product(
                asin="A1",
                title="Alpha 1",
                series_name="Alpha Series",
                series_position="1",
            ),
            make_product(
                asin="A2",
                title="Alpha 2",
                series_name="Alpha Series",
                series_position="2",
            ),
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
                series_position="1",
                series_asin="SER_ALPHA",
            ),
            make_product(
                asin="A2",
                title="Alpha Book 2",
                series_name="Alpha Series",
                series_position="2",
                series_asin="SER_ALPHA",
            ),
        ]
        mock_client.get_library.return_value = lib

        unowned = make_product(
            asin="A3",
            title="Alpha Book 3",
            series_name="Alpha Series",
            series_position="3",
        )
        mock_client.get_series_products.return_value = [lib[0], lib[1], unowned]

        runner = CliRunner()
        result = runner.invoke(cli, ["series", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert any(item["asin"] == "A3" for item in data)

    def test_owned_editions_are_deduplicated_before_min_books(
        self, tmp_config, mock_client
    ):
        mock_client.get_library.return_value = [
            make_product(
                asin="A1",
                title="Alpha Book 1",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
                series_position="1",
            ),
            make_product(
                asin="A1_ALT",
                title="Alpha Book 1",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
                series_position="1.0",
            ),
            make_product(
                asin="A2",
                title="Alpha Book 2",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
                series_position="2",
            ),
        ]

        result = CliRunner().invoke(cli, ["series", "--min-books", "3"])

        assert result.exit_code == 0, result.output
        assert "No series with 3+ owned books" in result.output
        mock_client.get_series_products_many.assert_not_called()

    def test_alternate_asin_for_owned_position_is_excluded(
        self, tmp_config, mock_client
    ):
        owned = [
            make_product(
                asin="A1",
                title="Alpha Book 1",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
                series_position="1",
            ),
            make_product(
                asin="A2",
                title="Alpha Side Story",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
                series_position="",
            ),
        ]
        alternate_owned = make_product(
            asin="A1_ALT",
            title="Alpha Book 1: New Narration",
            series_name="Alpha Series",
            series_asin="SER_ALPHA",
            series_position="Book 1.0",
        )
        unowned = make_product(
            asin="A3",
            title="Alpha Book 3",
            series_name="Alpha Series",
            series_asin="SER_ALPHA",
            series_position="3",
        )
        alternate_owned_title = make_product(
            asin="A2_ALT",
            title="  ALPHA\u3000SIDE STORY ",
            series_name="Alpha Series",
            series_asin="SER_ALPHA",
            series_position="1-2",
        )
        mock_client.get_library.return_value = owned
        mock_client.get_series_products.return_value = [
            alternate_owned,
            alternate_owned_title,
            unowned,
        ]

        result = CliRunner().invoke(cli, ["series", "--json"])

        assert result.exit_code == 0, result.output
        assert {item["asin"] for item in json.loads(result.stdout)} == {"A3"}

    def test_fallback_series_name_comparison_is_normalized(
        self, tmp_config, mock_client
    ):
        mock_client.get_library.return_value = [
            make_product(
                asin="A1",
                title="Alpha Book 1",
                series_name="Alpha\u3000 Series",
                series_asin="",
                series_position="1",
            ),
            make_product(
                asin="A2",
                title="Alpha Book 2",
                series_name="Alpha\u3000 Series",
                series_asin="",
                series_position="2",
            ),
        ]
        unowned = make_product(
            asin="A3",
            title="Alpha Book 3",
            series_name="  ALPHA   SERIES ",
            series_asin="",
            series_position="3",
        )
        mock_client.search_pages.return_value = iter([([unowned], 1, 1)])

        result = CliRunner().invoke(
            cli, ["series", "--series", " alpha   series ", "--json"]
        )

        assert result.exit_code == 0, result.output
        assert [item["asin"] for item in json.loads(result.stdout)] == ["A3"]

    def test_candidate_edition_preference(self, tmp_config, mock_client):
        owned = [
            make_product(
                asin="A1",
                title="Alpha Book 1",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
                series_position="1",
            ),
            make_product(
                asin="A2",
                title="Alpha Book 2",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
                series_position="2",
            ),
        ]
        unavailable = make_product(
            asin="A3_NULL",
            title="Alpha Book 3",
            series_name="Alpha Series",
            series_position="Book 3",
            price=None,
        )
        expensive = make_product(
            asin="A3_EXPENSIVE",
            title="Alpha Book 3",
            series_name="Alpha Series",
            series_position="3.0",
            price=8.0,
        )
        cheapest = make_product(
            asin="A3_CHEAP",
            title="Alpha Book 3",
            series_name="Alpha Series",
            series_position="3",
            price=4.0,
        )
        tied = make_product(
            asin="A3_TIED",
            title="Alpha Book 3",
            series_name="Alpha Series",
            series_position="Book 3.0",
            price=4.0,
        )
        mock_client.get_library.return_value = owned
        mock_client.get_series_products.return_value = [
            unavailable,
            expensive,
            cheapest,
            tied,
        ]

        result = CliRunner().invoke(cli, ["series", "--json"])

        assert result.exit_code == 0, result.output
        assert [item["asin"] for item in json.loads(result.stdout)] == ["A3_CHEAP"]

    def test_flat_removes_unavailable_placeholder_and_keeps_preorder(
        self, tmp_config, mock_client
    ):
        owned = [
            make_product(
                asin="A1",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
                series_position="1",
            ),
            make_product(
                asin="A2",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
                series_position="2",
            ),
        ]
        unavailable = make_product(
            asin="A3",
            title="Unavailable Book",
            series_name="Alpha Series",
            series_position="3",
            price=None,
        )
        placeholder = make_product(
            asin="PLACEHOLDER",
            title="Series Advisor Placeholder",
            series_name="Alpha Series",
            series_position="4",
            price=1.0,
        )
        preorder = make_product(
            asin="PREORDER",
            title="Upcoming Book",
            series_name="Alpha Series",
            series_position="5",
            price=6.0,
            length_minutes=0,
        )
        mock_client.get_library.return_value = owned
        mock_client.get_series_products.return_value = [
            unavailable,
            placeholder,
            preorder,
        ]

        result = CliRunner().invoke(cli, ["series", "--json"])

        assert result.exit_code == 0, result.output
        assert [item["asin"] for item in json.loads(result.stdout)] == ["PREORDER"]
        session = json.loads(constants_mod.LAST_RESULTS_FILE.read_text())
        assert [item["asin"] for item in session["candidates"]] == ["PREORDER"]

    def test_gaps_retains_unavailable_and_counts_unique_identities(
        self, tmp_config, mock_client
    ):
        first_edition = make_product(
            asin="A1",
            title="Alpha Book 1",
            series_name="Alpha Series",
            series_asin="SER_ALPHA",
            series_position="1",
        )
        alternate_edition = make_product(
            asin="A1_OWNED_ALT",
            title="Alpha Book 1: Full Cast",
            series_name="Alpha Series",
            series_asin="SER_ALPHA",
            series_position="1.0",
        )
        second = make_product(
            asin="A2",
            title="Alpha Book 2",
            series_name="Alpha Series",
            series_asin="SER_ALPHA",
            series_position="2",
        )
        catalog_owned_edition = make_product(
            asin="A1_CATALOG_ALT",
            title="Alpha Book 1: Another Edition",
            series_name="Alpha Series",
            series_position="Book 1",
        )
        unavailable = make_product(
            asin="A3",
            title="Alpha Book 3",
            series_name="Alpha Series",
            series_position="3",
            price=None,
        )
        placeholder = make_product(
            asin="PLACEHOLDER",
            title="Series Advisor Placeholder",
            series_name="Alpha Series",
            series_position="4",
            price=None,
        )
        mock_client.get_library.return_value = [
            first_edition,
            alternate_edition,
            second,
        ]
        mock_client.get_series_products.return_value = [
            catalog_owned_edition,
            unavailable,
            placeholder,
        ]

        result = CliRunner().invoke(cli, ["series", "--gaps", "--json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == [
            {
                "series": "Alpha Series",
                "owned": 2,
                "total_known": 3,
                "missing": [
                    {
                        "asin": "A3",
                        "title": "Alpha Book 3",
                        "position": "3",
                        "price": None,
                    }
                ],
            }
        ]

    def test_series_keeps_distinct_ambiguous_position_titles(
        self, tmp_config, mock_client
    ):
        owned = [
            make_product(
                asin="A1",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
                series_position="1",
            ),
            make_product(
                asin="A2",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
                series_position="2",
            ),
        ]
        first = make_product(
            asin="A3",
            title="Alpha Collection",
            series_name="Alpha Series",
            series_position="Books 1 & 3",
        )
        second = make_product(
            asin="A4",
            title="Alpha Companion",
            series_name="Alpha Series",
            series_position="Books 1 & 3",
        )
        mock_client.get_library.return_value = owned
        mock_client.get_series_products.return_value = [first, second]

        result = CliRunner().invoke(cli, ["series", "--json"])

        assert result.exit_code == 0, result.output
        assert {item["asin"] for item in json.loads(result.stdout)} == {"A3", "A4"}

    def test_gaps_shared_child_is_reported_for_each_invested_series(
        self, tmp_config, mock_client
    ):
        mock_client.get_library.return_value = [
            make_product(
                asin="A1",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
                series_position="1",
            ),
            make_product(
                asin="A2",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
                series_position="2",
            ),
            make_product(
                asin="B1",
                series_name="Beta Series",
                series_asin="SER_BETA",
                series_position="1",
            ),
            make_product(
                asin="B2",
                series_name="Beta Series",
                series_asin="SER_BETA",
                series_position="2",
            ),
        ]
        shared = make_product(
            asin="SHARED",
            title="Shared Book",
            series_name="Shared Universe",
            series_position="3",
        )
        mock_client.get_series_products.side_effect = [[shared], [shared]]

        result = CliRunner().invoke(cli, ["series", "--gaps", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert [entry["series"] for entry in data] == ["Alpha Series", "Beta Series"]
        assert [(entry["owned"], entry["total_known"]) for entry in data] == [
            (2, 3),
            (2, 3),
        ]
        assert [entry["missing"][0]["asin"] for entry in data] == [
            "SHARED",
            "SHARED",
        ]

    def test_gaps_cli_max_price_keeps_unavailable_and_filters_priced(
        self, tmp_config, mock_client
    ):
        self._assert_gaps_max_price(mock_client, ["--max-price", "5"])

    def test_gaps_config_max_price_keeps_unavailable_and_filters_priced(
        self, tmp_config, mock_client
    ):
        constants_mod.CONFIG_FILE.write_text(json.dumps({"max_price": 5.0}))
        self._assert_gaps_max_price(mock_client, [])

    @staticmethod
    def _assert_gaps_max_price(mock_client, options):
        mock_client.get_library.return_value = [
            make_product(
                asin="A1",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
                series_position="1",
            ),
            make_product(
                asin="A2",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
                series_position="2",
            ),
        ]
        mock_client.get_series_products.return_value = [
            make_product(
                asin="A3",
                title="Unavailable Book",
                series_name="Alpha Series",
                series_position="3",
                price=None,
            ),
            make_product(
                asin="A4",
                title="Affordable Book",
                series_name="Alpha Series",
                series_position="4",
                price=4.0,
            ),
            make_product(
                asin="A5",
                title="Expensive Book",
                series_name="Alpha Series",
                series_position="5",
                price=6.0,
            ),
        ]

        result = CliRunner().invoke(cli, ["series", "--gaps", "--json", *options])

        assert result.exit_code == 0, result.output
        missing = json.loads(result.stdout)[0]["missing"]
        assert [item["asin"] for item in missing] == ["A3", "A4"]

    def test_gaps_renders_unavailable(self, tmp_config, mock_client):
        owned = [
            make_product(
                asin="A1",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
                series_position="1",
            ),
            make_product(
                asin="A2",
                series_name="Alpha Series",
                series_asin="SER_ALPHA",
                series_position="2",
            ),
        ]
        unavailable = make_product(
            asin="A3",
            title="Unavailable Book",
            series_name="Alpha Series",
            series_position="3",
            price=None,
        )
        mock_client.get_library.return_value = owned
        mock_client.get_series_products.return_value = [unavailable]

        result = CliRunner().invoke(cli, ["series", "--gaps"])

        assert result.exit_code == 0, result.output
        assert "Unavailable Book" in result.output
        assert "unavailable" in result.output

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
