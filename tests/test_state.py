"""Tests for the persistence and I/O store modules."""

from __future__ import annotations

import datetime
import json

import pytest
from click.testing import CliRunner

from audible_deals.cli import cli
from audible_deals.price_history import (
    find_all_atl_hits,
    find_wishlist_atl_hits,
    hist_percentiles,
    load_all_price_histories,
    price_drop_pcts,
    purge_stale_history,
)
from audible_deals.results_cache import (
    _expand_ref_string,
    load_seen_asins,
    resolve_last_references,
    save_seen_asins,
)
from tests.conftest import make_product


# ===================================================================
# load_seen_asins / save_seen_asins
# ===================================================================


class TestLoadSeenAsins:
    def test_loads_from_seen_file(self, tmp_config):
        import audible_deals.constants as constants_mod

        constants_mod.SEEN_ASINS_FILE.write_text(json.dumps(["A1", "A2"]))
        seen = load_seen_asins()
        assert seen == {"A1", "A2"}

    def test_empty_when_no_file(self, tmp_config):
        seen = load_seen_asins()
        assert seen == set()

    def test_returns_set_from_list(self, tmp_config):
        import audible_deals.constants as constants_mod

        constants_mod.SEEN_ASINS_FILE.write_text(json.dumps(["B1", "B2", "B1"]))
        seen = load_seen_asins()
        assert seen == {"B1", "B2"}

    def test_empty_on_corrupt_file(self, tmp_config):
        import audible_deals.constants as constants_mod

        constants_mod.SEEN_ASINS_FILE.write_text("not valid json")
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
        import audible_deals.constants as constants_mod

        save_seen_asins({"A1", "A2"})
        save_seen_asins({"A2", "A3"})
        seen = load_seen_asins()
        assert seen == {"A1", "A2", "A3"}
        data = json.loads(constants_mod.SEEN_ASINS_FILE.read_text())
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
        import audible_deals.constants as constants_mod

        data = [{"asin": f"B00TESTA{i:02d}", "title": f"Book {i}"} for i in range(1, 8)]
        cache = {"title": "Test results", "results": data}
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache))

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
        import audible_deals.constants as constants_mod

        constants_mod.WISHLIST_FILE.write_text(json.dumps(items))

    def _write_history(self, tmp_config, asin, prices):
        import audible_deals.constants as constants_mod

        hist_dir = constants_mod.HISTORY_DIR
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
        import audible_deals.constants as constants_mod

        self._write_wishlist(
            tmp_config, [{"asin": "B00ATL0004", "title": "T", "max_price": 5.0}]
        )
        constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        entries = [
            {"date": "2024-01-01", "price": None, "title": "T"},
            {"date": "2024-01-02", "price": 3.99, "title": "T"},
        ]
        (constants_mod.HISTORY_DIR / "B00ATL0004.json").write_text(json.dumps(entries))
        assert find_wishlist_atl_hits() == []

    def test_latest_non_numeric_returns_empty(self, tmp_config):
        import audible_deals.constants as constants_mod

        self._write_wishlist(
            tmp_config, [{"asin": "B00ATL0005", "title": "T", "max_price": 5.0}]
        )
        constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        entries = [
            {"date": "2024-01-01", "price": 4.99, "title": "T"},
            {"date": "2024-01-02", "price": 3.99, "title": "T"},
            {"date": "2024-01-03", "price": None, "title": "T"},
        ]
        (constants_mod.HISTORY_DIR / "B00ATL0005.json").write_text(json.dumps(entries))
        assert find_wishlist_atl_hits() == []


# ===================================================================
# hist_percentiles
# ===================================================================


def _write_history(tmp_config, asin: str, prices: list[float]) -> None:
    import audible_deals.constants as constants_mod

    constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    entries = [
        {"date": f"2024-01-{i + 1:02d}", "price": p, "title": "T"}
        for i, p in enumerate(prices)
    ]
    (constants_mod.HISTORY_DIR / f"{asin}.json").write_text(json.dumps(entries))


