"""Catalog CLI and domain behavior."""

from __future__ import annotations

import datetime
import json

import click
import pytest
from click.testing import CliRunner

import audible_deals.config_store as config_store_mod
import audible_deals.constants as constants_mod
from audible_deals.catalog_workflow import (
    build_search_scan_plan,
    execute_catalog_scan,
    rank_catalog_relevance,
)
from audible_deals.cli import cli
from audible_deals.filtering import (
    dedupe_editions as _typed_dedupe_editions,
)
from audible_deals.filtering import (
    filter_products as _typed_filter_products,
)
from audible_deals.filtering import (
    first_in_series as _typed_first_in_series,
)
from audible_deals.filtering import (
    sort_local as _sort_local,
)
from audible_deals.metrics import (
    price_per_hour as _price_per_hour,
)
from audible_deals.metrics import (
    value_score as _value_score,
)
from audible_deals.presentation.terminal import catalog_scan_progress
from audible_deals.result_models import (
    CatalogScanPlan,
    CatalogScanProgress,
    FilterContext,
    FilterOutcome,
)
from audible_deals.serialization import (
    deserialize_product as _deserialize_product,
)
from audible_deals.serialization import (
    export_products as _export_products,
)
from audible_deals.serialization import (
    serialize_product as _serialize_product,
)
from audible_deals.settings import SettingsResolutionRequest, resolve_settings
from tests.conftest import make_product


def _resolve_settings(ctx, *, config, profile, cli_flags):
    explicit_options = {
        key
        for key in cli_flags
        if ctx.get_parameter_source(key) == click.core.ParameterSource.COMMANDLINE
    }
    return resolve_settings(
        SettingsResolutionRequest(
            config=config,
            profile=profile,
            cli_flags=cli_flags,
            explicit_options=explicit_options,
        )
    )


def _filter_products(products, **values):
    outcome = _typed_filter_products(products, FilterContext(**values))
    return list(outcome.products), dict(outcome.breakdown)


def _dedupe_editions(products):
    outcome = _typed_dedupe_editions(FilterOutcome(products))
    return list(outcome.products), outcome.editions_removed


def _first_in_series(products):
    outcome = _typed_first_in_series(FilterOutcome(products))
    return list(outcome.products), outcome.series_collapsed


def _mock_library_pages(mock_client, products):
    """Set up get_library_pages mock yielding a single page."""
    mock_client.get_library_pages.return_value = iter([(products, 1)])


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


def _seed_price_history(asin: str, prices: list[float]) -> None:
    """Write prior-day history entries for an ASIN (one per past day)."""
    constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today()
    entries = [
        {
            "date": (today - datetime.timedelta(days=len(prices) - i)).isoformat(),
            "price": price,
            "title": "Test Book",
        }
        for i, price in enumerate(prices)
    ]
    (constants_mod.HISTORY_DIR / f"{asin}.json").write_text(json.dumps(entries))


def _capture_history_context(monkeypatch):
    """Patch display_products to record the hist_context it receives."""
    import audible_deals.presentation.result_output as result_output_mod

    captured: dict[str, dict] = {}

    def fake_display_products(filtered, **kwargs):
        captured["hist_context"] = kwargs.get("hist_context")

    monkeypatch.setattr(result_output_mod, "display_products", fake_display_products)
    return captured


class TestCatalogRegressions:
    def test_network_error_is_rendered_as_click_error(self, mock_client, tmp_config):
        from audible.exceptions import NetworkError

        mock_client.search_pages.side_effect = NetworkError()
        result = CliRunner().invoke(cli, ["find"])
        assert result.exit_code != 0
        assert "Audible request failed" in result.output
        assert "Traceback" not in result.output

    @pytest.mark.parametrize("error_name", ["NetworkError", "NotResponding"])
    def test_request_errors_are_rendered_as_click_errors(
        self, mock_client, tmp_config, error_name
    ):
        import audible.exceptions as audible_exceptions

        mock_client.search_pages.side_effect = getattr(audible_exceptions, error_name)()
        result = CliRunner().invoke(cli, ["find"])
        assert result.exit_code != 0
        assert "Audible request failed" in result.output
        assert "Traceback" not in result.output

    @pytest.mark.parametrize(
        ("command", "module_name"),
        [
            (["library", "--output", "bad.txt"], "audible_deals.cli.library"),
            (["series", "--output", "bad.txt"], "audible_deals.cli.series"),
            (["for-me", "--output", "bad.txt"], "audible_deals.cli.foryou"),
        ],
    )
    def test_bad_export_extension_does_not_construct_client(
        self, tmp_config, monkeypatch, command, module_name
    ):
        module = __import__(module_name, fromlist=["_get_client"])
        monkeypatch.setattr(
            module,
            "_get_client",
            lambda locale: pytest.fail("constructed client before output validation"),
        )
        result = CliRunner().invoke(cli, command)
        assert result.exit_code != 0
        assert "Unsupported extension" in result.output

    def test_export_write_failure_prevents_scan_state_commits(
        self, mock_client, tmp_config
    ):
        mock_client.search_pages.return_value = iter([([make_product()], 1, 1)])
        output_dir = tmp_config / "output.json"
        output_dir.mkdir()
        result = CliRunner().invoke(
            cli, ["find", "--output", str(output_dir), "--all-languages"]
        )
        assert result.exit_code != 0
        assert "Filesystem error" in result.output
        assert not constants_mod.LAST_RESULTS_FILE.exists()
        assert not constants_mod.SEEN_ASINS_FILE.exists()
        assert not constants_mod.HISTORY_DIR.exists()

    def test_search_multi_query_dry_run_multiplies_estimates(self, tmp_config):
        result = CliRunner().invoke(
            cli, ["search", "one | two", "--pages", "2", "--deep", "--dry-run"]
        )
        assert result.exit_code == 0, result.output
        assert "Max items: ~700" in result.output
        assert "API calls: 14" in result.output


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
        filtered, _ = _filter_products(
            products_for_filtering, skip_asins={"CHEAP1", "CHEAP2"}
        )
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
            max_price=5.0,
            min_rating=4.0,
            language="english",
        )
        for p in filtered:
            assert p.price is not None and p.price <= 5.0
            assert p.rating >= 4.0
            assert p.language.lower() == "english"


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


