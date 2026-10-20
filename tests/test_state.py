"""Persisted result and price-history state behavior."""

from __future__ import annotations

import datetime
import json
import logging
import sqlite3
import threading

import click
import pytest
from click.testing import CliRunner

import audible_deals.price_history as price_history
import audible_deals.results_cache as results_cache_mod
import audible_deals.selectors as selectors_mod
import audible_deals.wishlist as wishlist_mod
from audible_deals import db, paths
from audible_deals.cli import cli
from audible_deals.price_history import (
    find_all_atl_hits,
    find_wishlist_atl_hits,
    hist_percentiles,
    load_all_price_histories,
    load_price_history,
    price_drop_pcts,
    price_history_context,
    purge_stale_history,
    record_prices,
    scan_price_changes,
)
from audible_deals.results_cache import (
    clear_dismissed_asins,
    load_dismissed_asins,
    load_seen_asins,
    save_dismissed_asins,
    save_last_results,
    save_seen_asins,
)
from audible_deals.selectors import (
    _expand_ref_string,
    resolve_last_references,
    resolve_selectors,
)
from tests.conftest import make_product, seed_history, seed_prices


def _write_history(tmp_config, asin: str, prices: list[float]) -> None:
    seed_prices(asin, prices)


def _write_history_with_dates(
    tmp_config, asin: str, dates: list[str], price: float = 5.0
) -> None:
    seed_history(asin, [(d, price) for d in dates])


def _make_hist(asin: str, dated_prices: list[tuple[str, float | None]]) -> dict:
    return {
        asin: [
            {"date": d, "price": p, "title": f"Book {asin}"} for d, p in dated_prices
        ]
    }


class TestLoadSeenAsins:
    def test_loads_what_was_saved(self, tmp_config):
        save_seen_asins({"A1", "A2"})
        assert load_seen_asins() == {"A1", "A2"}

    def test_empty_when_nothing_saved(self, tmp_config):
        assert load_seen_asins() == set()

    def test_set_semantics(self, tmp_config):
        save_seen_asins({"B1", "B2"})
        save_seen_asins({"B1"})
        assert load_seen_asins() == {"B1", "B2"}


