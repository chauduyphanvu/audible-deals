"""Cached-result and history CLI behavior."""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

import audible_deals.constants as constants_mod
from audible_deals.cli import cli
from audible_deals.results_cache import (
    load_dismissed_asins,
    load_result_session,
    load_seen_asins as _load_seen_asins,
    save_dismissed_asins,
)
from audible_deals.results_cache import (
    save_seen_asins as _save_seen_asins,
)
from audible_deals.selectors import resolve_last_references
from audible_deals.selectors import resolve_last_references as _resolve_last_references
from audible_deals.serialization import (
    serialize_product as _serialize_product,
)
from tests.conftest import make_product


def _routes_run(runner, args, **kwargs):
    """Invoke the CLI and return the result; fail on unexpected errors."""
    result = runner.invoke(cli, args, catch_exceptions=False, **kwargs)
    return result


def _routes_seed_last_results(tmp_config, products):
    """Write a last_results.json cache file."""
    data = {
        "title": "Test Results",
        "results": [_serialize_product(p) for p in products],
    }
    (tmp_config / "last_results.json").write_text(json.dumps(data))


class TestCachedResultRegressions:
    def test_last_does_not_record_cached_prices(self, tmp_config, monkeypatch):
        import audible_deals.result_publication as publication_mod

        constants_mod.LAST_RESULTS_FILE.write_text(
            json.dumps(
                {
                    "title": "Cached",
                    "results": [_serialize_product(make_product(price=4.99))],
                }
            )
        )
        record_prices = monkeypatch.setattr(
            publication_mod,
            "record_prices_safely",
            lambda products: pytest.fail("last recorded cached prices"),
        )
        assert record_prices is None
        result = CliRunner().invoke(cli, ["last", "--json"])
        assert result.exit_code == 0, result.output

    def test_last_rejects_bad_output_before_reading_cache(
        self, tmp_config, monkeypatch
    ):
        import audible_deals.cli.last as last_mod

        monkeypatch.setattr(
            last_mod,
            "load_result_session",
            lambda: pytest.fail("read cache before validating output"),
        )
        result = CliRunner().invoke(cli, ["last", "--output", "bad.txt"])
        assert result.exit_code != 0
        assert "Unsupported extension" in result.output

    def test_last_plus_patch_overrides_inherited_opposite_and_rejects_both(
        self, tmp_config, mock_client
    ):
        product = make_product(
            asin="LASTPLUS", price=3.0, in_plus_catalog=True, series_name=""
        )
        mock_client.search_pages.return_value = iter([([product], 1, 1)])
        runner = CliRunner()
        seeded = runner.invoke(
            cli,
            ["find", "--pages", "1", "--all-languages", "--only-plus", "-q"],
        )
        assert seeded.exit_code == 0, seeded.output

        refined = runner.invoke(cli, ["last", "--skip-plus", "--count"])
        assert refined.exit_code == 0, refined.output
        recipe = load_result_session().current_recipe
        assert (recipe.skip_plus, recipe.only_plus) == (True, False)
        before = constants_mod.LAST_RESULTS_FILE.read_text()

        conflicting = runner.invoke(cli, ["last", "--skip-plus", "--only-plus"])
        assert conflicting.exit_code != 0
        assert "mutually exclusive" in conflicting.output
        assert constants_mod.LAST_RESULTS_FILE.read_text() == before


class TestHistoryCommand:
    def test_no_history(self, tmp_config, mock_client):
        runner = CliRunner()
        result = runner.invoke(cli, ["history", "NOPE"])
        assert result.exit_code == 0, result.output
        assert "No price history" in result.output

    def test_history_after_recording(self, tmp_config, mock_client):
        from audible_deals.price_history import record_prices as _record_prices

        products = [make_product(asin="H1", price=5.99)]
        _record_prices(products)

        runner = CliRunner()
        result = runner.invoke(cli, ["history", "H1"])
        assert result.exit_code == 0, result.output
        assert "$5.99" in result.output

    def test_history_idempotent(self, tmp_config):
        from audible_deals.price_history import record_prices as _record_prices

        products = [make_product(asin="H2", price=3.00)]
        _record_prices(products)
        _record_prices(products)  # Same day

        hist_file = tmp_config / "history" / "H2.json"
        entries = json.loads(hist_file.read_text())
        assert len(entries) == 1

    def test_dry_run_without_purge_is_error(self, tmp_config, mock_client):
        """--dry-run without --purge-older-than raises UsageError."""
        runner = CliRunner()
        result = runner.invoke(cli, ["history", "B00EXAMPLE1", "--dry-run"])
        assert result.exit_code != 0
        assert "purge" in result.output.lower() or "usage" in result.output.lower()

    def test_yes_without_purge_is_error(self, tmp_config, mock_client):
        """--yes without --purge-older-than raises UsageError."""
        runner = CliRunner()
        result = runner.invoke(cli, ["history", "B00EXAMPLE1", "--yes"])
        assert result.exit_code != 0
        assert "purge" in result.output.lower() or "usage" in result.output.lower()