class TestHistPercentiles:
    def test_current_at_median_is_40th(self, tmp_config):
        # prices [1,2,3,4,5], current=3; 2 of 5 < 3 => 40th percentile (strict less-than)
        _write_history(tmp_config, "HP01", [1.0, 2.0, 3.0, 4.0, 5.0])
        p = make_product(asin="HP01", price=3.0)
        result = hist_percentiles([p])
        assert result["HP01"] == 40

    def test_current_at_minimum_is_0th(self, tmp_config):
        # prices [1,2,3,4,5], current=1; 0 of 5 < 1 => 0 (all-time low => 0)
        _write_history(tmp_config, "HP02", [1.0, 2.0, 3.0, 4.0, 5.0])
        p = make_product(asin="HP02", price=1.0)
        result = hist_percentiles([p])
        assert result["HP02"] == 0

    def test_current_at_maximum_is_80th(self, tmp_config):
        # prices [1,2,3,4,5], current=5; 4 of 5 < 5 => 80th
        _write_history(tmp_config, "HP03", [1.0, 2.0, 3.0, 4.0, 5.0])
        p = make_product(asin="HP03", price=5.0)
        result = hist_percentiles([p])
        assert result["HP03"] == 80

    def test_all_time_low_ranks_zero(self, tmp_config):
        # An all-time-low price should rank 0 so --hist-below 0..19 can match it
        _write_history(tmp_config, "HP08", [10.0, 8.0, 6.0, 5.0, 4.0])
        p = make_product(asin="HP08", price=4.0)
        result = hist_percentiles([p])
        assert result["HP08"] == 0

    def test_fewer_than_5_entries_excluded(self, tmp_config):
        _write_history(tmp_config, "HP04", [1.0, 2.0, 3.0, 4.0])
        p = make_product(asin="HP04", price=1.0)
        result = hist_percentiles([p])
        assert "HP04" not in result

    def test_no_history_excluded(self, tmp_config):
        p = make_product(asin="HP05", price=5.0)
        result = hist_percentiles([p])
        assert "HP05" not in result

    def test_no_price_excluded(self, tmp_config):
        _write_history(tmp_config, "HP06", [1.0, 2.0, 3.0, 4.0, 5.0])
        p = make_product(asin="HP06", price=None)
        result = hist_percentiles([p])
        assert "HP06" not in result

    def test_non_numeric_history_entries_skipped(self, tmp_config):
        import audible_deals.constants as constants_mod

        constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        entries = [
            {"date": "2024-01-01", "price": None, "title": "T"},
            {"date": "2024-01-02", "price": 2.0, "title": "T"},
            {"date": "2024-01-03", "price": 3.0, "title": "T"},
            {"date": "2024-01-04", "price": 4.0, "title": "T"},
            {"date": "2024-01-05", "price": 5.0, "title": "T"},
        ]
        (constants_mod.HISTORY_DIR / "HP07.json").write_text(json.dumps(entries))
        # 4 numeric entries — fewer than 5, should be excluded
        p = make_product(asin="HP07", price=3.0)
        result = hist_percentiles([p])
        assert "HP07" not in result


# ===================================================================
# price_drop_pcts
# ===================================================================