class TestCumulativeSeenAsins:
    def test_save_and_load(self, tmp_config):
        save_seen_asins({"A1", "A2"})
        assert load_seen_asins() == {"A1", "A2"}

    def test_cumulative_append(self, tmp_config):
        save_seen_asins({"A1", "A2"})
        save_seen_asins({"A3", "A4"})
        assert load_seen_asins() == {"A1", "A2", "A3", "A4"}

    def test_no_duplicates(self, tmp_config):
        save_seen_asins({"A1", "A2"})
        save_seen_asins({"A2", "A3"})
        assert load_seen_asins() == {"A1", "A2", "A3"}
        rows = db.state().execute("SELECT asin FROM seen ORDER BY asin").fetchall()
        assert [row[0] for row in rows] == ["A1", "A2", "A3"]

    def test_empty_when_no_file(self, tmp_config):
        assert load_seen_asins() == set()

    def test_failure_to_record_only_warns(self, tmp_config, monkeypatch, caplog):
        def broken(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(results_cache_mod.db, "transaction", broken)
        with caplog.at_level(logging.WARNING):
            save_seen_asins({"A1"})
        assert "failed to record seen ASINs" in caplog.text


class TestDismissedAsins:
    def test_dismissed_save_is_cumulative(self, tmp_config):
        save_dismissed_asins({"B2", "A1"})
        save_dismissed_asins({"C3", "B2"})

        assert load_dismissed_asins() == {"A1", "B2", "C3"}
        rows = db.state().execute("SELECT asin FROM dismissed ORDER BY asin")
        assert [row[0] for row in rows] == ["A1", "B2", "C3"]

    def test_dismissed_write_failure_propagates(self, tmp_config, monkeypatch):
        def broken(*args, **kwargs):
            raise OSError("read only")

        monkeypatch.setattr(results_cache_mod.db, "transaction", broken)
        with pytest.raises(OSError, match="read only"):
            save_dismissed_asins({"A1"})

    def test_clear_dismissed_removes_only_dismissed(self, tmp_config):
        save_dismissed_asins({"A1"})
        save_seen_asins({"S1"})

        assert clear_dismissed_asins()
        assert not clear_dismissed_asins()
        assert load_dismissed_asins() == set()
        assert load_seen_asins() == {"S1"}

    def test_clear_seen_command(self, tmp_config, mock_client):
        save_seen_asins({"A1", "A2"})
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--clear-seen"])
        assert result.exit_code == 0
        assert "cleared" in result.output.lower()
        assert load_seen_asins() == set()


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

        with pytest.raises(click.ClickException):
            _expand_ref_string(",1")

    def test_non_numeric_raises(self):

        with pytest.raises(click.ClickException):
            _expand_ref_string("abc")

    def test_start_gt_end_raises(self):

        with pytest.raises(click.ClickException):
            _expand_ref_string("5-3")

    def test_huge_range_raises(self):

        with pytest.raises(click.ClickException, match="width"):
            _expand_ref_string("1-99999999")


class TestResolveLastReferencesRangeSyntax:
    def _write_cache(self, tmp_config):
        data = [{"asin": f"B00TESTA{i:02d}", "title": f"Book {i}"} for i in range(1, 8)]
        save_last_results("Test results", data)

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

        self._write_cache(tmp_config)
        with pytest.raises(click.ClickException, match="out of range"):
            resolve_last_references(("1-3,50",))


class TestResolveSelectorsSessionReuse:
    @staticmethod
    def _write_cache(tmp_config):
        data = [
            {"asin": f"B00CACHE{i}", "title": f"Book {i}", "locale": "us"}
            for i in range(1, 4)
        ]
        save_last_results("Test results", data)

    def test_at_and_last_references_share_one_session_load(
        self, tmp_config, monkeypatch
    ):
        self._write_cache(tmp_config)
        real_load = selectors_mod.results_cache.load_result_session
        calls = 0

        def counted_load():
            nonlocal calls
            calls += 1
            return real_load()

        monkeypatch.setattr(
            selectors_mod.results_cache, "load_result_session", counted_load
        )

        resolved, locale = resolve_selectors(("@1", "@2"), last_refs=("3", "1"))

        assert calls == 1
        assert [item.asin for item in resolved] == [
            "B00CACHE1",
            "B00CACHE2",
            "B00CACHE3",
        ]
        assert [item.description for item in resolved] == [
            "Result #1 from 'Test results': Book 1 (B00CACHE1)",
            "Result #2 from 'Test results': Book 2 (B00CACHE2)",
            "Result #3 from 'Test results': Book 3 (B00CACHE3)",
        ]
        assert locale == "us"

    @pytest.mark.parametrize(
        ("selectors", "last_refs", "message"),
        [
            (("@1", "@9"), (), "selector 9 is out of range"),
            (("@1",), ("9",), "--last 9 is out of range"),
        ],
    )
    def test_reused_session_preserves_error_labels(
        self, tmp_config, monkeypatch, selectors, last_refs, message
    ):
        self._write_cache(tmp_config)
        real_load = selectors_mod.results_cache.load_result_session
        calls = 0

        def counted_load():
            nonlocal calls
            calls += 1
            return real_load()

        monkeypatch.setattr(
            selectors_mod.results_cache, "load_result_session", counted_load
        )

        with pytest.raises(click.ClickException, match=message):
            resolve_selectors(selectors, last_refs=last_refs)

        assert calls == 1


class TestFindWishlistAtlHits:
    def _write_wishlist(self, tmp_config, items):
        paths.wishlist_file().write_text(json.dumps(items))

    def _write_history(self, tmp_config, asin, prices):
        seed_prices(asin, prices, title=f"Book {asin}")

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

    def test_one_numeric_price_returns_empty(self):
        entries = [
            {"date": "2024-01-01", "price": None, "title": "T"},
            {"date": "2024-01-02", "price": 3.99, "title": "T"},
        ]
        assert price_history._atl_latest(entries) is None

    def test_latest_non_numeric_returns_empty(self):
        entries = [
            {"date": "2024-01-01", "price": 4.99, "title": "T"},
            {"date": "2024-01-02", "price": 3.99, "title": "T"},
            {"date": "2024-01-03", "price": None, "title": "T"},
        ]
        assert price_history._atl_latest(entries) is None


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

    def test_non_numeric_history_entries_skipped(self):
        histories = {
            "us:HP07": [
                {"date": "2024-01-01", "price": None, "title": "T"},
                {"date": "2024-01-02", "price": 2.0, "title": "T"},
                {"date": "2024-01-03", "price": 3.0, "title": "T"},
                {"date": "2024-01-04", "price": 4.0, "title": "T"},
                {"date": "2024-01-05", "price": 5.0, "title": "T"},
            ]
        }
        # 4 numeric entries — fewer than 5, should be excluded
        p = make_product(asin="HP07", price=3.0)
        result = hist_percentiles([p], histories)
        assert "HP07" not in result


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
        today = datetime.date.today().isoformat()
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        seed_history("PD08", [(yesterday, 10.0), (today, 8.0)])
        p = make_product(asin="PD08", price=8.0)
        result = price_drop_pcts([p])
        # reference is yesterday's 10.0; drop from 10.0 to 8.0 = 20%
        assert result["PD08"] == pytest.approx(20.0)

    def test_all_entries_today_excluded(self, tmp_config):
        """When all history entries are from today, ASIN is omitted (no reference price)."""
        today = datetime.date.today().isoformat()
        seed_history("PD09", [(today, 10.0)])
        p = make_product(asin="PD09", price=8.0)
        result = price_drop_pcts([p])
        assert "PD09" not in result


class TestLoadAllPriceHistories:
    def test_returns_empty_when_nothing_recorded(self, tmp_config):
        result = load_all_price_histories()
        assert result == {}

    def test_returns_all_histories(self, tmp_config):
        _write_history(tmp_config, "LA01", [1.0, 2.0])
        _write_history(tmp_config, "LA02", [3.0])
        result = load_all_price_histories()
        assert set(result.keys()) == {"LA01", "LA02"}
        assert len(result["LA01"]) == 2
        assert len(result["LA02"]) == 1

    def test_is_scoped_to_the_marketplace(self, tmp_config):
        seed_history("LA03", [("2024-01-01", 4.0)], locale="uk")
        _write_history(tmp_config, "LA04", [5.0])
        assert set(load_all_price_histories("us")) == {"LA04"}
        assert set(load_all_price_histories("uk")) == {"LA03"}

    def test_entries_are_oldest_first(self, tmp_config):
        seed_history(
            "LA05", [("2024-03-01", 3.0), ("2024-01-01", 1.0), ("2024-02-01", 2.0)]
        )
        assert [e["price"] for e in load_all_price_histories()["LA05"]] == [
            1.0,
            2.0,
            3.0,
        ]


class TestPurgeStaleHistory:
    def test_returns_zero_when_nothing_recorded(self, tmp_config):
        count, affected = purge_stale_history(90)
        assert count == 0
        assert affected == []

    def test_stale_history_is_deleted(self, tmp_config):
        old_date = (datetime.date.today() - datetime.timedelta(days=200)).isoformat()
        _write_history_with_dates(tmp_config, "STALE01", [old_date])
        count, affected = purge_stale_history(90)
        assert count == 1
        assert "STALE01" in affected
        assert load_price_history("STALE01") == []

    def test_purge_also_drops_the_product_index_row(self, tmp_config):
        old_date = (datetime.date.today() - datetime.timedelta(days=200)).isoformat()
        _write_history_with_dates(tmp_config, "STALE02", [old_date])
        purge_stale_history(90)
        remaining = (
            db.state()
            .execute("SELECT COUNT(*) FROM products WHERE asin = 'STALE02'")
            .fetchone()[0]
        )
        assert remaining == 0

    def test_fresh_history_is_kept(self, tmp_config):
        fresh_date = datetime.date.today().isoformat()
        _write_history_with_dates(tmp_config, "FRESH01", [fresh_date])
        count, affected = purge_stale_history(90)
        assert count == 0
        assert "FRESH01" not in affected
        assert load_price_history("FRESH01")

    def test_mixed_stale_and_fresh(self, tmp_config):
        old_date = (datetime.date.today() - datetime.timedelta(days=100)).isoformat()
        fresh_date = datetime.date.today().isoformat()
        _write_history_with_dates(tmp_config, "OLD01", [old_date])
        _write_history_with_dates(tmp_config, "NEW01", [fresh_date])
        count, affected = purge_stale_history(90)
        assert count == 1
        assert "OLD01" in affected
        assert load_price_history("OLD01") == []
        assert load_price_history("NEW01")

    def test_dry_run_deletes_nothing(self, tmp_config):
        old_date = (datetime.date.today() - datetime.timedelta(days=200)).isoformat()
        _write_history_with_dates(tmp_config, "DRY01", [old_date])
        count, affected = purge_stale_history(90, dry_run=True)
        assert count == 1
        assert "DRY01" in affected
        assert load_price_history("DRY01")

    def test_rechecks_staleness_when_deleting_the_chosen_asins(self, tmp_config):
        """The CLI previews, asks, then purges the previewed ASINs: a price
        recorded in between must save the history from deletion."""
        old_date = (datetime.date.today() - datetime.timedelta(days=200)).isoformat()
        _write_history_with_dates(tmp_config, "RACE01", [old_date])
        _, previewed = purge_stale_history(90, dry_run=True)
        assert previewed == ["RACE01"]

        record_prices(
            [make_product(asin="RACE01", locale="us", price=4.0, title="Fresh")]
        )
        count, affected = purge_stale_history(90, asins=previewed)

        assert count == 0
        assert affected == []
        assert (
            load_price_history("RACE01", "us")[-1]["date"]
            == datetime.date.today().isoformat()
        )

    def test_purge_only_removes_requested_marketplace(self, tmp_config):
        old_date = (datetime.date.today() - datetime.timedelta(days=200)).isoformat()
        fresh_date = datetime.date.today().isoformat()
        seed_history("MARKETS01", [(old_date, 5.0)], locale="us")
        seed_history("MARKETS01", [(fresh_date, 4.0)], locale="uk")

        count, affected = purge_stale_history(90, locale="us")

        assert count == 1
        assert affected == ["MARKETS01"]
        assert load_price_history("MARKETS01", "us") == []
        assert load_price_history("MARKETS01", "uk")[0]["price"] == 4.0

    def test_only_listed_asins_are_purged(self, tmp_config):
        old_date = (datetime.date.today() - datetime.timedelta(days=200)).isoformat()
        _write_history_with_dates(tmp_config, "ONLY01", [old_date])
        _write_history_with_dates(tmp_config, "ONLY02", [old_date])

        count, affected = purge_stale_history(90, asins=["ONLY01"])

        assert (count, affected) == (1, ["ONLY01"])
        assert load_price_history("ONLY02")

    def test_boundary_date_is_kept(self, tmp_config):
        # History whose last entry is exactly `days` ago (not older) is kept
        cutoff_date = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
        _write_history_with_dates(tmp_config, "BOUND01", [cutoff_date])
        count, affected = purge_stale_history(90)
        assert count == 0
        assert load_price_history("BOUND01")


class TestFindAllAtlHits:
    def _write_history(self, tmp_config, asin, prices):
        seed_prices(asin, prices, title=f"Book {asin}")

    def _write_wishlist(self, tmp_config, items):
        paths.wishlist_file().write_text(json.dumps(items))

    def test_atl_non_wishlist_asin_found(self, tmp_config):
        self._write_history(tmp_config, "B00ALL0001", [9.99, 8.99, 6.99])
        hits = find_all_atl_hits()
        assert len(hits) == 1
        assert hits[0]["asin"] == "B00ALL0001"
        assert hits[0]["price"] == pytest.approx(6.99)
        assert hits[0]["target"] is None
        assert hits[0]["title"] == "Book B00ALL0001"

    def test_non_atl_excluded(self, tmp_config):
        # Latest price is above the previous minimum — not ATL
        self._write_history(tmp_config, "B00ALL0002", [5.99, 8.99, 9.99])
        hits = find_all_atl_hits()
        assert hits == []

    def test_fewer_than_two_prices_excluded(self, tmp_config):
        seed_history("B00ALL0003", [("2024-01-01", 5.99)])
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

    def test_equal_drops_are_ordered_by_asin(self, tmp_config):
        for asin in ("B00TIE0003", "B00TIE0001", "B00TIE0002"):
            self._write_history(tmp_config, asin, [10.0, 5.0])
        assert [h["asin"] for h in find_all_atl_hits()] == [
            "B00TIE0001",
            "B00TIE0002",
            "B00TIE0003",
        ]

    def test_latest_above_previous_minimum_excluded_even_if_lower_than_first(
        self, tmp_config
    ):
        self._write_history(tmp_config, "B00ALL0008", [9.99, 3.0, 5.0])
        assert find_all_atl_hits() == []

    def test_is_scoped_to_the_marketplace(self, tmp_config):
        seed_history(
            "B00ALL0009", [("2024-01-01", 9.0), ("2024-01-02", 4.0)], locale="uk"
        )
        assert find_all_atl_hits() == []
        assert [h["asin"] for h in find_all_atl_hits(locale="uk")] == ["B00ALL0009"]

    def test_preloaded_histories_use_the_same_rule(self, tmp_config):
        histories = {
            "B00ALL0010": [
                {"date": "2024-01-01", "price": 9.99, "title": "T"},
                {"date": "2024-01-02", "price": None, "title": "T"},
            ],
            "B00ALL0011": [
                {"date": "2024-01-01", "price": 9.99, "title": "Pre"},
                {"date": "2024-01-02", "price": 4.0, "title": "Pre"},
            ],
        }
        hits = find_all_atl_hits(histories=histories)
        assert [h["asin"] for h in hits] == ["B00ALL0011"]

    def test_nothing_recorded_returns_empty(self, tmp_config):
        hits = find_all_atl_hits()
        assert hits == []


class TestScanPriceChanges:
    def _day(self, offset: int) -> str:
        return (datetime.date.today() - datetime.timedelta(days=offset)).isoformat()

    def test_drop_across_cutoff(self):
        # Entry before window at 10.0, entry within window at 7.0 → drop
        histories = _make_hist("SC01", [(self._day(10), 10.0), (self._day(2), 7.0)])
        drops, new_items = scan_price_changes(7, histories=histories)
        assert len(drops) == 1
        asin, title, old, new = drops[0]
        assert asin == "SC01"
        assert old == pytest.approx(10.0)
        assert new == pytest.approx(7.0)
        assert new_items == []

    def test_all_within_window_with_drop(self):
        # Both entries within window, price fell → in drops
        histories = _make_hist("SC02", [(self._day(3), 10.0), (self._day(1), 6.0)])
        drops, new_items = scan_price_changes(7, histories=histories)
        assert any(d[0] == "SC02" for d in drops)
        assert not any(n[0] == "SC02" for n in new_items)

    def test_all_within_window_no_drop_goes_to_new_items(self):
        # Both entries within window, price stable → new_item
        histories = _make_hist("SC03", [(self._day(3), 8.0), (self._day(1), 8.0)])
        drops, new_items = scan_price_changes(7, histories=histories)
        assert not any(d[0] == "SC03" for d in drops)
        assert any(n[0] == "SC03" for n in new_items)
        match = next(n for n in new_items if n[0] == "SC03")
        assert match[2] == pytest.approx(8.0)

    def test_single_entry_within_window_is_new_item(self):
        histories = _make_hist("SC04", [(self._day(2), 5.0)])
        drops, new_items = scan_price_changes(7, histories=histories)
        assert any(n[0] == "SC04" for n in new_items)
        assert not any(d[0] == "SC04" for d in drops)

    def test_none_price_and_missing_date_no_exception(self):
        # Entry with None price and one with missing date key
        histories = {
            "SC05": [
                {"price": None, "title": "T"},  # missing date
                {"date": self._day(2), "price": None, "title": "T"},
                {"date": self._day(1), "price": 5.0, "title": "T"},
            ]
        }
        # Must not raise; results may be empty or partial, just no exception
        drops, new_items = scan_price_changes(7, histories=histories)
        # SC05 is skipped entirely: the dateless first entry causes len(entries) != len(recent),
        # so the all-within-window branch is bypassed and before is empty, leaving SC05 in neither drops nor new_items
        assert not any(d[0] == "SC05" for d in drops)
        assert not any(n[0] == "SC05" for n in new_items)

    def test_straddling_cutoff_price_increase_not_reported(self):
        # Entry before window at 5.0, within window at 9.0 → not a drop, not new
        histories = _make_hist("SC06", [(self._day(10), 5.0), (self._day(2), 9.0)])
        drops, new_items = scan_price_changes(7, histories=histories)
        assert not any(d[0] == "SC06" for d in drops)
        assert not any(n[0] == "SC06" for n in new_items)


class TestPriceHistoryContext:
    def _write_history(self, tmp_config, asin, entries):
        seed_history(asin, entries)

    def test_single_entry_today_not_atl(self, tmp_config):
        today = datetime.date.today().isoformat()
        self._write_history(
            tmp_config, "PHC01", [{"date": today, "price": 5.0, "title": "T"}]
        )
        p = make_product(asin="PHC01", price=5.0)
        atl_asins, _ = price_history_context([p])
        assert "PHC01" not in atl_asins

    def test_prior_higher_prices_current_at_new_low(self, tmp_config):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        two_ago = (datetime.date.today() - datetime.timedelta(days=2)).isoformat()
        today = datetime.date.today().isoformat()
        self._write_history(
            tmp_config,
            "PHC02",
            [
                {"date": two_ago, "price": 10.0, "title": "T"},
                {"date": yesterday, "price": 8.0, "title": "T"},
                {"date": today, "price": 5.0, "title": "T"},
            ],
        )
        p = make_product(asin="PHC02", price=5.0)
        atl_asins, _ = price_history_context([p])
        assert "PHC02" in atl_asins

    def test_with_preloaded_histories(self, tmp_config):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        histories = {
            "us:PHC03": [
                {"date": yesterday, "price": 12.0, "title": "T"},
            ]
        }
        p = make_product(asin="PHC03", price=5.0)
        atl_asins, _ = price_history_context([p], histories=histories)
        assert "PHC03" in atl_asins

    def test_price_not_atl_when_current_above_prior_min(self, tmp_config):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        today = datetime.date.today().isoformat()
        self._write_history(
            tmp_config,
            "PHC04",
            [
                {"date": yesterday, "price": 5.0, "title": "T"},
                {"date": today, "price": 8.0, "title": "T"},
            ],
        )
        p = make_product(asin="PHC04", price=8.0)
        atl_asins, _ = price_history_context([p])
        assert "PHC04" not in atl_asins


class TestRecordPricesConcurrency:
    def test_concurrent_marketplace_updates_preserve_both_locales(self, tmp_config):
        asin = "B00LOCKED1"
        start = threading.Barrier(2)
        failures = []

        def _record(locale, price):
            try:
                start.wait(timeout=2)
                record_prices(
                    [make_product(asin=asin, locale=locale, price=price, title=locale)]
                )
            except Exception as exc:
                failures.append(exc)

        threads = [
            threading.Thread(target=_record, args=("us", 10.0)),
            threading.Thread(target=_record, args=("uk", 5.0)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert not any(thread.is_alive() for thread in threads)
        assert not failures
        assert load_price_history(asin, "us")[0]["price"] == 10.0
        assert load_price_history(asin, "uk")[0]["price"] == 5.0

    def test_a_failed_batch_records_nothing(self, tmp_config):
        """One bad row rolls the whole batch back instead of half-recording it."""
        products = [
            make_product(asin="B00BATCH01", price=1.0),
            make_product(asin="B00BATCH02", price=2.0, locale=["unbindable"]),
        ]
        with pytest.raises((sqlite3.InterfaceError, sqlite3.ProgrammingError)):
            record_prices(products)

        assert load_price_history("B00BATCH01") == []
        assert load_price_history("B00BATCH02") == []


class TestRecordPricesRules:
    def test_first_price_of_the_day_wins(self, tmp_config):
        record_prices([make_product(asin="B00RULES01", price=9.99)])
        record_prices([make_product(asin="B00RULES01", price=1.99)])
        entries = load_price_history("B00RULES01")
        assert [e["price"] for e in entries] == [9.99]

    def test_price_is_rounded_to_cents(self, tmp_config):
        record_prices([make_product(asin="B00RULES02", price=4.999)])
        assert load_price_history("B00RULES02")[0]["price"] == 5.0

    def test_keeps_the_newest_365_entries(self, tmp_config):
        start = datetime.date(2023, 1, 1)
        for i in range(370):
            day = (start + datetime.timedelta(days=i)).isoformat()
            record_prices(
                [make_product(asin="B00RULES03", price=1.0 + i)],
                observation_date=day,
            )
        entries = load_price_history("B00RULES03")
        assert len(entries) == 365
        assert entries[0]["date"] == (start + datetime.timedelta(days=5)).isoformat()
        assert entries[-1]["date"] == (start + datetime.timedelta(days=369)).isoformat()

    def test_cap_applies_per_marketplace(self, tmp_config):
        start = datetime.date(2023, 1, 1)
        for i in range(366):
            day = (start + datetime.timedelta(days=i)).isoformat()
            record_prices(
                [make_product(asin="B00RULES04", price=2.0, locale="us")],
                observation_date=day,
            )
        record_prices([make_product(asin="B00RULES04", price=3.0, locale="uk")])
        assert len(load_price_history("B00RULES04", "us")) == 365
        assert len(load_price_history("B00RULES04", "uk")) == 1

    def test_back_dated_observation_is_sorted_into_place(self, tmp_config):
        record_prices(
            [make_product(asin="B00RULES05", price=5.0)], observation_date="2024-03-01"
        )
        record_prices(
            [make_product(asin="B00RULES05", price=4.0)], observation_date="2024-01-01"
        )
        assert [e["date"] for e in load_price_history("B00RULES05")] == [
            "2024-01-01",
            "2024-03-01",
        ]

    def test_unpriced_and_invalid_asins_are_skipped(self, tmp_config):
        record_prices(
            [
                make_product(asin="B00RULES06", price=None),
                make_product(asin="not a valid asin!", price=3.0),
            ]
        )
        assert load_all_price_histories() == {}

    def test_observation_date_must_be_a_date(self, tmp_config):
        with pytest.raises(TypeError):
            record_prices([make_product(price=1.0)], observation_date=5)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            record_prices([make_product(price=1.0)], observation_date="yesterday")

    def test_title_is_kept_per_entry_and_indexed(self, tmp_config):
        record_prices(
            [make_product(asin="B00RULES07", price=3.0, title="First")],
            observation_date="2024-01-01",
        )
        record_prices(
            [make_product(asin="B00RULES07", price=2.0, title="Renamed")],
            observation_date="2024-01-02",
        )
        assert [e["title"] for e in load_price_history("B00RULES07")] == [
            "First",
            "Renamed",
        ]
        row = (
            db.state()
            .execute(
                "SELECT title, last_price, last_seen FROM products WHERE asin = 'B00RULES07'"
            )
            .fetchone()
        )
        assert row == ("Renamed", 2.0, "2024-01-02")

    def test_product_index_keeps_the_newest_sighting(self, tmp_config):
        record_prices(
            [make_product(asin="B00RULES08", price=3.0, title="Newer")],
            observation_date="2024-02-01",
        )
        record_prices(
            [make_product(asin="B00RULES08", price=9.0, title="Older")],
            observation_date="2024-01-01",
        )
        row = (
            db.state()
            .execute(
                "SELECT title, last_price, last_seen FROM products WHERE asin = 'B00RULES08'"
            )
            .fetchone()
        )
        assert row == ("Newer", 3.0, "2024-02-01")


class TestBugfixPriceHistoryValidationFindWishlistHitsNumericGuard:
    def _write_history(self, asin: str, entries: list[dict]) -> None:
        seed_history(asin, [e for e in entries if isinstance(e.get("price"), float)])

    def test_string_max_price_is_skipped(self, tmp_config):
        wishlist_mod.save_wishlist(
            [{"asin": "B00STR0001", "title": "T", "max_price": "10", "added": ""}]
        )
        self._write_history(
            "B00STR0001",
            [{"date": "2024-01-02", "price": 7.5, "title": "T"}],
        )
        assert price_history.find_wishlist_hits() == []

    def test_valid_numeric_hit_still_matches(self, tmp_config):
        item = {"asin": "B00HIT0001", "title": "T", "max_price": 10.0, "added": ""}
        wishlist_mod.save_wishlist([item])
        self._write_history(
            "B00HIT0001",
            [{"date": "2024-01-02", "price": 7.5, "title": "T"}],
        )
        assert price_history.find_wishlist_hits() == [item]

    def test_latest_price_above_target_is_not_a_hit(self, tmp_config):
        wishlist_mod.save_wishlist(
            [{"asin": "B00ABOVE01", "title": "T", "max_price": 5.0, "added": ""}]
        )
        self._write_history(
            "B00ABOVE01",
            [
                {"date": "2024-01-01", "price": 4.0, "title": "T"},
                {"date": "2024-01-02", "price": 7.5, "title": "T"},
            ],
        )
        assert price_history.find_wishlist_hits() == []

    def test_wishlist_item_without_history_is_not_a_hit(self, tmp_config):
        wishlist_mod.save_wishlist(
            [{"asin": "B00NOHIST1", "title": "T", "max_price": 5.0, "added": ""}]
        )
        assert price_history.find_wishlist_hits() == []

    def test_item_locale_selects_the_marketplace_history(self, tmp_config):
        item = {
            "asin": "B00LOCALE1",
            "title": "T",
            "max_price": 6.0,
            "added": "",
            "locale": "uk",
        }
        wishlist_mod.save_wishlist([item])
        seed_history("B00LOCALE1", [("2024-01-02", 9.0)], locale="us")
        seed_history("B00LOCALE1", [("2024-01-02", 4.0)], locale="uk")
        assert price_history.find_wishlist_hits() == [item]


class TestBugfixPriceHistoryValidationMarketplaceScopedHistory:
    def test_identical_asins_never_share_marketplace_prices(self, tmp_config):
        asin = "B00MARKET1"
        price_history.record_prices(
            [
                make_product(asin=asin, locale="us", price=10.0, title="US title"),
                make_product(asin=asin, locale="uk", price=5.0, title="UK title"),
            ]
        )

        assert price_history.load_price_history(asin, "us")[0]["price"] == 10.0
        assert price_history.load_price_history(asin, "uk")[0]["price"] == 5.0


class TestQueriesMatchTheInMemoryRules:
    """The SQL fast paths must give exactly the answers the plain rules give on
    the same data (the rules still run when callers pass preloaded histories)."""

    @staticmethod
    def _random_catalog(seed: int, size: int = 120):
        import random

        rng = random.Random(seed)
        today = datetime.date.today()
        for i in range(size):
            asin = f"B00RND{i:04d}"
            locale = rng.choice(["us", "us", "us", "uk"])
            days = sorted(rng.sample(range(0, 40), rng.randint(1, 9)), reverse=True)
            entries = [
                {
                    "date": (today - datetime.timedelta(days=d)).isoformat(),
                    "price": round(
                        rng.choice([2.0, 3.0, 4.5, 6.0, 8.0, 12.0]) + rng.random(), 2
                    )
                    if rng.random() < 0.7
                    else rng.choice([2.0, 3.0, 6.0]),
                    "title": rng.choice(["", f"Book {i}", f"Book {i} v2"]),
                }
                for d in days
            ]
            seed_history(asin, entries, locale=locale)

    @pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
    @pytest.mark.parametrize("days", [1, 3, 7, 14, 30])
    def test_scan_price_changes(self, tmp_config, seed, days):
        self._random_catalog(seed)
        expected_drops, expected_new = scan_price_changes(
            days, histories=load_all_price_histories("us")
        )

        drops, new_items = scan_price_changes(days)

        assert sorted(drops) == sorted(expected_drops)
        assert sorted(new_items) == sorted(expected_new)
        assert drops or new_items  # the generator produces some

    @pytest.mark.parametrize("seed", [1, 2, 3])
    def test_find_all_atl_hits(self, tmp_config, seed):
        self._random_catalog(seed)
        histories = load_all_price_histories("us")

        from_sql = find_all_atl_hits(limit=1000)
        from_memory = find_all_atl_hits(limit=1000, histories=histories)

        key = lambda hit: (hit["asin"], hit["price"], hit["title"], hit["target"])  # noqa: E731
        assert sorted(map(key, from_sql)) == sorted(map(key, from_memory))
        assert from_sql  # the generator produces some

    def test_has_price_history_is_per_marketplace(self, tmp_config):
        seed_history("B00HAS0001", [("2026-01-01", 1.0)], locale="uk")
        assert price_history.has_price_history("uk")
        assert not price_history.has_price_history("us")