class TestSortLocal:
    @pytest.fixture
    def products(self):
        return [
            make_product(
                asin="A",
                price=5.0,
                rating=3.0,
                length_minutes=300,
                release_date="2024-01-01",
                list_price=10.0,
            ),
            make_product(
                asin="B",
                price=2.0,
                rating=5.0,
                length_minutes=600,
                release_date="2024-06-01",
                list_price=20.0,
            ),
            make_product(
                asin="C",
                price=8.0,
                rating=4.0,
                length_minutes=120,
                release_date="2023-01-01",
                list_price=10.0,
            ),
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
        # "Book 1" now parses as 1.0 via parse_series_position, same as "1".
        # On a tie, the first-seen product wins (stable behaviour).
        products = [
            make_product(asin="A", series_name="S", series_position="Book 1"),
            make_product(asin="B", series_name="S", series_position="1"),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 1
        assert result[0].asin == "A"


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

    @pytest.mark.parametrize("suffix", ["json", "csv"])
    def test_export_uses_utf8_for_non_ascii_text(self, tmp_path, suffix):
        path = tmp_path / f"out.{suffix}"
        _export_products([make_product(title="Café 東京")], path)
        assert "Café 東京" in path.read_text(encoding="utf-8")

    def test_unsupported_format(self, tmp_path):
        import click

        path = tmp_path / "out.xml"
        with pytest.raises(click.BadParameter, match="Unsupported"):
            _export_products([make_product()], path)


class TestFindCommand:
    def test_find_basic(self, mock_client, tmp_config):
        products = [
            make_product(asin=f"F{i}", price=float(i), list_price=20.0)
            for i in range(1, 6)
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 5)])
        mock_client.resolve_genre.return_value = ("cat1", "Fiction")

        runner = CliRunner()
        result = runner.invoke(
            cli, ["find", "--genre", "fiction", "--max-price", "10", "--pages", "1"]
        )
        assert result.exit_code == 0, result.output
        assert "Deals under $10.00" in result.output

    def test_find_json_output(self, mock_client, tmp_config):
        products = [make_product(asin="J1", price=3.0)]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])

        out_file = tmp_config / "out.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 1
        assert data[0]["asin"] == "J1"

    def test_find_limit(self, mock_client, tmp_config):
        products = [
            make_product(
                asin=f"L{i}", price=float(i), series_name="", series_position=""
            )
            for i in range(1, 11)
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 10)])

        out_file = tmp_config / "limit.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "20",
                "--pages",
                "1",
                "--limit",
                "3",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 3

    def test_find_quiet(self, mock_client, tmp_config):
        products = [make_product(price=3.0)]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "-q",
            ],
        )
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
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--output",
                str(out_file),
            ],
        )
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
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--output",
                str(out_file),
                "-q",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Deals under" not in result.output


class TestSearchCommand:
    def test_search_basic(self, mock_client, tmp_config):
        products = [make_product(asin="S1", price=5.0)]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])

        out_file = tmp_config / "search.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "test query",
                "--pages",
                "1",
                "-q",
                "--output",
                str(out_file),
            ],
        )
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
        result = runner.invoke(
            cli,
            [
                "search",
                "test",
                "--pages",
                "1",
                "--output",
                str(out_file),
            ],
        )
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
        result = runner.invoke(
            cli,
            [
                "search",
                "test",
                "--pages",
                "1",
                "--output",
                str(out_file),
                "-q",
            ],
        )
        assert result.exit_code == 0, result.output
        assert 'Search: "test"' not in result.output


class TestDetailCommand:
    def test_detail_ok(self, mock_client, tmp_config):
        mock_client.get_product.return_value = make_product(
            asin="D1", title="Detail Test"
        )

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


class TestFetchWithProgress:
    def test_scan_plan_is_frozen_and_owns_request_counts(self):
        plan = CatalogScanPlan.create(
            queries=["one", "two"],
            category_ids=["a", "b"],
            sort_orders=["BestSellers", "-ReleaseDate", "AvgRating"],
            exact_probe_queries=["one", "two"],
            pages=4,
        )

        assert plan.queries == ("one", "two")
        assert plan.category_multiplier == 2
        assert plan.broad_calls == 48
        assert plan.probe_calls == 4
        assert plan.total_calls == 52
        assert plan.max_items == 2600
        with pytest.raises(AttributeError):
            plan.pages = 2

        unresolved = CatalogScanPlan.create(
            queries=[""],
            category_ids=None,
            sort_orders=["BestSellers"],
            pages=1,
        )
        assert unresolved.category_multiplier is None
        assert unresolved.total_calls is None
        assert unresolved.max_items is None

    def test_rich_adapter_preserves_caller_description(self, monkeypatch):
        class ProgressRecorder:
            def __init__(self):
                self.description = None
                self.updates = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def add_task(self, description, **kwargs):
                self.description = description
                return 1

            def update(self, task, **kwargs):
                self.updates.append(kwargs)

        recorder = ProgressRecorder()
        monkeypatch.setattr(
            "audible_deals.presentation.terminal.create_scan_progress", lambda: recorder
        )
        plan = CatalogScanPlan.create(
            queries=["query"],
            category_ids=["category"],
            sort_orders=["Relevance"],
            pages=1,
        )

        with catalog_scan_progress(plan, "Scanning selected category") as update:
            update(CatalogScanProgress("Searching 'query'", 1, 1, 3))

        assert recorder.description == "Scanning selected category"
        assert recorder.updates == [{"total": 1, "completed": 1, "items": 3}]

    def test_single_sort_no_dedup(self, mock_client, tmp_config):
        """Single sort order returns all products."""
        products = [
            make_product(asin="FP1"),
            make_product(asin="FP2"),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])

        plan = CatalogScanPlan.create(
            queries=[""],
            category_ids=[""],
            sort_orders=["BestSellers"],
            pages=1,
        )
        result = execute_catalog_scan(mock_client, plan)
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

        plan = CatalogScanPlan.create(
            queries=[""],
            category_ids=[""],
            sort_orders=["BestSellers", "AvgRating"],
            pages=1,
        )
        result = execute_catalog_scan(mock_client, plan)
        asins = [p.asin for p in result]
        assert sorted(asins) == ["MD1", "MD2", "MD3"]