class TestPriceDropPcts:
    def test_basic_drop(self, tmp_config):
        # last price was 10.0, current is 8.0 => 20% drop
        _write_history(tmp_config, "PD01", [12.0, 10.0])
        p = make_product(asin="PD01", price=8.0)
        result = price_drop_pcts([p])
        assert result["PD01"] == pytest.approx(20.0)

    def test_no_history_excluded(self, tmp_config):
        p = make_product(asin="PD02", price=5.0)
        result = price_drop_pcts([p])
        assert "PD02" not in result

    def test_price_increase_is_negative(self, tmp_config):
        # last price was 5.0, current is 8.0 => -60% "drop" (price went up)
        _write_history(tmp_config, "PD03", [5.0])
        p = make_product(asin="PD03", price=8.0)
        result = price_drop_pcts([p])
        assert result["PD03"] == pytest.approx(-60.0)

    def test_same_price_is_zero(self, tmp_config):
        _write_history(tmp_config, "PD04", [10.0])
        p = make_product(asin="PD04", price=10.0)
        result = price_drop_pcts([p])
        assert result["PD04"] == pytest.approx(0.0)

    def test_no_price_excluded(self, tmp_config):
        _write_history(tmp_config, "PD05", [10.0])
        p = make_product(asin="PD05", price=None)
        result = price_drop_pcts([p])
        assert "PD05" not in result

    def test_uses_last_non_today_entry(self, tmp_config):
        # multiple history entries — uses the last non-today one
        _write_history(tmp_config, "PD06", [20.0, 15.0, 10.0])
        p = make_product(asin="PD06", price=8.0)
        result = price_drop_pcts([p])
        # drop from 10.0 to 8.0 = 20%
        assert result["PD06"] == pytest.approx(20.0)

    def test_zero_last_price_excluded(self, tmp_config):
        _write_history(tmp_config, "PD07", [0.0])
        p = make_product(asin="PD07", price=5.0)
        result = price_drop_pcts([p])
        assert "PD07" not in result

    def test_same_day_rerun_skips_today_entry(self, tmp_config):
        """Same-day re-run: today's entry is skipped; reference is yesterday's price."""
        import audible_deals.constants as constants_mod

        today = datetime.date.today().isoformat()
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        entries = [
            {"date": yesterday, "price": 10.0, "title": "T"},
            {"date": today, "price": 8.0, "title": "T"},
        ]
        (constants_mod.HISTORY_DIR / "PD08.json").write_text(json.dumps(entries))
        p = make_product(asin="PD08", price=8.0)
        result = price_drop_pcts([p])
        # reference is yesterday's 10.0; drop from 10.0 to 8.0 = 20%
        assert result["PD08"] == pytest.approx(20.0)

    def test_all_entries_today_excluded(self, tmp_config):
        """When all history entries are from today, ASIN is omitted (no reference price)."""
        import audible_deals.constants as constants_mod

        today = datetime.date.today().isoformat()
        constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        entries = [{"date": today, "price": 10.0, "title": "T"}]
        (constants_mod.HISTORY_DIR / "PD09.json").write_text(json.dumps(entries))
        p = make_product(asin="PD09", price=8.0)
        result = price_drop_pcts([p])
        assert "PD09" not in result


# ===================================================================
# load_all_price_histories
# ===================================================================


class TestLoadAllPriceHistories:
    def test_returns_empty_when_dir_missing(self, tmp_config):
        result = load_all_price_histories()
        assert result == {}

    def test_returns_all_valid_histories(self, tmp_config):
        _write_history(tmp_config, "LA01", [1.0, 2.0])
        _write_history(tmp_config, "LA02", [3.0])
        result = load_all_price_histories()
        assert set(result.keys()) == {"LA01", "LA02"}
        assert len(result["LA01"]) == 2
        assert len(result["LA02"]) == 1

    def test_skips_corrupt_files(self, tmp_config):
        import audible_deals.constants as constants_mod

        constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        (constants_mod.HISTORY_DIR / "CORRUPT.json").write_text("not json")
        _write_history(tmp_config, "GOOD01", [5.0])
        result = load_all_price_histories()
        assert "CORRUPT" not in result
        assert "GOOD01" in result

    def test_skips_empty_history(self, tmp_config):
        import audible_deals.constants as constants_mod

        constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        (constants_mod.HISTORY_DIR / "EMPTY01.json").write_text("[]")
        _write_history(tmp_config, "VALID01", [4.0])
        result = load_all_price_histories()
        assert "EMPTY01" not in result
        assert "VALID01" in result


