"""Tests for audible_deals.state — persistence and I/O functions."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from audible_deals.cli import cli
from audible_deals.state import load_seen_asins, save_seen_asins, find_wishlist_atl_hits
from audible_deals.state import _expand_ref_string, resolve_last_references


# ===================================================================
# load_seen_asins / save_seen_asins
# ===================================================================


class TestLoadSeenAsins:
    def test_loads_from_seen_file(self, tmp_config):
        import audible_deals.state as state_mod

        state_mod.SEEN_ASINS_FILE.write_text(json.dumps(["A1", "A2"]))
        seen = load_seen_asins()
        assert seen == {"A1", "A2"}

    def test_empty_when_no_file(self, tmp_config):
        seen = load_seen_asins()
        assert seen == set()

    def test_returns_set_from_list(self, tmp_config):
        import audible_deals.state as state_mod

        state_mod.SEEN_ASINS_FILE.write_text(json.dumps(["B1", "B2", "B1"]))
        seen = load_seen_asins()
        assert seen == {"B1", "B2"}

    def test_empty_on_corrupt_file(self, tmp_config):
        import audible_deals.state as state_mod

        state_mod.SEEN_ASINS_FILE.write_text("not valid json")
        seen = load_seen_asins()
        assert seen == set()


class TestCumulativeSeenAsins:
    def test_save_and_load(self, tmp_config):
        save_seen_asins({"A1", "A2"})
        assert load_seen_asins() == {"A1", "A2"}

    def test_cumulative_append(self, tmp_config):
        save_seen_asins({"A1", "A2"})
        save_seen_asins({"A3", "A4"})
        assert load_seen_asins() == {"A1", "A2", "A3", "A4"}

    def test_no_duplicates(self, tmp_config):
        import audible_deals.state as state_mod

        save_seen_asins({"A1", "A2"})
        save_seen_asins({"A2", "A3"})
        seen = load_seen_asins()
        assert seen == {"A1", "A2", "A3"}
        data = json.loads(state_mod.SEEN_ASINS_FILE.read_text())
        assert data == sorted(data)

    def test_empty_when_no_file(self, tmp_config):
        assert load_seen_asins() == set()

    def test_clear_seen_command(self, tmp_config, mock_client):
        save_seen_asins({"A1", "A2"})
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--clear-seen"])
        assert result.exit_code == 0
        assert "cleared" in result.output.lower()
        assert load_seen_asins() == set()


# ===================================================================
# resolve_last_references — range syntax
# ===================================================================


class TestExpandRefString:
    def test_single_int(self):
        assert _expand_ref_string(1) == [1]

    def test_single_str_int(self):
        assert _expand_ref_string("3") == [3]

    def test_range(self):
        assert _expand_ref_string("1-3") == [1, 2, 3]

    def test_comma_list(self):
        assert _expand_ref_string("1,3,5") == [1, 3, 5]

    def test_mixed_range_and_comma(self):
        assert _expand_ref_string("1-3,7,9") == [1, 2, 3, 7, 9]

    def test_empty_part_raises(self):
        import click

        with pytest.raises(click.ClickException):
            _expand_ref_string(",1")

    def test_non_numeric_raises(self):
        import click

        with pytest.raises(click.ClickException):
            _expand_ref_string("abc")

    def test_start_gt_end_raises(self):
        import click

        with pytest.raises(click.ClickException):
            _expand_ref_string("5-3")

    def test_huge_range_raises(self):
        import click

        with pytest.raises(click.ClickException, match="width"):
            _expand_ref_string("1-99999999")


class TestResolveLastReferencesRangeSyntax:
    def _write_cache(self, tmp_config):
        import audible_deals.state as state_mod

        data = [{"asin": f"B00TESTA{i:02d}", "title": f"Book {i}"} for i in range(1, 8)]
        cache = {"title": "Test results", "results": data}
        state_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache))

    def test_range_string(self, tmp_config):
        self._write_cache(tmp_config)
        results = resolve_last_references(("1-3",))
        assert len(results) == 3
        assert results[0][0] == "B00TESTA01"
        assert results[2][0] == "B00TESTA03"

    def test_comma_list(self, tmp_config):
        self._write_cache(tmp_config)
        results = resolve_last_references(("1,3,5",))
        assert len(results) == 3
        assert [r[0] for r in results] == ["B00TESTA01", "B00TESTA03", "B00TESTA05"]

    def test_int_still_works(self, tmp_config):
        self._write_cache(tmp_config)
        results = resolve_last_references((2,))
        assert results[0][0] == "B00TESTA02"

    def test_out_of_range_raises(self, tmp_config):
        import click

        self._write_cache(tmp_config)
        with pytest.raises(click.ClickException, match="out of range"):
            resolve_last_references(("1-3,50",))


# ===================================================================
# find_wishlist_atl_hits
# ===================================================================


class TestFindWishlistAtlHits:
    def _write_wishlist(self, tmp_config, items):
        import audible_deals.state as state_mod

        state_mod.WISHLIST_FILE.write_text(json.dumps(items))

    def _write_history(self, tmp_config, asin, prices):
        import audible_deals.state as state_mod

        hist_dir = state_mod.HISTORY_DIR
        hist_dir.mkdir(parents=True, exist_ok=True)
        entries = [
            {"date": f"2024-01-{i + 1:02d}", "price": p, "title": f"Book {asin}"}
            for i, p in enumerate(prices)
        ]
        (hist_dir / f"{asin}.json").write_text(json.dumps(entries))

    def test_returns_atl_hit(self, tmp_config):
        self._write_wishlist(
            tmp_config, [{"asin": "B00ATL0001", "title": "ATL Book", "max_price": 10.0}]
        )
        self._write_history(tmp_config, "B00ATL0001", [9.99, 8.99, 7.99, 5.99])
        hits = find_wishlist_atl_hits()
        assert len(hits) == 1
        assert hits[0]["asin"] == "B00ATL0001"
        assert hits[0]["price"] == pytest.approx(5.99)

    def test_not_atl_returns_empty(self, tmp_config):
        self._write_wishlist(
            tmp_config,
            [{"asin": "B00ATL0002", "title": "Other Book", "max_price": 10.0}],
        )
        self._write_history(tmp_config, "B00ATL0002", [5.99, 8.99, 9.99])
        hits = find_wishlist_atl_hits()
        assert hits == []

    def test_requires_two_history_entries(self, tmp_config):
        self._write_wishlist(
            tmp_config,
            [{"asin": "B00ATL0003", "title": "Short Book", "max_price": 5.0}],
        )
        self._write_history(tmp_config, "B00ATL0003", [5.0])
        hits = find_wishlist_atl_hits()
        assert hits == []

    def test_empty_wishlist(self, tmp_config):
        hits = find_wishlist_atl_hits()
        assert hits == []

    def test_one_numeric_price_returns_empty(self, tmp_config):
        import audible_deals.state as state_mod

        self._write_wishlist(
            tmp_config, [{"asin": "B00ATL0004", "title": "T", "max_price": 5.0}]
        )
        state_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        entries = [
            {"date": "2024-01-01", "price": None, "title": "T"},
            {"date": "2024-01-02", "price": 3.99, "title": "T"},
        ]
        (state_mod.HISTORY_DIR / "B00ATL0004.json").write_text(json.dumps(entries))
        assert find_wishlist_atl_hits() == []

    def test_latest_non_numeric_returns_empty(self, tmp_config):
        import audible_deals.state as state_mod

        self._write_wishlist(
            tmp_config, [{"asin": "B00ATL0005", "title": "T", "max_price": 5.0}]
        )
        state_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        entries = [
            {"date": "2024-01-01", "price": 4.99, "title": "T"},
            {"date": "2024-01-02", "price": 3.99, "title": "T"},
            {"date": "2024-01-03", "price": None, "title": "T"},
        ]
        (state_mod.HISTORY_DIR / "B00ATL0005.json").write_text(json.dumps(entries))
        assert find_wishlist_atl_hits() == []