class TestResolveLastReferences:
    def test_valid_reference(self, tmp_config):

        p = make_product(asin="REF1")
        data = [_serialize_product(p)]
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(data))
        results = _resolve_last_references((1,))
        assert len(results) == 1
        asin, desc = results[0]
        assert asin == "REF1"
        assert "REF1" in desc
        assert "Result #1" in desc

    def test_multiple_references(self, tmp_config):

        products = [make_product(asin=f"R{i}") for i in range(1, 4)]
        data = [_serialize_product(p) for p in products]
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(data))
        results = _resolve_last_references((1, 3))
        asins = [r[0] for r in results]
        assert asins == ["R1", "R3"]

    def test_out_of_range(self, tmp_config):

        data = [_serialize_product(make_product(asin="ONLY1"))]
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(data))
        with pytest.raises(click.ClickException, match="out of range"):
            _resolve_last_references((5,))

    def test_missing_file(self, tmp_config):
        with pytest.raises(click.ClickException, match="No cached results"):
            _resolve_last_references((1,))

    def test_corrupt_file(self, tmp_config):

        constants_mod.LAST_RESULTS_FILE.write_text("not-json{{{{")
        with pytest.raises(click.ClickException, match="Could not read"):
            _resolve_last_references((1,))


class TestLastCommand:
    def _seed_cache(self, tmp_config, products):

        data = [_serialize_product(p) for p in products]
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(data))

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
            make_product(
                asin="LS1",
                price=5.0,
                list_price=10.0,
                series_name="",
                series_position="",
            ),
            make_product(
                asin="LS2",
                price=3.0,
                list_price=3.0,
                series_name="",
                series_position="",
            ),  # 0% discount
        ]
        self._seed_cache(tmp_config, products)
        out_file = tmp_config / "last_sort.json"
        runner = CliRunner()
        result = runner.invoke(
            cli, ["last", "--sort", "discount", "--output", str(out_file)]
        )
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
        result = runner.invoke(
            cli, ["last", "--max-price", "5", "--output", str(out_file)]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LF1" in asins
        assert "LF2" not in asins

    def test_last_output_implies_quiet(self, tmp_config):
        products = [
            make_product(asin="LQ1", price=3.0, series_name="", series_position="")
        ]
        self._seed_cache(tmp_config, products)
        out_file = tmp_config / "last_quiet.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--output", str(out_file)])
        assert result.exit_code == 0, result.output
        assert "Last results" not in result.output

    def test_last_preserves_legacy_candidate_pool(self, tmp_config):
        """Cached narrowing keeps every legacy candidate available for widening."""
        products = [
            make_product(asin="NC1", price=2.0, series_name="", series_position=""),
            make_product(asin="NC2", price=8.0, series_name="", series_position=""),
        ]
        self._seed_cache(tmp_config, products)

        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--max-price", "5"])
        assert result.exit_code == 0, result.output
        from audible_deals.results_cache import load_result_session

        session = load_result_session()
        assert [item["asin"] for item in session.candidates] == ["NC1", "NC2"]
        assert session.visible_asins == ["NC1"]
        assert session.current_recipe.max_price == 5