# ===================================================================
# purge_stale_history
# ===================================================================


def _write_history_with_dates(
    tmp_config, asin: str, dates: list[str], price: float = 5.0
) -> None:
    import audible_deals.constants as constants_mod

    constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    entries = [{"date": d, "price": price, "title": "T"} for d in dates]
    (constants_mod.HISTORY_DIR / f"{asin}.json").write_text(json.dumps(entries))


class TestPurgeStaleHistory:
    def test_returns_zero_when_dir_missing(self, tmp_config):
        count, affected = purge_stale_history(90)
        assert count == 0
        assert affected == []

    def test_stale_file_is_deleted(self, tmp_config):
        import audible_deals.constants as constants_mod

        old_date = (datetime.date.today() - datetime.timedelta(days=200)).isoformat()
        _write_history_with_dates(tmp_config, "STALE01", [old_date])
        count, affected = purge_stale_history(90)
        assert count == 1
        assert "STALE01" in affected
        assert not (constants_mod.HISTORY_DIR / "STALE01.json").exists()

    def test_fresh_file_is_kept(self, tmp_config):
        import audible_deals.constants as constants_mod

        fresh_date = datetime.date.today().isoformat()
        _write_history_with_dates(tmp_config, "FRESH01", [fresh_date])
        count, affected = purge_stale_history(90)
        assert count == 0
        assert "FRESH01" not in affected
        assert (constants_mod.HISTORY_DIR / "FRESH01.json").exists()

    def test_mixed_stale_and_fresh(self, tmp_config):
        import audible_deals.constants as constants_mod

        old_date = (datetime.date.today() - datetime.timedelta(days=100)).isoformat()
        fresh_date = datetime.date.today().isoformat()
        _write_history_with_dates(tmp_config, "OLD01", [old_date])
        _write_history_with_dates(tmp_config, "NEW01", [fresh_date])
        count, affected = purge_stale_history(90)
        assert count == 1
        assert "OLD01" in affected
        assert not (constants_mod.HISTORY_DIR / "OLD01.json").exists()
        assert (constants_mod.HISTORY_DIR / "NEW01.json").exists()

    def test_dry_run_deletes_nothing(self, tmp_config):
        import audible_deals.constants as constants_mod

        old_date = (datetime.date.today() - datetime.timedelta(days=200)).isoformat()
        _write_history_with_dates(tmp_config, "DRY01", [old_date])
        count, affected = purge_stale_history(90, dry_run=True)
        assert count == 1
        assert "DRY01" in affected
        assert (constants_mod.HISTORY_DIR / "DRY01.json").exists()

    def test_corrupt_file_is_skipped(self, tmp_config):
        import audible_deals.constants as constants_mod

        constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        (constants_mod.HISTORY_DIR / "CORRUPT2.json").write_text("not json")
        count, affected = purge_stale_history(90)
        assert "CORRUPT2" not in affected
        assert (constants_mod.HISTORY_DIR / "CORRUPT2.json").exists()

    def test_file_with_no_date_is_skipped(self, tmp_config):
        import audible_deals.constants as constants_mod

        constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        entries = [{"price": 5.0, "title": "T"}]
        (constants_mod.HISTORY_DIR / "NODATE01.json").write_text(json.dumps(entries))
        count, affected = purge_stale_history(90)
        assert "NODATE01" not in affected
        assert (constants_mod.HISTORY_DIR / "NODATE01.json").exists()

    def test_boundary_date_is_kept(self, tmp_config):
        # A file whose last entry is exactly `days` ago (not older) is kept
        import audible_deals.constants as constants_mod

        cutoff_date = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
        _write_history_with_dates(tmp_config, "BOUND01", [cutoff_date])
        count, affected = purge_stale_history(90)
        assert count == 0
        assert (constants_mod.HISTORY_DIR / "BOUND01.json").exists()


