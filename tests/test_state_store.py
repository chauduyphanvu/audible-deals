"""Automation state (monitor baselines, notify cooldowns) and the disposable cache."""

from __future__ import annotations

import time

import pytest

from audible_deals import cache_store, db
from audible_deals.automation_models import MonitorSnapshot
from audible_deals.state_store import (
    delete_monitor_state,
    load_monitor_snapshot,
    load_notify_state,
    monitor_summaries,
    record_monitor_failure,
    save_monitor_snapshot,
    save_notify_state,
)


def product(asin, price):
    return {"asin": asin, "title": f"Title {asin}", "price": price, "url": f"u/{asin}"}


class TestMonitorSnapshots:
    def test_unknown_monitor_has_an_empty_uninitialized_snapshot(self, tmp_config):
        assert load_monitor_snapshot("nope") == MonitorSnapshot()

    def test_round_trip(self, tmp_config):
        snapshot = MonitorSnapshot(
            initialized=True,
            products={"A1": product("A1", 3.5), "A2": product("A2", None)},
            last_success="2026-01-02T10:00:00",
            last_error="RuntimeError: old",
        )
        save_monitor_snapshot("cheap", snapshot)

        assert load_monitor_snapshot("cheap") == snapshot

    def test_saving_replaces_the_previous_products(self, tmp_config):
        save_monitor_snapshot(
            "cheap",
            MonitorSnapshot(initialized=True, products={"A1": product("A1", 3)}),
        )
        save_monitor_snapshot(
            "cheap",
            MonitorSnapshot(initialized=True, products={"B1": product("B1", 4)}),
        )
        assert set(load_monitor_snapshot("cheap").products) == {"B1"}

    def test_monitors_do_not_share_products(self, tmp_config):
        save_monitor_snapshot("one", MonitorSnapshot(products={"A1": product("A1", 1)}))
        save_monitor_snapshot("two", MonitorSnapshot(products={"A1": product("A1", 2)}))
        assert load_monitor_snapshot("one").products["A1"]["price"] == 1
        assert load_monitor_snapshot("two").products["A1"]["price"] == 2

    def test_summaries_do_not_load_the_products(self, tmp_config):
        save_monitor_snapshot(
            "cheap",
            MonitorSnapshot(
                initialized=True,
                products={f"A{i}": product(f"A{i}", 1) for i in range(7)},
                last_success="2026-01-02T10:00:00",
            ),
        )
        summary = monitor_summaries()["cheap"]
        assert summary.product_count == 7
        assert summary.initialized is True
        assert summary.last_success == "2026-01-02T10:00:00"
        assert summary.last_error is None

    def test_a_failure_keeps_the_baseline(self, tmp_config):
        save_monitor_snapshot(
            "cheap",
            MonitorSnapshot(initialized=True, products={"A1": product("A1", 3)}),
        )
        record_monitor_failure("cheap", "boom")

        snapshot = load_monitor_snapshot("cheap")
        assert snapshot.last_error == "boom"
        assert snapshot.initialized is True
        assert set(snapshot.products) == {"A1"}

    def test_deleting_removes_the_products_too(self, tmp_config):
        save_monitor_snapshot(
            "cheap", MonitorSnapshot(products={"A1": product("A1", 3)})
        )
        delete_monitor_state("cheap")
        delete_monitor_state("never-existed")

        assert monitor_summaries() == {}
        assert (
            db.state().execute("SELECT COUNT(*) FROM monitor_products").fetchone()[0]
            == 0
        )

    def test_non_numeric_prices_are_stored_as_null(self, tmp_config):
        save_monitor_snapshot(
            "m",
            MonitorSnapshot(
                products={"A1": product("A1", "free"), "A2": product("A2", True)}
            ),
        )
        rows = dict(db.state().execute("SELECT asin, price FROM monitor_products"))
        assert rows == {"A1": None, "A2": None}

    def test_a_large_snapshot_round_trips(self, tmp_config):
        products = {f"A{i:05d}": product(f"A{i:05d}", i / 10) for i in range(1500)}
        save_monitor_snapshot(
            "big", MonitorSnapshot(initialized=True, products=products)
        )
        assert load_monitor_snapshot("big").products == products


