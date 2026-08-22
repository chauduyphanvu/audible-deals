"""Integration coverage for catalog CLI routes."""

from __future__ import annotations

import json

from click.testing import CliRunner

from audible_deals.cli import cli
from audible_deals.serialization import (
    serialize_product as _serialize_product,
)
from tests.conftest import make_product


def _routes_run(runner, args, **kwargs):
    """Invoke the CLI and return the result; fail on unexpected errors."""
    result = runner.invoke(cli, args, catch_exceptions=False, **kwargs)
    return result


def _routes_setup_search_mock(mock_client, products):
    """Configure mock_client.search_pages to yield a single page of products."""
    mock_client.search_pages.return_value = iter([(products, 1, len(products))])


def _routes_setup_library_mock(mock_client, products):
    """Configure mock_client.get_library_pages to yield a single page."""
    mock_client.get_library_pages.return_value = iter([(products, 1)])


def _routes_seed_last_results(tmp_config, products):
    """Write a last_results.json cache file."""

    data = {
        "title": "Test Results",
        "results": [_serialize_product(p) for p in products],
    }
    (tmp_config / "last_results.json").write_text(json.dumps(data))


class TestRoutesCategoriesCommand:
    def test_categories_top_level(self, tmp_config, mock_client):
        mock_client.get_categories.return_value = [
            {"id": "cat1", "name": "Science Fiction & Fantasy"},
            {"id": "cat2", "name": "Mystery, Thriller & Suspense"},
        ]
        result = _routes_run(CliRunner(), ["categories"])
        assert result.exit_code == 0
        assert "Science Fiction" in result.output

    def test_categories_with_parent(self, tmp_config, mock_client):
        mock_client.get_categories.return_value = [
            {"id": "sub1", "name": "Hard Science Fiction"},
        ]
        result = _routes_run(CliRunner(), ["categories", "--parent", "cat1"])
        assert result.exit_code == 0
        assert "Hard Science Fiction" in result.output