class TestDetailLastFlag:
    def test_detail_last(self, mock_client, tmp_config):
        products = [make_product(asin="DL1")]

        constants_mod.LAST_RESULTS_FILE.write_text(
            json.dumps([_serialize_product(p) for p in products])
        )
        mock_client.get_product.return_value = make_product(
            asin="DL1", title="Detail Last"
        )
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

        constants_mod.LAST_RESULTS_FILE.write_text(
            json.dumps([_serialize_product(p) for p in products])
        )
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

        constants_mod.LAST_RESULTS_FILE.write_text(
            json.dumps([_serialize_product(p) for p in products])
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="CM1", title="Book 1", price=5.0, length_minutes=600),
            make_product(asin="CM2", title="Book 2", price=8.0, length_minutes=600),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["compare", "CM1", "--last", "1"])
        assert result.exit_code == 0, result.output


class TestLastQueryContext:
    def test_new_cache_format_stores_title(self, mock_client, tmp_config):
        """find writes new-format cache with title and results."""
        products = [
            make_product(asin="QC1", price=3.0, series_name="", series_position="")
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
                "-q",
            ],
        )
        assert result.exit_code == 0, result.output

        raw = json.loads(constants_mod.LAST_RESULTS_FILE.read_text())
        assert isinstance(raw, dict)
        assert "title" in raw
        assert "results" in raw
        assert isinstance(raw["results"], list)
        assert raw["title"] != ""

    def test_last_shows_original_title(self, mock_client, tmp_config):
        """deals last shows the title from the cached query."""

        products = [
            make_product(asin="QT1", price=3.0, series_name="", series_position="")
        ]
        cache_obj = {
            "title": "Deals under $5.00 in Sci-Fi",
            "results": [_serialize_product(p) for p in products],
        }
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))
        runner = CliRunner()
        result = runner.invoke(cli, ["last"])
        assert result.exit_code == 0, result.output
        assert "Deals under $5.00 in Sci-Fi" in result.output

    def test_backward_compat_plain_list(self, tmp_config):
        """deals last handles old plain-list cache format gracefully."""

        products = [
            make_product(asin="BC1", price=3.0, series_name="", series_position="")
        ]
        # Old format: plain list
        constants_mod.LAST_RESULTS_FILE.write_text(
            json.dumps([_serialize_product(p) for p in products])
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["last"])
        assert result.exit_code == 0, result.output
        assert "Last results" in result.output

    def test_title_sort_is_alphabetical_and_uses_only_cached_results(
        self, tmp_config, monkeypatch
    ):
        import audible_deals.cli.helpers as helpers_mod

        products = [
            make_product(asin="Z1", title="zebra", series_name="", series_position=""),
            make_product(asin="A1", title="Alpha", series_name="", series_position=""),
        ]
        constants_mod.LAST_RESULTS_FILE.write_text(
            json.dumps([_serialize_product(product) for product in products])
        )
        monkeypatch.setattr(
            helpers_mod,
            "_get_client",
            lambda locale: pytest.fail("last constructed an API client"),
        )

        result = CliRunner().invoke(cli, ["last", "--sort", "title", "--json"])

        assert result.exit_code == 0, result.output
        assert [item["title"] for item in json.loads(result.output)] == [
            "Alpha",
            "zebra",
        ]

    def test_corrupt_cache_raises(self, tmp_config):
        """deals last raises ClickException for a corrupt (non-list, non-dict) cache."""

        constants_mod.LAST_RESULTS_FILE.write_text('"just a string"')
        runner = CliRunner()
        result = runner.invoke(cli, ["last"])
        assert result.exit_code != 0
        assert "corrupt" in result.output.lower()

    def test_resolve_last_refs_with_new_format(self, tmp_config):
        """_resolve_last_references works with new cache format."""

        p = make_product(asin="NF1")
        cache_obj = {"title": "Test", "results": [_serialize_product(p)]}
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))
        results = _resolve_last_references((1,))
        asin, desc = results[0]
        assert asin == "NF1"
        assert "NF1" in desc
        assert "Test" in desc