class TestExactTitleSearch:
    def test_broad_then_title_merge_dedup_and_relevance_tiers(
        self, mock_client, tmp_config
    ):
        broad = [
            make_product(asin="OTHER", title="Unrelated", authors=["Someone"]),
            make_product(
                asin="AUTHORPHRASE", title="Elsewhere", authors=["The Dune Writer"]
            ),
            make_product(asin="TITLEPHRASE", title="Dune Messiah"),
            make_product(asin="EXACTAUTHOR", title="Biography", authors=["DUNE"]),
        ]
        exact = [
            make_product(asin="EXACTTITLE", title="  dune "),
            make_product(asin="TITLEPHRASE", title="Dune Messiah"),
        ]
        calls = []

        def fake_search_pages(**kwargs):
            calls.append(kwargs)
            if kwargs.get("title"):
                yield exact, 1, len(exact)
            else:
                yield broad, 1, len(broad)

        mock_client.search_pages.side_effect = fake_search_pages
        plan = CatalogScanPlan.create(
            queries=["Dune"],
            category_ids=["fiction"],
            sort_orders=["Relevance"],
            exact_probe_queries=["Dune"],
            pages=2,
        )
        products = execute_catalog_scan(mock_client, plan)

        assert [call.get("title") for call in calls] == [None, "Dune"]
        assert calls[1]["category_id"] == "fiction"
        assert calls[1]["sort_by"] == "Relevance"
        assert calls[1]["max_pages"] == 1
        assert [p.asin for p in products] == [
            "EXACTTITLE",
            "EXACTAUTHOR",
            "TITLEPHRASE",
            "AUTHORPHRASE",
            "OTHER",
        ]

    def test_probe_failure_retains_broad_results(self, mock_client, tmp_config, caplog):
        broad = [make_product(asin="BROAD", title="Broad")]

        def fake_search_pages(**kwargs):
            if kwargs.get("title"):
                raise RuntimeError("title unavailable")
            yield broad, 1, 1

        mock_client.search_pages.side_effect = fake_search_pages
        with caplog.at_level("INFO", logger="audible_deals"):
            plan = build_search_scan_plan("query", pages=1)
            products = execute_catalog_scan(
                mock_client,
                plan,
            )
        assert products == broad
        assert "Exact-title probe failed" in caplog.text

    def test_probe_failure_discards_products_yielded_before_error(
        self, mock_client, tmp_config, caplog
    ):
        broad = [make_product(asin="BROAD", title="Broad")]
        partial = make_product(asin="PARTIAL", title="query")
        updates = []

        def fake_search_pages(**kwargs):
            if kwargs.get("title"):
                yield [partial], 1, 1
                raise RuntimeError("title stream failed")
            yield broad, 1, 1

        mock_client.search_pages.side_effect = fake_search_pages
        with caplog.at_level("INFO", logger="audible_deals"):
            plan = build_search_scan_plan("query", pages=1)
            products = execute_catalog_scan(mock_client, plan, updates.append)

        assert products == broad
        assert (updates[-1].total, updates[-1].completed, updates[-1].items) == (
            2,
            2,
            2,
        )
        assert "Exact-title probe failed" in caplog.text

    def test_or_queries_each_receive_one_title_probe(self, mock_client, tmp_config):
        calls = []

        def fake_search_pages(**kwargs):
            calls.append(kwargs)
            query = kwargs.get("title") or kwargs.get("keywords")
            prefix = "T" if kwargs.get("title") else "B"
            yield [make_product(asin=f"{prefix}{query}", title=query)], 1, 1

        mock_client.search_pages.side_effect = fake_search_pages
        plan = build_search_scan_plan("one | two", category_ids=["cat"], pages=1)
        products = execute_catalog_scan(mock_client, plan)
        assert [call.get("title") for call in calls] == [None, "one", None, "two"]
        assert {product.asin for product in products} == {
            "Tone",
            "Bone",
            "Ttwo",
            "Btwo",
        }

    def test_relevance_rank_is_stable_within_tiers(self):
        products = [
            make_product(asin="A", title="Dune", authors=["Other"]),
            make_product(asin="B", title="Other", authors=["dune"]),
            make_product(asin="C", title="Dune Returns"),
        ]
        assert [p.asin for p in rank_catalog_relevance(products, " DUNE ")] == [
            "A",
            "B",
            "C",
        ]

    def test_progress_includes_broad_and_exact_calls_and_unique_items(
        self, mock_client, tmp_config, monkeypatch
    ):
        updates = []
        broad = [make_product(asin="A", title="Query")]
        exact = [
            make_product(asin="A", title="Query"),
            make_product(asin="B", title="Query Two"),
        ]

        def fake_search_pages(**kwargs):
            products = exact if kwargs.get("title") else broad
            yield products, 1, len(products)

        mock_client.search_pages.side_effect = fake_search_pages

        plan = build_search_scan_plan("Query", category_ids=["cat"], pages=2)
        products = execute_catalog_scan(
            mock_client,
            plan,
            updates.append,
        )

        assert {product.asin for product in products} == {"A", "B"}
        assert (updates[-1].total, updates[-1].completed, updates[-1].items) == (
            2,
            2,
            2,
        )

    def test_multi_query_progress_reports_global_deduplicated_count(
        self, mock_client, tmp_config, monkeypatch
    ):
        updates = []

        def fake_search_pages(**kwargs):
            query = kwargs.get("title") or kwargs.get("keywords")
            asin = "SHARED" if not kwargs.get("title") else f"EXACT{query}"
            yield [make_product(asin=asin, title=query)], 1, 1

        mock_client.search_pages.side_effect = fake_search_pages

        plan = build_search_scan_plan("one | two", category_ids=["cat"], pages=1)
        products = execute_catalog_scan(mock_client, plan, updates.append)

        assert {product.asin for product in products} == {
            "SHARED",
            "EXACTone",
            "EXACTtwo",
        }
        assert (updates[-1].total, updates[-1].completed, updates[-1].items) == (
            4,
            4,
            3,
        )

    def test_failed_exact_probe_still_completes_progress_task(
        self, mock_client, tmp_config, monkeypatch
    ):
        updates = []

        def fake_search_pages(**kwargs):
            if kwargs.get("title"):
                raise RuntimeError("probe failed")
            yield [make_product(asin="BROAD")], 1, 1

        mock_client.search_pages.side_effect = fake_search_pages

        plan = build_search_scan_plan("query", category_ids=["cat"], pages=1)
        products = execute_catalog_scan(mock_client, plan, updates.append)

        assert [product.asin for product in products] == ["BROAD"]
        assert (updates[-1].total, updates[-1].completed, updates[-1].items) == (
            2,
            2,
            1,
        )