class TestRoutesSearchCommand:
    def test_search_basic(self, tmp_config, mock_client):
        products = [make_product(asin="B001", title="Found Book", price=4.99)]
        _routes_setup_search_mock(mock_client, products)
        mock_client.resolve_genre.return_value = ("cat1", "Science Fiction")
        result = _routes_run(CliRunner(), ["search", "test query"])
        assert result.exit_code == 0
        assert "Found Book" in result.output

    def test_search_with_genre(self, tmp_config, mock_client):
        products = [make_product(asin="B002", title="Sci-Fi Book", price=3.99)]
        _routes_setup_search_mock(mock_client, products)
        mock_client.resolve_genre.return_value = ("cat1", "Science Fiction")
        result = _routes_run(CliRunner(), ["search", "query", "--genre", "sci-fi"])
        assert result.exit_code == 0

    def test_search_with_category(self, tmp_config, mock_client):
        products = [make_product(asin="B003", title="Cat Book", price=2.99)]
        _routes_setup_search_mock(mock_client, products)
        mock_client.get_category_name.return_value = "Mystery"
        result = _routes_run(CliRunner(), ["search", "query", "--category", "cat1"])
        assert result.exit_code == 0

    def test_search_json_output(self, tmp_config, mock_client):
        products = [make_product(asin="B004", title="JSON Book", price=5.99)]
        _routes_setup_search_mock(mock_client, products)
        result = _routes_run(CliRunner(), ["search", "test", "--json", "--quiet"])
        assert result.exit_code == 0
        # JSON output goes to stdout; progress bar goes to stderr via console redirect
        # Extract the JSON portion from output
        json_start = result.output.index("[")
        data = json.loads(result.output[json_start:])
        assert len(data) > 0

    def test_search_quiet(self, tmp_config, mock_client):
        products = [make_product(asin="B005", price=1.99)]
        _routes_setup_search_mock(mock_client, products)
        result = _routes_run(CliRunner(), ["search", "test", "--quiet"])
        assert result.exit_code == 0

    def test_search_csv_export(self, tmp_config, mock_client):
        products = [make_product(asin="B006", price=2.99)]
        _routes_setup_search_mock(mock_client, products)
        out_path = tmp_config / "out.csv"
        result = _routes_run(CliRunner(), ["search", "test", "-o", str(out_path)])
        assert result.exit_code == 0
        assert out_path.exists()

    def test_search_json_export(self, tmp_config, mock_client):
        products = [make_product(asin="B007", price=2.99)]
        _routes_setup_search_mock(mock_client, products)
        out_path = tmp_config / "out.json"
        result = _routes_run(CliRunner(), ["search", "test", "-o", str(out_path)])
        assert result.exit_code == 0
        assert out_path.exists()
        data = json.loads(out_path.read_text())
        assert len(data) > 0

    def test_search_deep(self, tmp_config, mock_client):
        products = [make_product(asin="B008", price=1.99)]
        _routes_setup_search_mock(mock_client, products)
        result = _routes_run(CliRunner(), ["search", "test", "--deep"])
        assert result.exit_code == 0

    def test_search_or_queries(self, tmp_config, mock_client):
        products = [make_product(asin="B009", price=3.99)]
        _routes_setup_search_mock(mock_client, products)
        result = _routes_run(CliRunner(), ["search", "query1 | query2"])
        assert result.exit_code == 0

    def test_search_dry_run(self, tmp_config, mock_client):
        result = _routes_run(CliRunner(), ["search", "test", "--dry-run"])
        assert result.exit_code == 0
        assert "Dry run" in result.output

    def test_search_skip_owned(self, tmp_config, mock_client):
        products = [make_product(asin="B010", price=4.99)]
        _routes_setup_search_mock(mock_client, products)
        mock_client.get_library_asins.return_value = set()
        result = _routes_run(CliRunner(), ["search", "test", "--skip-owned"])
        assert result.exit_code == 0

    def test_search_exclude_seen(self, tmp_config, mock_client):
        products = [make_product(asin="B011", price=4.99)]
        _routes_setup_search_mock(mock_client, products)
        result = _routes_run(CliRunner(), ["search", "test", "--exclude-seen"])
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
        _routes_setup_search_mock(mock_client, products)
        result = _routes_run(
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
        _routes_setup_search_mock(mock_client, products)
        mock_client.resolve_genre.return_value = ("cat_exc", "Erotica")
        result = _routes_run(
            CliRunner(), ["search", "test", "--exclude-genre", "erotica"]
        )
        assert result.exit_code == 0

    def test_search_exclude_author(self, tmp_config, mock_client):
        products = [make_product(asin="B014", price=2.99, authors=["Good Author"])]
        _routes_setup_search_mock(mock_client, products)
        result = _routes_run(
            CliRunner(), ["search", "test", "--exclude-author", "Bad Author"]
        )
        assert result.exit_code == 0

    def test_search_with_profile(self, tmp_config, mock_client):
        # Save a profile first
        profiles_file = tmp_config / "profiles.json"
        profiles_file.write_text(
            json.dumps({"test-profile": {"max_price": 5.0, "genre": "sci-fi"}})
        )
        products = [make_product(asin="B015", price=3.99)]
        _routes_setup_search_mock(mock_client, products)
        mock_client.resolve_genre.return_value = ("cat1", "Science Fiction")
        result = _routes_run(
            CliRunner(), ["search", "test", "--profile", "test-profile"]
        )
        assert result.exit_code == 0

    def test_search_no_query_error(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["search"])
        assert result.exit_code != 0

    def test_search_max_pph(self, tmp_config, mock_client):
        products = [make_product(asin="B016", price=2.99, length_minutes=600)]
        _routes_setup_search_mock(mock_client, products)
        result = _routes_run(
            CliRunner(), ["search", "test", "--max-price-per-hour", "1.0"]
        )
        assert result.exit_code == 0

    def test_search_author_filter(self, tmp_config, mock_client):
        products = [make_product(asin="B017", price=2.99, authors=["Andy Weir"])]
        _routes_setup_search_mock(mock_client, products)
        result = _routes_run(CliRunner(), ["search", "test", "--author", "Andy"])
        assert result.exit_code == 0

    def test_search_narrator_filter(self, tmp_config, mock_client):
        products = [make_product(asin="B018", price=2.99, narrators=["R.C. Bray"])]
        _routes_setup_search_mock(mock_client, products)
        result = _routes_run(CliRunner(), ["search", "test", "--narrator", "Bray"])
        assert result.exit_code == 0

    def test_search_series_filter(self, tmp_config, mock_client):
        products = [make_product(asin="B019", price=2.99, series_name="Expanse")]
        _routes_setup_search_mock(mock_client, products)
        result = _routes_run(CliRunner(), ["search", "test", "--series", "Expanse"])
        assert result.exit_code == 0


class TestRoutesFindCommand:
    def test_find_basic(self, tmp_config, mock_client):
        products = [make_product(asin="F001", title="Deal Book", price=3.99)]
        _routes_setup_search_mock(mock_client, products)
        result = _routes_run(CliRunner(), ["find"])
        assert result.exit_code == 0

    def test_find_with_genre(self, tmp_config, mock_client):
        products = [make_product(asin="F002", price=2.99)]
        _routes_setup_search_mock(mock_client, products)
        mock_client.resolve_genre.return_value = ("cat1", "Science Fiction")
        result = _routes_run(CliRunner(), ["find", "--genre", "sci-fi"])
        assert result.exit_code == 0

    def test_find_with_category(self, tmp_config, mock_client):
        products = [make_product(asin="F003", price=2.99)]
        _routes_setup_search_mock(mock_client, products)
        mock_client.get_category_name.return_value = "Fantasy"
        result = _routes_run(CliRunner(), ["find", "--category", "cat1"])
        assert result.exit_code == 0

    def test_find_with_keywords(self, tmp_config, mock_client):
        products = [make_product(asin="F004", price=2.99)]
        _routes_setup_search_mock(mock_client, products)
        result = _routes_run(CliRunner(), ["find", "--keywords", "space"])
        assert result.exit_code == 0

    def test_find_deep(self, tmp_config, mock_client):
        products = [make_product(asin="F005", price=1.99)]
        _routes_setup_search_mock(mock_client, products)
        result = _routes_run(CliRunner(), ["find", "--deep"])
        assert result.exit_code == 0

    def test_find_dry_run(self, tmp_config, mock_client):
        result = _routes_run(CliRunner(), ["find", "--dry-run"])
        assert result.exit_code == 0
        assert "Dry run" in result.output

    def test_find_json_output(self, tmp_config, mock_client):
        products = [make_product(asin="F006", price=2.99)]
        _routes_setup_search_mock(mock_client, products)
        result = _routes_run(CliRunner(), ["find", "--json", "--quiet"])
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
        _routes_setup_search_mock(mock_client, products)
        result = _routes_run(
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
        _routes_setup_search_mock(mock_client, products)
        mock_client.get_library_asins.return_value = set()
        result = _routes_run(CliRunner(), ["find", "--skip-owned"])
        assert result.exit_code == 0

    def test_find_with_profile(self, tmp_config, mock_client):
        profiles_file = tmp_config / "profiles.json"
        profiles_file.write_text(
            json.dumps({"scifi": {"genre": "sci-fi", "max_price": 5.0}})
        )
        products = [make_product(asin="F009", price=3.99)]
        _routes_setup_search_mock(mock_client, products)
        mock_client.resolve_genre.return_value = ("cat1", "Science Fiction")
        result = _routes_run(CliRunner(), ["find", "--profile", "scifi"])
        assert result.exit_code == 0

    def test_find_csv_export(self, tmp_config, mock_client):
        products = [make_product(asin="F010", price=2.99)]
        _routes_setup_search_mock(mock_client, products)
        out_path = tmp_config / "find_out.csv"
        result = _routes_run(CliRunner(), ["find", "-o", str(out_path)])
        assert result.exit_code == 0
        assert out_path.exists()

    def test_find_exclude_genre(self, tmp_config, mock_client):
        products = [make_product(asin="F011", price=2.99)]
        _routes_setup_search_mock(mock_client, products)
        mock_client.resolve_genre.return_value = ("cat_exc", "Erotica")
        result = _routes_run(CliRunner(), ["find", "--exclude-genre", "erotica"])
        assert result.exit_code == 0


class TestRoutesDetailCommand:
    def test_detail_by_asin(self, tmp_config, mock_client):
        p = make_product(asin="D001", title="Detail Book")
        mock_client.get_product.return_value = p
        result = _routes_run(CliRunner(), ["detail", "D001"])
        assert result.exit_code == 0
        assert "Detail Book" in result.output

    def test_detail_by_last_ref(self, tmp_config, mock_client):
        products = [make_product(asin="D002", title="Last Ref Book")]
        _routes_seed_last_results(tmp_config, products)
        mock_client.get_product.return_value = products[0]
        result = _routes_run(CliRunner(), ["detail", "--last", "1"])
        assert result.exit_code == 0
        assert "Last Ref Book" in result.output

    def test_detail_no_asin_error(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["detail"])
        assert result.exit_code != 0


class TestRoutesCompareCommand:
    def test_compare_two_asins(self, tmp_config, mock_client):
        products = [
            make_product(asin="C001", title="Book A", price=5.99),
            make_product(asin="C002", title="Book B", price=3.99),
        ]
        mock_client.get_products_batch.return_value = products
        result = _routes_run(CliRunner(), ["compare", "C001", "C002"])
        assert result.exit_code == 0
        assert "Book A" in result.output
        assert "Book B" in result.output

    def test_compare_with_last_refs(self, tmp_config, mock_client):
        products = [
            make_product(asin="C003", title="Ref A", price=5.99),
            make_product(asin="C004", title="Ref B", price=3.99),
        ]
        _routes_seed_last_results(tmp_config, products)
        mock_client.get_products_batch.return_value = products
        result = _routes_run(CliRunner(), ["compare", "--last", "1", "--last", "2"])
        assert result.exit_code == 0

    def test_compare_too_few_error(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["compare", "C005"])
        assert result.exit_code != 0


class TestRoutesLibraryCommand:
    def test_library_basic(self, tmp_config, mock_client):
        products = [make_product(asin="L001", title="My Book", price=14.99)]
        _routes_setup_library_mock(mock_client, products)
        result = _routes_run(CliRunner(), ["library"])
        assert result.exit_code == 0
        assert "My Book" in result.output

    def test_library_json(self, tmp_config, mock_client):
        products = [make_product(asin="L002", price=9.99)]
        _routes_setup_library_mock(mock_client, products)
        result = _routes_run(CliRunner(), ["library", "--json", "--quiet"])
        assert result.exit_code == 0
        json_start = result.output.index("[")
        data = json.loads(result.output[json_start:])
        assert len(data) == 1

    def test_library_with_filters(self, tmp_config, mock_client):
        products = [
            make_product(asin="L003", price=9.99, rating=4.5, authors=["Andy Weir"]),
            make_product(asin="L004", price=5.99, rating=3.0, authors=["Other"]),
        ]
        _routes_setup_library_mock(mock_client, products)
        result = _routes_run(
            CliRunner(), ["library", "--author", "Andy", "--min-rating", "4.0"]
        )
        assert result.exit_code == 0

    def test_library_export(self, tmp_config, mock_client):
        products = [make_product(asin="L005", price=9.99)]
        _routes_setup_library_mock(mock_client, products)
        out_path = tmp_config / "library.json"
        result = _routes_run(CliRunner(), ["library", "-o", str(out_path)])
        assert result.exit_code == 0
        assert out_path.exists()

    def test_library_sort(self, tmp_config, mock_client):
        products = [
            make_product(asin="L006", price=9.99, rating=4.0),
            make_product(asin="L007", price=5.99, rating=4.8),
        ]
        _routes_setup_library_mock(mock_client, products)
        result = _routes_run(CliRunner(), ["library", "--sort", "rating", "-n", "1"])
        assert result.exit_code == 0


class TestRoutesZeroLengthFiltering:
    def test_zero_length_excluded_from_find(self, tmp_config, mock_client):
        """find drops products with length_minutes==0 and records 'no runtime' breakdown."""
        products = [
            make_product(asin="NR1", length_minutes=0, price=3.99),
            make_product(asin="NR2", length_minutes=600, price=3.99),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "zero_length.json"
        result = _routes_run(
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
        from audible_deals.result_models import FilterContext
        from audible_deals.result_processing import (
            DiscoveryProcessingRequest,
            process_discovery,
        )

        products = [
            make_product(asin="NR3", length_minutes=0, price=3.99),
            make_product(asin="NR4", length_minutes=300, price=3.99),
        ]
        result = process_discovery(
            DiscoveryProcessingRequest(
                tuple(products),
                FilterContext(drop_zero_length=True, sort="price"),
            )
        )
        assert result.breakdown.get("no runtime") == 1

    def test_library_keeps_zero_length(self, tmp_config, mock_client):
        """library does NOT drop zero-length items — they are kept as-is."""
        products = [
            make_product(asin="LZ1", length_minutes=0, price=0.0),
            make_product(asin="LZ2", length_minutes=600, price=0.0),
        ]
        _routes_setup_library_mock(mock_client, products)
        result = _routes_run(
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
            series_position="1",
            length_minutes=600,
            price=0.0,
        )
        owned2 = make_product(
            asin="SO2",
            series_name="Epic Arc",
            series_asin="SARC1",
            series_position="2",
            length_minutes=600,
            price=0.0,
        )
        # A pre-order with length_minutes==0 that should survive the series pipeline
        preorder = make_product(
            asin="SPRE1",
            series_name="Epic Arc",
            series_asin="SARC1",
            series_position="3",
            length_minutes=0,
            price=14.99,
        )
        mock_client.get_library.return_value = [owned1, owned2]
        mock_client.get_series_products.return_value = [preorder]

        result = _routes_run(
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
        result = _routes_run(
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


class TestRoutesLibraryStats:
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
        _routes_setup_library_mock(mock_client, products)
        result = _routes_run(CliRunner(), ["library", "--stats"])
        assert result.exit_code == 0
        assert "2" in result.output  # total books
        assert "15" in result.output  # total hours (600+300 = 900 min = 15 h)

    def test_library_stats_shows_top_author(self, tmp_config, mock_client):
        products = [
            make_product(asin="LS03", authors=["Repeated Author"], length_minutes=300),
            make_product(asin="LS04", authors=["Repeated Author"], length_minutes=300),
            make_product(asin="LS05", authors=["Other Author"], length_minutes=300),
        ]
        _routes_setup_library_mock(mock_client, products)
        result = _routes_run(CliRunner(), ["library", "--stats"])
        assert result.exit_code == 0
        assert "Repeated Author" in result.output


class TestRoutesParseSeriesPosition:
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


class TestRoutesSeriesGapsMode:
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
        result = _routes_run(
            CliRunner(),
            ["series", "--gaps", "--min-books", "2"],
        )
        assert result.exit_code == 0, result.output

        # JSON confirms the gap report content
        result_json = _routes_run(
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

        result = _routes_run(
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

        result = _routes_run(
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

        result = _routes_run(
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