class TestLastFilters:
    def _seed_cache(self, tmp_config, products):

        data = [_serialize_product(p) for p in products]
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(data))

    def test_last_narrator_filter(self, tmp_config):
        """deals last --narrator filters by narrator substring match."""
        products = [
            make_product(
                asin="LN1",
                price=3.0,
                narrators=["R.C. Bray"],
                series_name="",
                series_position="",
            ),
            make_product(
                asin="LN2",
                price=4.0,
                narrators=["Scott Brick"],
                series_name="",
                series_position="",
            ),
        ]
        self._seed_cache(tmp_config, products)
        out_file = tmp_config / "last_narrator.json"
        runner = CliRunner()
        result = runner.invoke(
            cli, ["last", "--narrator", "bray", "--output", str(out_file)]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LN1" in asins
        assert "LN2" not in asins

    def test_last_min_ratings_filter(self, tmp_config):
        """deals last --min-ratings filters by number of ratings."""
        products = [
            make_product(
                asin="LR1",
                price=3.0,
                num_ratings=500,
                series_name="",
                series_position="",
            ),
            make_product(
                asin="LR2",
                price=4.0,
                num_ratings=50,
                series_name="",
                series_position="",
            ),
        ]
        self._seed_cache(tmp_config, products)
        out_file = tmp_config / "last_ratings.json"
        runner = CliRunner()
        result = runner.invoke(
            cli, ["last", "--min-ratings", "100", "--output", str(out_file)]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LR1" in asins
        assert "LR2" not in asins

    def test_last_language_filter(self, tmp_config):
        """deals last --language filters by language."""
        products = [
            make_product(
                asin="LL1",
                price=3.0,
                language="english",
                series_name="",
                series_position="",
            ),
            make_product(
                asin="LL2",
                price=4.0,
                language="french",
                series_name="",
                series_position="",
            ),
        ]
        self._seed_cache(tmp_config, products)
        out_file = tmp_config / "last_lang.json"
        runner = CliRunner()
        result = runner.invoke(
            cli, ["last", "--language", "english", "--output", str(out_file)]
        )
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


class TestLastClearFlag:
    def test_clear_existing_cache(self, tmp_config):

        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps([]))
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--clear"])
        assert result.exit_code == 0
        assert "cleared" in result.output.lower()
        assert not constants_mod.LAST_RESULTS_FILE.exists()

    def test_clear_no_cache(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--clear"])
        assert result.exit_code == 0
        assert "No cached results" in result.output

    def test_clear_exits_without_display(self, tmp_config):
        """--clear should not attempt to read or display any results."""

        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps([]))
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--clear"])
        assert result.exit_code == 0
        # Should show clear confirmation, not a product table
        assert "cleared" in result.output.lower()
        assert "deals found" not in result.output


class TestLastRefDescription:
    def test_detail_last_shows_description(self, mock_client, tmp_config):
        """detail --last N prints a dim description of the resolved result."""

        p = make_product(asin="DESC1", title="The Martian")
        cache_obj = {"title": "Search: Andy Weir", "results": [_serialize_product(p)]}
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))
        mock_client.get_product.return_value = make_product(
            asin="DESC1", title="The Martian"
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["detail", "--last", "1"])
        assert result.exit_code == 0, result.output
        assert "Result #1" in result.output
        assert "The Martian" in result.output
        assert "DESC1" in result.output

    def test_open_last_shows_description(self, mock_client, tmp_config):
        """open --last N prints a dim description of the resolved result."""

        p = make_product(asin="OPEN1", title="Some Book")
        cache_obj = {"title": "Search: test", "results": [_serialize_product(p)]}
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))
        runner = CliRunner()
        result = runner.invoke(cli, ["open", "--last", "1"])
        assert result.exit_code == 0, result.output
        assert "Result #1" in result.output
        assert "OPEN1" in result.output

    def test_compare_last_shows_description(self, mock_client, tmp_config):
        """compare --last N prints a dim description for each resolved result."""

        products = [
            make_product(asin="CMP1", title="Book Alpha"),
            make_product(asin="CMP2", title="Book Beta"),
        ]
        cache_obj = {
            "title": "Search: test",
            "results": [_serialize_product(p) for p in products],
        }
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))
        mock_client.get_products_batch.return_value = [
            make_product(
                asin="CMP1", title="Book Alpha", price=5.0, length_minutes=600
            ),
            make_product(asin="CMP2", title="Book Beta", price=8.0, length_minutes=600),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["compare", "--last", "1", "--last", "2"])
        assert result.exit_code == 0, result.output
        assert "Result #1" in result.output
        assert "Result #2" in result.output

    def test_wishlist_add_last_shows_description(self, mock_client, tmp_config):
        """wishlist add --last N prints a dim description of the resolved result."""

        p = make_product(asin="WADD1", title="Wishlist Book")
        cache_obj = {"title": "Search: test", "results": [_serialize_product(p)]}
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))
        mock_client.get_product.return_value = make_product(
            asin="WADD1", title="Wishlist Book"
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "add", "--last", "1"])
        assert result.exit_code == 0, result.output
        assert "Result #1" in result.output
        assert "WADD1" in result.output