class TestSearchDeepFlag:
    def test_search_deep_flag_exists(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "--help"])
        assert result.exit_code == 0
        assert "--deep" in result.output

    def test_search_deep_deduplicates(self, mock_client, tmp_config):
        """search --deep fetches 3 sort orders and deduplicates."""
        pass1 = [
            make_product(asin="SD1", price=3.0, series_name="", series_position=""),
            make_product(asin="SD2", price=4.0, series_name="", series_position=""),
        ]
        pass2 = [
            make_product(asin="SD2", price=4.0, series_name="", series_position=""),
            make_product(asin="SD3", price=5.0, series_name="", series_position=""),
        ]
        pass3 = [
            make_product(asin="SD1", price=3.0, series_name="", series_position=""),
            make_product(asin="SD4", price=2.0, series_name="", series_position=""),
        ]

        call_count = 0

        def fake_search_pages(**kwargs):
            nonlocal call_count
            if kwargs.get("title"):
                yield [], 1, 0
                return
            data = [pass1, pass2, pass3][call_count]
            call_count += 1
            yield data, 1, len(data)

        mock_client.search_pages.side_effect = fake_search_pages
        out_file = tmp_config / "search_deep.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "test",
                "--deep",
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
        asins = sorted(d["asin"] for d in data)
        assert asins == ["SD1", "SD2", "SD3", "SD4"]


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
            make_product(
                asin="FEA1",
                price=3.0,
                authors=["Andy Weir"],
                series_name="",
                series_position="",
            ),
            make_product(
                asin="FEA2",
                price=4.0,
                authors=["Brandon Sanderson"],
                series_name="",
                series_position="",
            ),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "find_excl_author.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--exclude-author",
                "andy weir",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "FEA1" not in asins
        assert "FEA2" in asins

    def test_last_exclude_author_flag(self, tmp_config):
        """deals last --exclude-author filters from cache."""

        products = [
            make_product(
                asin="LEA1",
                price=3.0,
                authors=["Andy Weir"],
                series_name="",
                series_position="",
            ),
            make_product(
                asin="LEA2",
                price=4.0,
                authors=["Brandon Sanderson"],
                series_name="",
                series_position="",
            ),
        ]
        constants_mod.LAST_RESULTS_FILE.write_text(
            json.dumps([_serialize_product(p) for p in products])
        )
        out_file = tmp_config / "last_excl_author.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "last",
                "--exclude-author",
                "weir",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LEA1" not in asins
        assert "LEA2" in asins

    def test_exclude_author_in_profile(self, tmp_config):
        """profile save --exclude-author persists the exclusion."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "profile",
                "save",
                "no-weir",
                "--exclude-author",
                "Andy Weir",
                "--exclude-author",
                "Brandon Sanderson",
            ],
        )
        assert result.exit_code == 0, result.output

        profiles = config_store_mod.load_profiles()
        assert "no-weir" in profiles
        excluded = profiles["no-weir"]["exclude_authors"]
        assert "Andy Weir" in excluded
        assert "Brandon Sanderson" in excluded

    def test_find_profile_exclude_author_applied(self, mock_client, tmp_config):
        """find --profile with exclude_authors actually filters out the author."""

        config_store_mod.save_profiles({"no-weir": {"exclude_authors": ["Andy Weir"]}})
        products = [
            make_product(
                asin="EA1",
                price=3.0,
                authors=["Andy Weir"],
                series_name="",
                series_position="",
            ),
            make_product(
                asin="EA2",
                price=3.0,
                authors=["Pierce Brown"],
                series_name="",
                series_position="",
            ),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "profile_excl.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--profile",
                "no-weir",
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
        assert "EA1" not in asins  # Andy Weir excluded
        assert "EA2" in asins  # Pierce Brown kept


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
            make_product(
                asin="FA1",
                price=3.0,
                authors=["Andy Weir"],
                series_name="",
                series_position="",
            ),
            make_product(
                asin="FA2",
                price=4.0,
                authors=["Brandon Sanderson"],
                series_name="",
                series_position="",
            ),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "find_author.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--author",
                "weir",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "FA1" in asins
        assert "FA2" not in asins

    def test_last_author_filter(self, tmp_config):
        """deals last --author filters by author."""

        products = [
            make_product(
                asin="LA1",
                price=3.0,
                authors=["Andy Weir"],
                series_name="",
                series_position="",
            ),
            make_product(
                asin="LA2",
                price=4.0,
                authors=["Brandon Sanderson"],
                series_name="",
                series_position="",
            ),
        ]
        constants_mod.LAST_RESULTS_FILE.write_text(
            json.dumps([_serialize_product(p) for p in products])
        )
        out_file = tmp_config / "last_author.json"
        runner = CliRunner()
        result = runner.invoke(
            cli, ["last", "--author", "weir", "--output", str(out_file)]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LA1" in asins
        assert "LA2" not in asins

    def test_author_in_profile(self, tmp_config):
        """profile save --author persists the author filter."""
        runner = CliRunner()
        result = runner.invoke(
            cli, ["profile", "save", "weir-profile", "--author", "Andy Weir"]
        )
        assert result.exit_code == 0, result.output

        profiles = config_store_mod.load_profiles()
        assert profiles["weir-profile"]["author"] == "Andy Weir"

    def test_author_in_config(self, tmp_config):
        """config set author saves and retrieves the author filter."""
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "author", "Andy Weir"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(cli, ["config", "get", "author"])
        assert result.exit_code == 0, result.output
        assert "Andy Weir" in result.output


class TestFindTitleIncludesGenre:
    def test_find_title_with_genre(self, mock_client, tmp_config):
        """find --genre shows category name in the table title."""
        products = [
            make_product(asin="GT1", price=3.0, series_name="", series_position="")
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        mock_client.resolve_genre.return_value = ("cat42", "Science Fiction")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--genre",
                "sci-fi",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--all-languages",
                "-n",
                "0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Science Fiction" in result.output

    def test_find_title_without_genre(self, mock_client, tmp_config):
        """find without --genre does not include a category in title."""
        products = [
            make_product(asin="NT1", price=3.0, series_name="", series_position="")
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--all-languages",
                "-n",
                "0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Deals under $10.00" in result.output


class TestFindDefaultLimit:
    def test_find_default_limit_25(self, mock_client, tmp_config):
        """find without --limit defaults to 25 results."""
        products = [
            make_product(
                asin=f"DL{i:02d}",
                price=float(i),
                series_name="",
                series_position="",
                num_ratings=10,
            )
            for i in range(1, 36)
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 35)])
        out_file = tmp_config / "default_limit.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "100",
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
        assert len(data) == 25

    def test_find_limit_zero_means_unlimited(self, mock_client, tmp_config):
        """find -n 0 shows all results (unlimited)."""
        products = [
            make_product(
                asin=f"UL{i:02d}",
                price=float(i),
                series_name="",
                series_position="",
                num_ratings=10,
            )
            for i in range(1, 36)
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 35)])
        out_file = tmp_config / "unlimited.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "100",
                "--pages",
                "1",
                "-n",
                "0",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 35

    def test_search_default_limit_25(self, mock_client, tmp_config):
        """search defaults to limit=25 (same as find)."""
        products = [
            make_product(
                asin=f"SL{i:02d}", price=float(i), series_name="", series_position=""
            )
            for i in range(1, 36)
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 35)])
        out_file = tmp_config / "search_default_limit.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "test",
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
        assert len(data) == 25

    def test_search_limit_zero_means_unlimited(self, mock_client, tmp_config):
        """search -n 0 shows all results (unlimited)."""
        products = [
            make_product(
                asin=f"SL{i:02d}", price=float(i), series_name="", series_position=""
            )
            for i in range(1, 36)
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 35)])
        out_file = tmp_config / "search_unlimited.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "test",
                "--pages",
                "1",
                "-n",
                "0",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 35


class TestFindDefaults:
    def test_find_default_sort_price_per_hour(self, mock_client, tmp_config):
        """find without --sort uses price-per-hour ordering."""
        products = [
            # A: $10 / 2hrs = $5/hr
            make_product(
                asin="PPH_A",
                price=10.0,
                length_minutes=120,
                series_name="",
                series_position="",
                num_ratings=10,
            ),
            # B: $3 / 10hrs = $0.30/hr (better value)
            make_product(
                asin="PPH_B",
                price=3.0,
                length_minutes=600,
                series_name="",
                series_position="",
                num_ratings=10,
            ),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "pph_sort.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "100",
                "--pages",
                "1",
                "-n",
                "0",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        # PPH_B has lower price-per-hour and should appear first
        assert data[0]["asin"] == "PPH_B"
        assert data[1]["asin"] == "PPH_A"

    def test_find_default_min_ratings_filters_unreviewed(self, mock_client, tmp_config):
        """find with default min-ratings=1 filters out items with 0 ratings."""
        products = [
            make_product(
                asin="MR1", price=3.0, num_ratings=0, series_name="", series_position=""
            ),
            make_product(
                asin="MR2", price=3.0, num_ratings=5, series_name="", series_position=""
            ),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "min_ratings.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "-n",
                "0",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "MR1" not in asins
        assert "MR2" in asins


class TestExcludeNarratorFilter:
    def test_exclude_narrator_single(self):
        products = [
            make_product(asin="EN1", narrators=["R.C. Bray"]),
            make_product(asin="EN2", narrators=["Scott Brick"]),
        ]
        filtered, breakdown = _filter_products(
            products, exclude_narrators=("R.C. Bray",)
        )
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
            make_product(
                asin="FEN1",
                price=3.0,
                narrators=["R.C. Bray"],
                series_name="",
                series_position="",
                num_ratings=10,
            ),
            make_product(
                asin="FEN2",
                price=3.0,
                narrators=["Scott Brick"],
                series_name="",
                series_position="",
                num_ratings=10,
            ),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "find_excl_narrator.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--exclude-narrator",
                "bray",
                "--all-languages",
                "-q",
                "-n",
                "0",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "FEN1" not in asins
        assert "FEN2" in asins

    def test_last_exclude_narrator_flag(self, tmp_config):

        products = [
            make_product(
                asin="LEN1",
                price=3.0,
                narrators=["R.C. Bray"],
                series_name="",
                series_position="",
            ),
            make_product(
                asin="LEN2",
                price=3.0,
                narrators=["Scott Brick"],
                series_name="",
                series_position="",
            ),
        ]
        constants_mod.LAST_RESULTS_FILE.write_text(
            json.dumps([_serialize_product(p) for p in products])
        )
        out_file = tmp_config / "last_excl_narrator.json"
        runner = CliRunner()
        result = runner.invoke(
            cli, ["last", "--exclude-narrator", "bray", "--output", str(out_file)]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LEN1" not in asins
        assert "LEN2" in asins

    def test_exclude_narrator_in_profile(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "profile",
                "save",
                "no-bray",
                "--exclude-narrator",
                "R.C. Bray",
            ],
        )
        assert result.exit_code == 0, result.output

        profiles = config_store_mod.load_profiles()
        assert "no-bray" in profiles
        excluded = profiles["no-bray"]["exclude_narrators"]
        assert "R.C. Bray" in excluded


class TestSearchQueryOptional:
    def test_search_no_query_no_genre_raises(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["search"])
        assert result.exit_code != 0

    def test_search_with_genre_no_query(self, mock_client, tmp_config):
        products = [
            make_product(asin="SG1", price=5.0, series_name="", series_position="")
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        mock_client.resolve_genre.return_value = ("cat99", "Mystery")
        out_file = tmp_config / "search_genre.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "--genre",
                "mystery",
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
        assert len(data) == 1

    def test_search_with_category_no_query(self, mock_client, tmp_config):
        products = [
            make_product(asin="SC1", price=5.0, series_name="", series_position="")
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        mock_client.get_category_name.return_value = "Thriller"
        out_file = tmp_config / "search_cat.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "--category",
                "123456",
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
        assert len(data) == 1

    def test_search_with_query_still_works(self, mock_client, tmp_config):
        products = [
            make_product(asin="SQ1", price=5.0, series_name="", series_position="")
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        out_file = tmp_config / "search_q.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "test query",
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
        assert len(data) == 1


class TestDynamicTitleColumnWidth:
    def test_narrow_terminal_uses_minimum(self):
        """On a narrow terminal (e.g. 80 chars), the title remains readable."""
        from io import StringIO

        from rich.console import Console

        from audible_deals.presentation import products as display_mod
        from audible_deals.presentation import terminal

        buf = StringIO()
        narrow_console = Console(file=buf, width=80, force_terminal=False)
        original = terminal.console
        terminal.console = narrow_console
        try:
            products = [make_product(asin="TW1", title="A Book", price=3.0)]
            display_mod.display_products(products, title="Test")
        finally:
            terminal.console = original
        out = buf.getvalue()
        assert "A Book" in out

    def test_wide_terminal_uses_larger_width(self):
        """On a wide terminal (e.g. 200 chars), title column should be wider."""
        from io import StringIO

        from rich.console import Console

        from audible_deals.presentation import products as display_mod
        from audible_deals.presentation import terminal

        buf = StringIO()
        wide_console = Console(file=buf, width=200, force_terminal=False)
        original = terminal.console
        terminal.console = wide_console
        try:
            long_title = "A" * 70
            products = [make_product(asin="TW2", title=long_title, price=3.0)]
            display_mod.display_products(products, title="Test")
        finally:
            terminal.console = original
        out = buf.getvalue()
        # The output should contain at least part of the long title
        assert "TW2" in out


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
            make_product(
                asin=f"SQ{i:02d}", price=float(i), series_name="", series_position=""
            )
            for i in range(1, 41)
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 40)])
        out_file = tmp_config / "search_def.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "test",
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
        assert len(data) == 25


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


class TestSearchAuthorHint:
    def test_hint_shown_for_exact_fetched_author(self, mock_client, tmp_config):
        """search shows --author tip when fetched data confirms the author."""
        products = [
            make_product(
                asin="AH1",
                price=5.0,
                authors=["Andy Weir"],
                series_name="",
                series_position="",
            )
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "Andy Weir",
                "--pages",
                "1",
                "--all-languages",
                "-n",
                "0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "--author" in result.output
        assert "Andy Weir" in result.output

    def test_hint_not_shown_without_author_evidence(self, mock_client, tmp_config):
        products = [
            make_product(
                asin="AHNO1",
                title="Andy Weir: A Biography",
                authors=["Someone Else"],
                series_name="",
                series_position="",
            )
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])

        result = CliRunner().invoke(
            cli,
            [
                "search",
                "Andy Weir",
                "--pages",
                "1",
                "--all-languages",
                "-n",
                "0",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Tip:" not in result.output

    def test_hint_not_shown_when_author_already_set(self, mock_client, tmp_config):
        """search does NOT show tip when --author is already used."""
        products = [
            make_product(asin="AH2", price=5.0, series_name="", series_position="")
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "Andy Weir",
                "--author",
                "Andy Weir",
                "--pages",
                "1",
                "--all-languages",
                "-n",
                "0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Tip:" not in result.output

    def test_hint_not_shown_for_non_name_query(self, mock_client, tmp_config):
        """search does NOT show tip for a single-word query."""
        products = [
            make_product(asin="AH3", price=5.0, series_name="", series_position="")
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "Dune",
                "--pages",
                "1",
                "--all-languages",
                "-n",
                "0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Tip:" not in result.output

    def test_hint_not_shown_with_quiet(self, mock_client, tmp_config):
        """search does NOT show tip in quiet mode."""
        products = [
            make_product(asin="AH4", price=5.0, series_name="", series_position="")
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "Andy Weir",
                "--pages",
                "1",
                "--all-languages",
                "-q",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Tip:" not in result.output


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
        """Free items (price=0.0) with valid rating and hours return inf."""
        p = make_product(price=0.0, length_minutes=600, rating=4.5)
        assert _value_score(p) == float("inf")

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


class TestMinDiscount:
    def test_filters_low_discount(self):
        products = [
            make_product(asin="HIGH", price=3.0, list_price=20.0),  # 85% off
            make_product(asin="LOW", price=8.0, list_price=10.0),  # 20% off
            make_product(asin="NONE", price=5.0, list_price=5.0),  # 0% off
        ]
        filtered, breakdown = _filter_products(products, min_discount=50)
        assert len(filtered) == 1
        assert filtered[0].asin == "HIGH"
        assert "min discount" in breakdown

    def test_no_filter_when_zero(self):
        products = [make_product(price=5.0, list_price=5.0)]
        filtered, breakdown = _filter_products(products, min_discount=0)
        assert len(filtered) == 1


class TestValueSortTiebreaker:
    def test_tiebreaker_by_rating(self):
        """Items with same value score should sort by rating."""
        # score 0.0 because rating == 0
        unrated = make_product(
            asin="UNRATED", price=5.0, length_minutes=600, rating=0.0
        )
        # score 0.0 because hours == 0
        zero_hrs = make_product(asin="ZEROHRS", price=5.0, length_minutes=0, rating=4.5)
        result = _sort_local([unrated, zero_hrs], "value")
        # zero_hrs has rating 4.5 > 0.0, so it should come first
        assert result[0].asin == "ZEROHRS"
        assert result[1].asin == "UNRATED"


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
        mock_client._categories_cache = [
            {"id": "cat1", "name": "Mystery, Thriller & Suspense"}
        ]
        mock_client.resolve_genre.return_value = (
            "cat1",
            "Mystery, Thriller & Suspense",
        )
        mock_client.__enter__ = lambda s: s
        mock_client.__exit__ = lambda s, *a: False

        # Bypass real genre resolution by using --category
        runner = CliRunner()
        result = runner.invoke(
            cli, ["find", "--dry-run", "--pages", "2", "--category", "cat1"]
        )
        assert result.exit_code == 0, result.output
        assert "Dry run" in result.output
        mock_client.search_pages.assert_not_called()

    def test_find_dry_run_with_category_never_constructs_client(
        self, tmp_config, monkeypatch
    ):
        """Dry runs do not resolve categories through the authenticated client."""
        import audible_deals.cli.catalog as catalog_mod

        monkeypatch.setattr(
            catalog_mod,
            "_get_client",
            lambda locale: pytest.fail("dry run constructed a client"),
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--category", "cat1", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "resolved during scan" in result.output

    def test_dry_run_shows_effective_settings_and_filters(self, tmp_config):
        config_store_mod.save_config(
            {"max_price": 9.0, "sort": "rating", "limit": 40, "skip_owned": True}
        )
        config_store_mod.save_profiles(
            {
                "strict": {
                    "max_price": 7.0,
                    "sort": "title",
                    "limit": 10,
                    "min_rating": 4.2,
                    "exclude_authors": ["Blocked Author"],
                }
            }
        )

        result = CliRunner().invoke(
            cli,
            [
                "find",
                "--profile",
                "strict",
                "--max-price",
                "5",
                "--sort",
                "discount",
                "--limit",
                "3",
                "--on-sale",
                "--released-after",
                "2025-01-01",
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Result sort: discount" in result.output
        assert "Limit: 3" in result.output
        assert "Profile: strict" in result.output
        assert "max-price=5.0" in result.output
        assert "min-rating=4.2" in result.output
        assert "on-sale=yes" in result.output
        assert "skip-owned=yes" in result.output
        assert "exclude-authors=Blocked Author" in result.output
        assert "released-after=2025-01-01" in result.output
        filters = result.output.split("Filters: ", 1)[1].splitlines()[0]
        assert "; " in filters


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

    def test_search_dry_run_rejects_empty_or_query_before_planning(
        self, tmp_config, monkeypatch
    ):
        import audible_deals.cli.catalog as catalog_mod

        monkeypatch.setattr(
            catalog_mod,
            "_get_client",
            lambda locale: pytest.fail("dry run constructed a client"),
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["search", " | ", "--dry-run"])
        assert result.exit_code != 0
        assert "No keywords found" in result.output

    def test_find_dry_run_subcategories_marks_live_count_unknown(
        self, tmp_config, monkeypatch
    ):
        import audible_deals.cli.catalog as catalog_mod

        monkeypatch.setattr(
            catalog_mod,
            "_get_client",
            lambda locale: pytest.fail("dry run constructed a client"),
        )
        runner = CliRunner()
        result = runner.invoke(
            cli, ["find", "--genre", "fantasy", "--subcategories", "--dry-run"]
        )
        assert result.exit_code == 0, result.output
        assert "Subcategories: unknown" in result.output


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


class TestSeriesCommand:
    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        monkeypatch.setattr("audible_deals.cli.series.time.sleep", lambda _: None)

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
        # Progress bar may leak into stdout in test; extract JSON portion
        json_start = result.output.index("[")
        data = json.loads(result.output[json_start:])
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


class TestPlusCatalogFilter:
    def test_skip_plus_excludes_plus_titles(self):
        products = [
            make_product(asin="P1", in_plus_catalog=True),
            make_product(asin="P2", in_plus_catalog=False),
        ]
        filtered, breakdown = _filter_products(products, skip_plus=True)
        assert len(filtered) == 1
        assert filtered[0].asin == "P2"
        assert breakdown.get("plus catalog") == 1

    def test_only_plus_keeps_only_plus(self):
        products = [
            make_product(asin="P1", in_plus_catalog=True),
            make_product(asin="P2", in_plus_catalog=False),
        ]
        filtered, breakdown = _filter_products(products, only_plus=True)
        assert len(filtered) == 1
        assert filtered[0].asin == "P1"
        assert breakdown.get("not plus") == 1

    def test_neither_flag_passes_all(self):
        products = [
            make_product(asin="P1", in_plus_catalog=True),
            make_product(asin="P2", in_plus_catalog=False),
        ]
        filtered, breakdown = _filter_products(products)
        assert len(filtered) == 2
        assert "plus catalog" not in breakdown
        assert "not plus" not in breakdown

    def test_find_skip_plus(self, mock_client, tmp_config):
        products = [
            make_product(
                asin="SP1",
                price=3.0,
                in_plus_catalog=True,
                series_name="",
                series_position="",
            ),
            make_product(
                asin="SP2",
                price=3.0,
                in_plus_catalog=False,
                series_name="",
                series_position="",
            ),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "skip_plus.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--all-languages",
                "--skip-plus",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "SP1" not in asins
        assert "SP2" in asins

    def test_find_only_plus(self, mock_client, tmp_config):
        products = [
            make_product(
                asin="OP1",
                price=3.0,
                in_plus_catalog=True,
                series_name="",
                series_position="",
            ),
            make_product(
                asin="OP2",
                price=3.0,
                in_plus_catalog=False,
                series_name="",
                series_position="",
            ),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "only_plus.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--all-languages",
                "--only-plus",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "OP1" in asins
        assert "OP2" not in asins

    def test_find_skip_plus_and_only_plus_mutually_exclusive(self):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--skip-plus",
                "--only-plus",
            ],
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output


class TestExcludeKeyword:
    def test_exclude_keyword_by_title(self):
        products = [
            make_product(asin="EK1", title="Foo Box Set"),
            make_product(asin="EK2", title="Foo Complete Edition"),
        ]
        filtered, breakdown = _filter_products(products, exclude_keywords=("box set",))
        asins = [p.asin for p in filtered]
        assert "EK1" not in asins
        assert "EK2" in asins
        assert breakdown.get("excluded keywords") == 1

    def test_exclude_keyword_case_insensitive(self):
        products = [make_product(asin="EK3", title="ABRIDGED VERSION")]
        filtered, _ = _filter_products(products, exclude_keywords=("abridged",))
        assert len(filtered) == 0

    def test_exclude_keyword_subtitle_match(self):
        products = [
            make_product(asin="EK4", title="Good Book", subtitle="Abridged Edition")
        ]
        filtered, _ = _filter_products(products, exclude_keywords=("abridged",))
        assert len(filtered) == 0

    def test_exclude_multiple_keywords(self):
        products = [
            make_product(asin="EK5", title="Box Set Collection"),
            make_product(asin="EK6", title="Abridged Cut"),
            make_product(asin="EK7", title="Full Novel"),
        ]
        filtered, breakdown = _filter_products(
            products, exclude_keywords=("abridged", "box set")
        )
        asins = [p.asin for p in filtered]
        assert "EK5" not in asins
        assert "EK6" not in asins
        assert "EK7" in asins
        assert breakdown.get("excluded keywords") == 2

    def test_find_exclude_keyword_flag(self, mock_client, tmp_config):
        products = [
            make_product(
                asin="FEK1",
                price=3.0,
                title="Abridged Story",
                series_name="",
                series_position="",
            ),
            make_product(
                asin="FEK2",
                price=3.0,
                title="Full Story",
                series_name="",
                series_position="",
            ),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "excl_kw.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--all-languages",
                "--exclude-keyword",
                "abridged",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "FEK1" not in asins
        assert "FEK2" in asins


class TestFindSubcategories:
    def _make_search_side_effect(self, products_by_call: list[list]):
        """Return a side_effect that yields successive product lists."""
        call_idx = 0

        def _side_effect(**kwargs):
            nonlocal call_idx
            batch = products_by_call[call_idx % len(products_by_call)]
            call_idx += 1
            yield batch, 1, len(batch)

        return _side_effect

    def test_subcategories_scans_each_child(self, mock_client, tmp_config):
        """--subcategories calls get_categories and scans each child id."""
        child1 = make_product(
            asin="SUB1", price=2.0, series_name="", series_position=""
        )
        child2 = make_product(
            asin="SUB2", price=3.0, series_name="", series_position=""
        )

        mock_client.resolve_genre.return_value = ("parent1", "Sci-Fi")
        mock_client.get_categories.return_value = [
            {"id": "child1", "name": "Space Opera"},
            {"id": "child2", "name": "Cyberpunk"},
        ]

        call_order: list[str] = []

        def fake_search_pages(**kwargs):
            call_order.append(kwargs["category_id"])
            batch = [child1] if kwargs["category_id"] == "child1" else [child2]
            yield batch, 1, 1

        mock_client.search_pages.side_effect = fake_search_pages

        out_file = tmp_config / "sub.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--genre",
                "sci-fi",
                "--subcategories",
                "--pages",
                "1",
                "--max-price",
                "10",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        mock_client.get_categories.assert_called_once_with(root="parent1")
        assert set(call_order) == {"child1", "child2"}
        data = json.loads(out_file.read_text())
        asins = {d["asin"] for d in data}
        assert asins == {"SUB1", "SUB2"}

    def test_subcategories_no_children_falls_back(self, mock_client, tmp_config):
        """No children → scans the parent and prints the notice."""
        products = [
            make_product(asin="FB1", price=2.0, series_name="", series_position="")
        ]

        mock_client.resolve_genre.return_value = ("parent2", "Mystery")
        mock_client.get_categories.return_value = []
        mock_client.search_pages.return_value = iter([(products, 1, 1)])

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--genre",
                "mystery",
                "--subcategories",
                "--pages",
                "1",
                "--max-price",
                "10",
                "--all-languages",
                "-q",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "No subcategories found" in result.output
        called_ids = [
            c.kwargs["category_id"] for c in mock_client.search_pages.call_args_list
        ]
        assert called_ids == ["parent2"]

    def test_subcategories_without_genre_raises(self, mock_client, tmp_config):
        """--subcategories without --genre/--category raises UsageError."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--subcategories",
                "--pages",
                "1",
                "--max-price",
                "10",
            ],
        )
        assert result.exit_code != 0
        assert "--subcategories requires --genre or --category" in result.output

    def test_subcategories_dedup_across_children(self, mock_client, tmp_config):
        """Same ASIN in two subcategories appears only once."""
        shared = make_product(
            asin="DEDUP", price=2.0, series_name="", series_position=""
        )
        unique = make_product(
            asin="UNIQ", price=3.0, series_name="", series_position=""
        )

        mock_client.resolve_genre.return_value = ("parent3", "Fantasy")
        mock_client.get_categories.return_value = [
            {"id": "c1", "name": "Epic Fantasy"},
            {"id": "c2", "name": "Urban Fantasy"},
        ]

        def fake_search_pages(**kwargs):
            if kwargs["category_id"] == "c1":
                yield [shared, unique], 1, 2
            else:
                yield [shared], 1, 1

        mock_client.search_pages.side_effect = fake_search_pages

        out_file = tmp_config / "dedup.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--genre",
                "fantasy",
                "--subcategories",
                "--pages",
                "1",
                "--max-price",
                "10",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert asins.count("DEDUP") == 1
        assert "UNIQ" in asins

    def test_subcategories_dry_run_marks_live_counts_unknown(
        self, mock_client, tmp_config
    ):
        """Dry run with subcategories avoids fetching the live category tree."""
        mock_client.resolve_genre.return_value = ("parent4", "Romance")
        mock_client.get_categories.return_value = [
            {"id": "r1", "name": "Contemporary"},
            {"id": "r2", "name": "Historical"},
            {"id": "r3", "name": "Paranormal"},
        ]

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--genre",
                "romance",
                "--subcategories",
                "--pages",
                "2",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Subcategories: unknown" in result.output
        assert "API calls: unknown" in result.output
        mock_client.get_categories.assert_not_called()