class TestNotifyState:
    def test_round_trip_replaces_everything(self, tmp_config):
        save_notify_state({"A1": {"price": 3.5, "date": "2026-01-02"}})
        save_notify_state({"B1": {"price": 1, "date": "2026-01-03"}})

        assert load_notify_state() == {"B1": {"price": 1.0, "date": "2026-01-03"}}

    def test_empty_state(self, tmp_config):
        assert load_notify_state() == {}
        save_notify_state({})
        assert load_notify_state() == {}

    @pytest.mark.parametrize(
        "entry",
        [
            "text",
            None,
            {"price": 1},
            {"date": "2026-01-02"},
            {"price": "1", "date": "x"},
            {"price": True, "date": "2026-01-02"},
        ],
    )
    def test_malformed_entries_are_not_stored(self, tmp_config, entry):
        save_notify_state({"BAD": entry, "OK": {"price": 2.0, "date": "2026-01-02"}})
        assert set(load_notify_state()) == {"OK"}


class TestCacheStore:
    def test_miss_then_hit(self, tmp_config):
        assert cache_store.get("ns", "k") is None
        cache_store.put("ns", "k", {"a": [1, 2]})
        assert cache_store.get("ns", "k") == {"a": [1, 2]}

    def test_namespaces_and_keys_are_independent(self, tmp_config):
        cache_store.put("one", "k", 1)
        cache_store.put("two", "k", 2)
        cache_store.put("one", "other", 3)
        assert (
            cache_store.get("one", "k"),
            cache_store.get("two", "k"),
            cache_store.get("one", "other"),
        ) == (1, 2, 3)

    def test_put_replaces(self, tmp_config):
        cache_store.put("ns", "k", "old")
        cache_store.put("ns", "k", "new")
        assert cache_store.get("ns", "k") == "new"

    def test_entries_expire(self, tmp_config, monkeypatch):
        cache_store.put("ns", "k", "v")
        now = time.time()
        monkeypatch.setattr(cache_store.time, "time", lambda: now + 100)

        assert cache_store.get("ns", "k", max_age_s=200) == "v"
        assert cache_store.get("ns", "k", max_age_s=50) is None
        assert cache_store.get("ns", "k") == "v"

    def test_clear_one_namespace_or_everything(self, tmp_config):
        cache_store.put("one", "a", 1)
        cache_store.put("one", "b", 1)
        cache_store.put("two", "a", 1)

        assert cache_store.clear("one") == 2
        assert cache_store.get("two", "a") == 1
        assert cache_store.clear() == 1
        assert cache_store.clear() == 0

    def test_summary_counts_by_namespace(self, tmp_config):
        cache_store.put("one", "a", 1)
        cache_store.put("one", "b", 1)
        cache_store.put("two", "a", 1)
        summary = cache_store.summary()
        assert {name: count for name, (count, _) in summary.items()} == {
            "one": 2,
            "two": 1,
        }

    def test_unreadable_values_read_as_a_miss(self, tmp_config):
        with db.transaction(db.cache()) as conn:
            conn.execute(
                "INSERT INTO cache_entries (namespace, key, value, stored_at) "
                "VALUES ('ns', 'k', '{not json', 0)"
            )
        assert cache_store.get("ns", "k") is None

    def test_failures_never_propagate(self, tmp_config, monkeypatch):
        def broken():
            raise RuntimeError("no cache")

        monkeypatch.setattr(cache_store.db, "cache", broken)
        assert cache_store.get("ns", "k") is None
        cache_store.put("ns", "k", 1)  # must not raise

    def test_the_cache_never_touches_the_state_database(self, tmp_config):
        cache_store.put("ns", "k", 1)
        assert not (tmp_config / "state.db").exists()