class TestHistoryLast:
    def _seed_cache(self, tmp_config, products):

        cache_obj = {
            "title": "Search: test",
            "results": [_serialize_product(p) for p in products],
        }
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))

    def test_history_last_resolves_from_cache(self, tmp_config):
        """history --last N resolves the ASIN from the last results cache."""
        from audible_deals.price_history import record_prices as _record_prices

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


class TestLoadSeenAsins:
    def test_loads_from_seen_file(self, tmp_config):

        constants_mod.SEEN_ASINS_FILE.write_text(json.dumps(["A1", "A2"]))
        seen = _load_seen_asins()
        assert seen == {"A1", "A2"}

    def test_empty_when_no_file(self, tmp_config):
        seen = _load_seen_asins()
        assert seen == set()

    def test_returns_set_from_list(self, tmp_config):

        constants_mod.SEEN_ASINS_FILE.write_text(json.dumps(["B1", "B2", "B1"]))
        seen = _load_seen_asins()
        assert seen == {"B1", "B2"}

    def test_empty_on_corrupt_file(self, tmp_config):

        constants_mod.SEEN_ASINS_FILE.write_text("not valid json")
        seen = _load_seen_asins()
        assert seen == set()


class TestCumulativeSeenAsins:
    def test_save_and_load(self, tmp_config):
        _save_seen_asins({"A1", "A2"})
        assert _load_seen_asins() == {"A1", "A2"}

    def test_cumulative_append(self, tmp_config):
        _save_seen_asins({"A1", "A2"})
        _save_seen_asins({"A3", "A4"})
        assert _load_seen_asins() == {"A1", "A2", "A3", "A4"}

    def test_no_duplicates(self, tmp_config):

        _save_seen_asins({"A1", "A2"})
        _save_seen_asins({"A2", "A3"})
        seen = _load_seen_asins()
        assert seen == {"A1", "A2", "A3"}
        # Verify file is a clean sorted list
        data = json.loads(constants_mod.SEEN_ASINS_FILE.read_text())
        assert data == sorted(data)

    def test_empty_when_no_file(self, tmp_config):
        assert _load_seen_asins() == set()

    def test_clear_dismissed_does_not_change_seen(self, tmp_config, mock_client):
        _save_seen_asins({"SEEN1"})
        save_dismissed_asins({"DISMISSED1"})

        result = CliRunner().invoke(cli, ["last", "--clear-dismissed"])

        assert result.exit_code == 0, result.output
        assert load_dismissed_asins() == set()
        assert _load_seen_asins() == {"SEEN1"}

    def test_clear_dismissed_composes_with_seen_and_cache(
        self, tmp_config, mock_client
    ):
        _save_seen_asins({"SEEN1"})
        save_dismissed_asins({"DISMISSED1"})
        _routes_seed_last_results(tmp_config, [make_product(asin="CACHE1")])

        result = CliRunner().invoke(
            cli, ["last", "--clear-dismissed", "--clear-seen", "--clear"]
        )

        assert result.exit_code == 0, result.output
        assert load_dismissed_asins() == set()
        assert _load_seen_asins() == set()
        assert not constants_mod.LAST_RESULTS_FILE.exists()

    def test_clear_seen_command(self, tmp_config, mock_client):
        _save_seen_asins({"A1", "A2"})
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--clear-seen"])
        assert result.exit_code == 0
        assert "cleared" in result.output.lower()
        assert _load_seen_asins() == set()