class TestRequireHistoryCLI:
    def test_require_history_without_hist_filter_raises_usage_error_find(
        self, tmp_config
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--require-history"])
        assert result.exit_code == 2
        assert "--require-history requires" in result.output

    def test_require_history_without_hist_filter_raises_usage_error_search(
        self, tmp_config
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "test", "--require-history"])
        assert result.exit_code == 2
        assert "--require-history requires" in result.output

    def test_require_history_with_hist_below_accepted(self, mock_client, tmp_config):
        mock_client.search_pages.return_value = iter([([], 1, 0)])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--require-history",
                "--hist-below",
                "50",
                "--pages",
                "1",
                "-q",
            ],
        )
        assert result.exit_code == 0, result.output

    def test_require_history_with_min_price_drop_accepted(
        self, mock_client, tmp_config
    ):
        mock_client.search_pages.return_value = iter([([], 1, 0)])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--require-history",
                "--min-price-drop",
                "10",
                "--pages",
                "1",
                "-q",
            ],
        )
        assert result.exit_code == 0, result.output


class TestReleasedDateCLI:
    def test_invalid_released_after_raises_usage_error(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--released-after", "not-a-date"])
        assert result.exit_code == 2
        assert "invalid date" in result.output

    def test_invalid_released_before_raises_usage_error(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--released-before", "2024/01/01"])
        assert result.exit_code == 2
        assert "invalid date" in result.output

    def test_valid_released_after_accepted(self, mock_client, tmp_config):
        mock_client.search_pages.return_value = iter([([], 1, 0)])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["find", "--released-after", "2024-01-01", "--pages", "1", "-q"],
        )
        assert result.exit_code == 0, result.output

    def test_valid_released_before_accepted(self, mock_client, tmp_config):
        mock_client.search_pages.return_value = iter([([], 1, 0)])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["find", "--released-before", "2024-12-31", "--pages", "1", "-q"],
        )
        assert result.exit_code == 0, result.output

    def test_invalid_released_after_search_raises_usage_error(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "test", "--released-after", "bad"])
        assert result.exit_code == 2
        assert "invalid date" in result.output

    @pytest.mark.parametrize("command", [["find"], ["search", "test"]])
    def test_inverted_release_window_is_rejected_before_client_creation(
        self, command, tmp_config, monkeypatch
    ):
        import audible_deals.cli.catalog as catalog_mod

        monkeypatch.setattr(
            catalog_mod,
            "_get_client",
            lambda locale: pytest.fail("inverted dates constructed a client"),
        )
        result = CliRunner().invoke(
            cli,
            [
                *command,
                "--released-after",
                "2025-01-02",
                "--released-before",
                "2025-01-01",
                "--dry-run",
            ],
        )

        assert result.exit_code == 2
        assert "cannot be later" in result.output

    @pytest.mark.parametrize("command", [["find"], ["search", "test"]])
    def test_equal_release_bounds_are_valid(self, command, tmp_config):
        result = CliRunner().invoke(
            cli,
            [
                *command,
                "--released-after",
                "2025-01-01",
                "--released-before",
                "2025-01-01",
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output


class TestReleasedDateNormalization:
    def test_compact_released_after_is_normalized(self, mock_client, tmp_config):
        """Compact ISO form '20240101' parses and normalizes to '2024-01-01'."""
        from audible_deals.cli.catalog import _validate_history_filter_options

        after, before = _validate_history_filter_options(
            False, None, 0.0, "20240101", ""
        )
        assert after == "2024-01-01"
        assert before == ""

    def test_compact_released_before_is_normalized(self, mock_client, tmp_config):
        """Compact ISO form '20241231' normalizes to '2024-12-31'."""
        from audible_deals.cli.catalog import _validate_history_filter_options

        after, before = _validate_history_filter_options(
            False, None, 0.0, "", "20241231"
        )
        assert after == ""
        assert before == "2024-12-31"

    def test_dashed_dates_pass_through_unchanged(self, mock_client, tmp_config):
        """Standard dashed dates are returned as-is."""
        from audible_deals.cli.catalog import _validate_history_filter_options

        after, before = _validate_history_filter_options(
            False, None, 0.0, "2024-06-01", "2024-12-31"
        )
        assert after == "2024-06-01"
        assert before == "2024-12-31"


class TestCreditAdviceInFind:
    def test_buy_column_with_config(self, mock_client, tmp_config):
        config_store_mod.save_config({"credit_price": 11.25})
        products = [
            make_product(asin="CR1", price=24.99, series_name="", series_position=""),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        runner = CliRunner()
        result = runner.invoke(
            cli, ["find", "--pages", "1", "--all-languages", "--max-price", "30"]
        )
        assert result.exit_code == 0, result.output
        assert "Buy" in result.output
        assert "credit" in result.output

    def test_no_buy_column_without_config(self, mock_client, tmp_config):
        products = [
            make_product(asin="CR2", price=3.99, series_name="", series_position=""),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--pages", "1", "--all-languages"])
        assert result.exit_code == 0, result.output
        assert "Buy" not in result.output

    def test_max_effective_price_filter(self, mock_client, tmp_config):
        config_store_mod.save_config({"credit_price": 11.25})
        products = [
            make_product(asin="CR3", price=24.99, series_name="", series_position=""),
            make_product(asin="CR4", price=14.99, series_name="", series_position=""),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        out_file = tmp_config / "eff.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--pages",
                "1",
                "--all-languages",
                "--max-price",
                "30",
                "--max-effective-price",
                "12",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        asins = [d["asin"] for d in json.loads(out_file.read_text())]
        # Both cost one credit (11.25 effective), so both pass the 12 cap
        assert asins == ["CR3", "CR4"] or set(asins) == {"CR3", "CR4"}


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


class TestHistoricalMedianBadgeFlagIndependence:
    def test_vs_median_independent_of_hist_below_flag(
        self, mock_client, tmp_config, monkeypatch
    ):
        # Exactly 2 prior on-disk entries. The 'vs median' badge requires >=3
        # entries; today's just-recorded price must be excluded (matching ATL),
        # so the badge must be absent in BOTH runs regardless of --hist-below.
        product = make_product(asin="F1", price=5.0, series_name="", series_position="")

        def reset_and_run(args):
            _seed_price_history("F1", [9.0, 8.0])
            mock_client.search_pages.return_value = iter([([product], 1, 1)])
            captured = _capture_history_context(monkeypatch)
            runner = CliRunner()
            result = runner.invoke(cli, args)
            assert result.exit_code == 0, result.output
            return captured["hist_context"]

        plain = reset_and_run(["find", "--pages", "1"])
        with_flag = reset_and_run(["find", "--pages", "1", "--hist-below", "100"])

        assert plain == with_flag
        assert "F1" not in plain


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
        payload = json.loads(result.output[result.output.index("{") :])
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
        payload = json.loads(result.output[result.output.index("[") :])
        assert isinstance(payload, list)
        assert {p["asin"] for p in payload} == {"JS3", "JS4"}