# ===================================================================
# find_all_atl_hits
# ===================================================================


class TestFindAllAtlHits:
    def _write_history(self, tmp_config, asin, prices):
        import audible_deals.constants as constants_mod

        hist_dir = constants_mod.HISTORY_DIR
        hist_dir.mkdir(parents=True, exist_ok=True)
        entries = [
            {"date": f"2024-01-{i + 1:02d}", "price": p, "title": f"Book {asin}"}
            for i, p in enumerate(prices)
        ]
        (hist_dir / f"{asin}.json").write_text(json.dumps(entries))

    def _write_wishlist(self, tmp_config, items):
        import audible_deals.constants as constants_mod

        constants_mod.WISHLIST_FILE.write_text(json.dumps(items))

    def test_atl_non_wishlist_asin_found(self, tmp_config):
        self._write_history(tmp_config, "B00ALL0001", [9.99, 8.99, 6.99])
        hits = find_all_atl_hits()
        assert len(hits) == 1
        assert hits[0]["asin"] == "B00ALL0001"
        assert hits[0]["price"] == pytest.approx(6.99)
        assert hits[0]["target"] is None

    def test_non_atl_excluded(self, tmp_config):
        # Latest price is above the previous minimum — not ATL
        self._write_history(tmp_config, "B00ALL0002", [5.99, 8.99, 9.99])
        hits = find_all_atl_hits()
        assert hits == []

    def test_fewer_than_two_numeric_prices_excluded(self, tmp_config):
        import audible_deals.constants as constants_mod

        constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        entries = [{"date": "2024-01-01", "price": 5.99, "title": "T"}]
        (constants_mod.HISTORY_DIR / "B00ALL0003.json").write_text(json.dumps(entries))
        hits = find_all_atl_hits()
        assert hits == []

    def test_target_filled_from_wishlist(self, tmp_config):
        self._write_wishlist(
            tmp_config,
            [{"asin": "B00ALL0004", "title": "WL Book", "max_price": 7.0}],
        )
        self._write_history(tmp_config, "B00ALL0004", [9.99, 8.99, 6.99])
        hits = find_all_atl_hits()
        assert len(hits) == 1
        assert hits[0]["target"] == pytest.approx(7.0)

    def test_target_none_when_not_on_wishlist(self, tmp_config):
        self._write_history(tmp_config, "B00ALL0005", [9.99, 8.99, 6.99])
        hits = find_all_atl_hits()
        assert hits[0]["target"] is None

    def test_sorted_by_drop_magnitude_descending(self, tmp_config):
        # B00ALL0006: prev_min=8.99, latest=6.99, drop=2.00
        # B00ALL0007: prev_min=9.99, latest=5.99, drop=4.00 — bigger drop
        self._write_history(tmp_config, "B00ALL0006", [9.99, 8.99, 6.99])
        self._write_history(tmp_config, "B00ALL0007", [9.99, 5.99])
        hits = find_all_atl_hits()
        asins = [h["asin"] for h in hits]
        assert asins.index("B00ALL0007") < asins.index("B00ALL0006")

    def test_limit_respected(self, tmp_config):
        for i in range(5):
            asin = f"B00LIM{i:04d}"
            self._write_history(tmp_config, asin, [10.0, float(i + 1)])
        hits = find_all_atl_hits(limit=3)
        assert len(hits) == 3

    def test_latest_non_numeric_excluded(self, tmp_config):
        import audible_deals.constants as constants_mod

        constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        entries = [
            {"date": "2024-01-01", "price": 9.99, "title": "T"},
            {"date": "2024-01-02", "price": None, "title": "T"},
        ]
        (constants_mod.HISTORY_DIR / "B00ALL0008.json").write_text(json.dumps(entries))
        hits = find_all_atl_hits()
        assert hits == []

    def test_empty_history_dir_returns_empty(self, tmp_config):
        hits = find_all_atl_hits()
        assert hits == []