class TestLastCount:
    def _write_cache(self, tmp_config, products):
        """Write a mock last results cache."""

        data = [_serialize_product(p) for p in products]
        payload = json.dumps({"title": "Test Results", "results": data})
        constants_mod.LAST_RESULTS_FILE.write_text(payload)

    def test_last_count_outputs_integer(self, tmp_config):
        """deals last --count prints the number of cached results."""
        products = [
            make_product(asin=f"LC{i:02d}", price=float(i)) for i in range(1, 8)
        ]
        self._write_cache(tmp_config, products)
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--count"])
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "7"

    def test_last_count_zero_when_empty_cache(self, tmp_config):
        """deals last --count returns 0 for an empty result cache."""

        constants_mod.LAST_RESULTS_FILE.write_text(
            json.dumps({"title": "Empty", "results": []})
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--count"])
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "0"


class TestSingleRefLastValidation:
    def _seed_cache(self, tmp_config):
        data = [
            {"asin": "B00TESTAA01", "title": "Book 1"},
            {"asin": "B00TESTAA02", "title": "Book 2"},
            {"asin": "B00TESTAA03", "title": "Book 3"},
        ]
        cache = {"title": "Test", "results": data}
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache))

    def test_detail_rejects_range(self, mock_client, tmp_config):
        self._seed_cache(tmp_config)
        runner = CliRunner()
        result = runner.invoke(cli, ["detail", "--last", "1-3"])
        assert result.exit_code != 0
        assert (
            "single position" in result.output.lower()
            or "single position" in str(result.exception).lower()
        )

    def test_history_rejects_comma_list(self, tmp_config):
        self._seed_cache(tmp_config)
        runner = CliRunner()
        result = runner.invoke(cli, ["history", "--last", "1,2"])
        assert result.exit_code != 0
        assert (
            "single position" in result.output.lower()
            or "single position" in str(result.exception).lower()
        )


class TestHistBelowZero:
    def test_hist_below_zero_accepted_with_require_history(
        self, mock_client, tmp_config
    ):
        """--hist-below 0 is valid and must not trigger the UsageError."""
        mock_client.search_pages.return_value = iter([([], 1, 0)])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["find", "--require-history", "--hist-below", "0", "--pages", "1", "-q"],
        )
        assert result.exit_code == 0, result.output

    def test_hist_below_zero_search_accepted(self, mock_client, tmp_config):
        """--hist-below 0 on search with --require-history must not raise UsageError."""
        mock_client.search_pages.return_value = iter([([], 1, 0)])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "test",
                "--require-history",
                "--hist-below",
                "0",
                "--pages",
                "1",
                "-q",
            ],
        )
        assert result.exit_code == 0, result.output


class TestExpandRefStringLabel:
    def test_default_label_in_error(self):
        """Default label is '--last' in error messages."""
        from audible_deals.selectors import _expand_ref_string

        with pytest.raises(click.ClickException, match="--last"):
            _expand_ref_string("abc")

    def test_custom_label_in_error(self):
        """Custom label replaces '--last' in error messages."""
        from audible_deals.selectors import _expand_ref_string

        with pytest.raises(click.ClickException, match="selection"):
            _expand_ref_string("abc", label="selection")

    def test_custom_label_range_error(self):
        """Custom label appears in range-specific error messages."""
        from audible_deals.selectors import _expand_ref_string

        with pytest.raises(click.ClickException, match="selection"):
            _expand_ref_string("5-3", label="selection")

    def test_custom_label_empty_part_error(self):
        """Custom label appears in empty-part error messages."""
        from audible_deals.selectors import _expand_ref_string

        with pytest.raises(click.ClickException, match="selection"):
            _expand_ref_string(",1", label="selection")


class TestRoutesLastCommand:
    def test_last_basic(self, tmp_config, mock_client):
        products = [make_product(asin="LA01", title="Cached Book", price=4.99)]
        _routes_seed_last_results(tmp_config, products)
        result = _routes_run(CliRunner(), ["last"])
        assert result.exit_code == 0
        assert "Cached Book" in result.output

    def test_last_with_resort(self, tmp_config, mock_client):
        products = [
            make_product(asin="LA02", price=2.99),
            make_product(asin="LA03", price=1.99),
        ]
        _routes_seed_last_results(tmp_config, products)
        result = _routes_run(CliRunner(), ["last", "--sort", "price"])
        assert result.exit_code == 0

    def test_last_with_filters(self, tmp_config, mock_client):
        products = [
            make_product(asin="LA04", price=2.99, rating=4.5),
            make_product(asin="LA05", price=12.99, rating=3.0),
        ]
        _routes_seed_last_results(tmp_config, products)
        result = _routes_run(
            CliRunner(), ["last", "--max-price", "5", "--min-rating", "4.0"]
        )
        assert result.exit_code == 0

    def test_last_json(self, tmp_config, mock_client):
        products = [make_product(asin="LA06", price=4.99)]
        _routes_seed_last_results(tmp_config, products)
        result = _routes_run(CliRunner(), ["last", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) > 0

    def test_last_count(self, tmp_config, mock_client):
        products = [make_product(asin="LA07"), make_product(asin="LA08")]
        _routes_seed_last_results(tmp_config, products)
        result = _routes_run(CliRunner(), ["last", "--count"])
        assert result.exit_code == 0
        assert "2" in result.output

    def test_last_clear(self, tmp_config, mock_client):
        products = [make_product(asin="LA09")]
        _routes_seed_last_results(tmp_config, products)
        result = _routes_run(CliRunner(), ["last", "--clear"])
        assert result.exit_code == 0
        assert not (tmp_config / "last_results.json").exists()

    def test_last_clear_seen(self, tmp_config, mock_client):
        (tmp_config / "seen_asins.json").write_text(json.dumps(["A1", "A2"]))
        result = _routes_run(CliRunner(), ["last", "--clear-seen"])
        assert result.exit_code == 0

    def test_last_no_cache_error(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["last"])
        assert result.exit_code != 0

    def test_last_export(self, tmp_config, mock_client):
        products = [make_product(asin="LA10", price=3.99)]
        _routes_seed_last_results(tmp_config, products)
        out_path = tmp_config / "last_out.json"
        result = _routes_run(CliRunner(), ["last", "-o", str(out_path)])
        assert result.exit_code == 0
        assert out_path.exists()


class TestRoutesHistoryCommand:
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
        result = _routes_run(CliRunner(), ["history", "H001"])
        assert result.exit_code == 0
        assert "9.99" in result.output
        assert "4.99" in result.output

    def test_history_no_data(self, tmp_config, mock_client):
        result = _routes_run(CliRunner(), ["history", "NODATA01"])
        assert result.exit_code == 0
        assert "No price history" in result.output

    def test_history_with_last_ref(self, tmp_config, mock_client):
        products = [make_product(asin="H002")]
        _routes_seed_last_results(tmp_config, products)
        result = _routes_run(CliRunner(), ["history", "--last", "1"])
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
        result = _routes_run(CliRunner(), ["history", "H003", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]["price"] == 9.99

    def test_history_json_empty_emits_empty_list(self, tmp_config, mock_client):
        result = _routes_run(CliRunner(), ["history", "NODATA02", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output) == []

    def test_history_all_json_emits_all(self, tmp_config, mock_client):
        hist_dir = tmp_config / "history"
        hist_dir.mkdir()
        e1 = [{"date": "2024-01-01", "price": 1.0, "title": "A"}]
        e2 = [{"date": "2024-01-02", "price": 2.0, "title": "B"}]
        (hist_dir / "H004.json").write_text(json.dumps({"marketplaces": {"us": e1}}))
        (hist_dir / "H005.json").write_text(json.dumps({"marketplaces": {"us": e2}}))
        result = _routes_run(CliRunner(), ["history", "--all", "--json"])
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
        result = _routes_run(
            CliRunner(), ["history", "--purge-older-than", "90", "--dry-run"]
        )
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
        result = _routes_run(
            CliRunner(), ["history", "--purge-older-than", "90", "--yes"]
        )
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

        result = _routes_run(
            CliRunner(), ["history", "--purge-older-than", "90"], input="y\n"
        )

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
        result = _routes_run(
            CliRunner(), ["history", "--purge-older-than", "90", "--dry-run"]
        )
        assert result.exit_code == 0
        assert "No history" in result.output


class TestResolveLastReferenceMissingAsin:
    def test_old_format_entry_without_asin_raises_clickexception(self, tmp_config):
        """A cache entry lacking 'asin' must surface as a clean ClickException."""
        data = [{"title": "NoAsinItem"}]
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(data))
        with pytest.raises(click.ClickException, match="no ASIN"):
            resolve_last_references((1,))

    def test_new_format_entry_without_asin_raises_clickexception(self, tmp_config):
        """New-format results entry with no 'asin' must also raise ClickException."""
        cache_obj = {"title": "Last results", "results": [{"title": "NoAsinItem"}]}
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))
        with pytest.raises(click.ClickException, match="no ASIN"):
            resolve_last_references((1,))
