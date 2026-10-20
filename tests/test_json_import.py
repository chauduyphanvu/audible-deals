"""The one-time import of the retired JSON state files into state.db."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from audible_deals import db, json_import, paths
from audible_deals.logging_setup import NOTE
from audible_deals.price_history import load_all_price_histories, load_price_history
from audible_deals.refresh_eligibility import load_refresh_eligibility
from audible_deals.results_cache import (
    load_dismissed_asins,
    load_result_session,
    load_seen_asins,
)
from audible_deals.state_store import (
    load_install_record,
    load_monitor_snapshot,
    load_notify_state,
    load_track_state,
    monitor_summaries,
)
from audible_deals.storage import read_text

# These tests are about creating and upgrading the databases themselves.
pytestmark = pytest.mark.fresh_schema


def put(name: str, content, *, directory: Path | None = None) -> Path:
    """Write a legacy file (text, bytes or JSON-able data) into the data directory."""
    path = (directory or paths.data_dir()) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    elif isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    return path


def history_file(asin: str, marketplaces) -> Path:
    return put(f"history/{asin}.json", {"marketplaces": marketplaces})


def entry(date: str, price, title: str = "T") -> dict:
    return {"date": date, "price": price, "title": title}


def summary() -> dict:
    result = json_import.summary(db.state())
    assert result is not None
    return result


def names(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir()}


# ---------------------------------------------------------------------------
# A fresh install and untouched files
# ---------------------------------------------------------------------------


class TestNothingToImport:
    def test_a_fresh_install_records_an_empty_import_and_renames_nothing(
        self, tmp_config
    ):
        db.state()

        result = summary()
        assert result["sources"] == []
        assert result["counts"] == {}
        assert result["renames_pending"] == []
        assert not [n for n in names(tmp_config) if n.endswith(".migrated")]

    def test_a_fresh_install_does_not_announce_a_migration(self, tmp_config, caplog):
        with caplog.at_level(logging.INFO):
            db.state()
        assert "Moved your saved data" not in caplog.text

    def test_the_import_is_not_attempted_twice(self, tmp_config, monkeypatch):
        db.state()
        db.close_all()
        calls = []
        monkeypatch.setattr(json_import, "parse_sources", lambda: calls.append(1))

        db.state()

        assert calls == []

    def test_files_the_user_owns_are_never_touched(self, tmp_config):
        owned = {
            "config.json": '{"credit_price": 9.5}',
            "profiles.json": '{"p": {"genre": "sci-fi"}}',
            "wishlist.json": "[]",
            "monitors.json": "{}",
            "auth.json": '{"access_token": "x"}',
        }
        for name, content in owned.items():
            put(name, content)
        put("seen_asins.json", ["A1"])

        db.state()

        for name, content in owned.items():
            assert (tmp_config / name).read_text() == content
            assert not (tmp_config / f"{name}.migrated").exists()


# ---------------------------------------------------------------------------
# Price history
# ---------------------------------------------------------------------------


class TestHistoryImport:
    def test_current_format_entries_arrive_with_their_titles(self, tmp_config):
        history_file(
            "B00HIST001",
            {
                "us": [
                    entry("2026-01-02", 4.5, "Newer"),
                    entry("2026-01-01", 5, "Old"),
                ],
                "uk": [entry("2026-01-03", 3.25, "UK")],
            },
        )

        assert load_price_history("B00HIST001", "us") == [
            entry("2026-01-01", 5.0, "Old"),
            entry("2026-01-02", 4.5, "Newer"),
        ]
        assert load_price_history("B00HIST001", "uk") == [
            entry("2026-01-03", 3.25, "UK")
        ]

    def test_prices_come_back_as_numbers_not_text(self, tmp_config):
        history_file("B00HIST002", {"us": [entry("2026-01-01", 7)]})
        value = load_price_history("B00HIST002")[0]["price"]
        assert value == 7.0
        assert isinstance(value, float)

    def test_the_product_index_is_built_from_the_latest_entries(self, tmp_config):
        history_file(
            "B00HIST003",
            {
                "us": [
                    entry("2026-01-01", 9.0, "First"),
                    entry("2026-01-05", 6.0, ""),
                    entry("2026-01-03", 7.5, "Middle"),
                ]
            },
        )

        db.state()

        row = (
            db.state()
            .execute(
                "SELECT title, last_price, last_seen FROM products WHERE asin = 'B00HIST003'"
            )
            .fetchone()
        )
        # the newest entry sets price and date; the title is the newest non-empty one
        assert row == ("Middle", 6.0, "2026-01-05")

    def test_two_entries_for_one_day_collapse_to_the_later_one_in_the_file(
        self, tmp_config
    ):
        history_file(
            "B00HIST004",
            {"us": [entry("2026-01-01", 5.0, "a"), entry("2026-01-01", 4.0, "b")]},
        )
        assert load_price_history("B00HIST004") == [entry("2026-01-01", 4.0, "b")]

    @pytest.mark.parametrize(
        "bad",
        [
            "not an entry",
            None,
            {"price": 2.0, "title": "no date"},
            {"date": "yesterday", "price": 2.0},
            {"date": "2026-13-45", "price": 2.0},
            {"date": "2026-01-02", "price": None},
            {"date": "2026-01-02", "price": True},
            {"date": "2026-01-02", "price": "3.5"},
            {"date": "2026-01-02", "price": float("nan")},
            {"date": "2026-01-02", "price": float("inf")},
        ],
    )
    def test_unusable_entries_are_skipped_and_counted(self, tmp_config, bad):
        history_file("B00HIST005", {"us": [entry("2026-01-01", 3.0), bad]})

        assert load_price_history("B00HIST005") == [entry("2026-01-01", 3.0)]
        assert summary()["skipped"] == {
            "history entries without a valid date and price": 1
        }

    def test_non_ascii_titles_survive(self, tmp_config):
        history_file(
            "B00HIST006", {"us": [entry("2026-01-01", 3.0, "Café – 日本語 🎧")]}
        )
        assert load_price_history("B00HIST006")[0]["title"] == "Café – 日本語 🎧"

    def test_more_than_the_cap_is_kept_until_the_next_write(self, tmp_config):
        import datetime

        start = datetime.date(2025, 1, 1)
        history_file(
            "B00HIST007",
            {
                "us": [
                    entry((start + datetime.timedelta(days=i)).isoformat(), 5.0)
                    for i in range(400)
                ]
            },
        )
        assert len(load_price_history("B00HIST007")) == 400

    def test_a_legacy_unscoped_list_is_not_assigned_to_a_marketplace(self, tmp_config):
        original = b'[ {"date": "2024-01-01", "price": 10.0} ]\n'
        put("history/B00LEGACY1.json", original)

        assert load_price_history("B00LEGACY1", "us") == []
        assert load_all_price_histories("us") == {}
        # nothing is lost: the bytes are kept as they were
        kept = tmp_config / "history.migrated" / "B00LEGACY1.json"
        assert kept.read_bytes() == original
        assert summary()["skipped"] == {"legacy unscoped history files": 1}

    def test_archives_and_backups_travel_with_the_directory(self, tmp_config):
        history_file("B00HIST008", {"us": [entry("2026-01-01", 3.0)]})
        put("history/B00OLD0001.json.legacy", b"older")
        put("history/B00BAD0001.json.bak", b"broken")
        put("history/.B00HIST008.json.lock", b"")

        assert [e["price"] for e in load_price_history("B00HIST008")] == [3.0]

        moved = tmp_config / "history.migrated"
        assert {
            "B00HIST008.json",
            "B00OLD0001.json.legacy",
            "B00BAD0001.json.bak",
            ".B00HIST008.json.lock",
        } <= names(moved)
        assert (moved / "B00OLD0001.json.legacy").read_bytes() == b"older"
        assert not (tmp_config / "history").exists()

    @pytest.mark.parametrize(
        ("content", "note"),
        [
            ("this is not json", "is not valid JSON"),
            ('{"marketplaces": {"us": [', "is not valid JSON"),
            ("", None),
            ('"just a string"', "does not hold the expected data"),
        ],
    )
    def test_unreadable_history_files_are_skipped_but_kept(
        self, tmp_config, content, note
    ):
        put("history/B00BROKEN1.json", content)
        history_file("B00GOOD001", {"us": [entry("2026-01-01", 2.0)]})

        assert set(load_all_price_histories()) == {"B00GOOD001"}
        assert (
            tmp_config / "history.migrated" / "B00BROKEN1.json"
        ).read_text() == content
        if note:
            assert any(note in n for n in summary()["notes"])

    def test_a_history_dict_with_the_wrong_inner_shape_is_skipped(self, tmp_config):
        put("history/B00SHAPE01.json", {"marketplaces": []})
        put("history/B00SHAPE02.json", {"marketplaces": {"us": "nope"}})
        put("history/B00SHAPE03.json", {"other": 1})

        assert load_all_price_histories() == {}
        assert sum(summary()["skipped"].values()) == 3

    def test_files_whose_name_is_not_an_asin_are_skipped(self, tmp_config):
        history_file("not an asin!", {"us": [entry("2026-01-01", 2.0)]})
        assert load_all_price_histories() == {}
        assert summary()["skipped"] == {"history files with an invalid ASIN": 1}

    def test_a_utf8_bom_is_tolerated(self, tmp_config):
        payload = json.dumps({"marketplaces": {"us": [entry("2026-01-01", 2.0)]}})
        put("history/B00BOM0001.json", b"\xef\xbb\xbf" + payload.encode())
        assert len(load_price_history("B00BOM0001")) == 1


# ---------------------------------------------------------------------------
# seen / dismissed / eligibility
# ---------------------------------------------------------------------------


class TestSetsImport:
    def test_seen_and_dismissed_arrive_as_sets(self, tmp_config):
        put("seen_asins.json", ["S1", "S2", "S1"])
        put("dismissed_asins.json", ["D1"])

        assert load_seen_asins() == {"S1", "S2"}
        assert load_dismissed_asins() == {"D1"}

    def test_non_text_entries_are_ignored(self, tmp_config):
        put("dismissed_asins.json", ["A1", 2, {}, None])
        assert load_dismissed_asins() == {"A1"}
        assert summary()["skipped"] == {"dismissed_asins entries that were not text": 3}

    def test_duplicate_asins_are_merged_without_a_warning(self, tmp_config, caplog):
        put("seen_asins.json", ["S1", "S2", "S1", "S1"])
        put("dismissed_asins.json", ["D1", "D1"])

        with caplog.at_level(logging.WARNING, logger="audible_deals.json_import"):
            assert load_seen_asins() == {"S1", "S2"}

        assert load_dismissed_asins() == {"D1"}
        assert summary()["skipped"] == {}
        assert summary()["notes"] == []
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_only_entries_that_are_not_text_are_counted(self, tmp_config):
        put("seen_asins.json", ["A1", "A1", 2, None, "A2", "A2"])

        assert load_seen_asins() == {"A1", "A2"}
        assert summary()["skipped"] == {"seen_asins entries that were not text": 2}

    @pytest.mark.parametrize("content", ["not json", '{"asin": "A1"}'])
    def test_unusable_files_import_as_empty_and_are_kept(self, tmp_config, content):
        put("seen_asins.json", content)

        assert load_seen_asins() == set()
        assert (tmp_config / "seen_asins.json.migrated").read_text() == content
        assert summary()["notes"]

    def test_eligibility_is_imported_per_marketplace(self, tmp_config):
        put(
            "refresh_eligibility.json",
            {
                "version": 1,
                "marketplaces": {
                    "us": {"B00ELIG001": "2026-01-05"},
                    "uk": {"B00ELIG001": "2026-01-02"},
                },
            },
        )
        assert load_refresh_eligibility() == {
            "us": {"B00ELIG001": "2026-01-05"},
            "uk": {"B00ELIG001": "2026-01-02"},
        }

    def test_missing_eligibility_is_rebuilt_from_seen_history(self, tmp_config):
        put("seen_asins.json", ["B00SEEN001"])
        history_file(
            "B00SEEN001",
            {"us": [entry("2026-01-01", 4.0), entry("2026-01-04", 3.0)]},
        )
        history_file("B00UNSEEN1", {"us": [entry("2026-01-09", 3.0)]})

        assert load_refresh_eligibility() == {"us": {"B00SEEN001": "2026-01-04"}}

    def test_an_existing_eligibility_file_is_not_second_guessed(self, tmp_config):
        put("seen_asins.json", ["B00SEEN001"])
        history_file("B00SEEN001", {"us": [entry("2026-01-04", 3.0)]})
        put("refresh_eligibility.json", {"version": 1, "marketplaces": {}})

        assert load_refresh_eligibility() == {}


# ---------------------------------------------------------------------------
# monitors, sessions, notify and track state
# ---------------------------------------------------------------------------


def product(asin: str, price) -> dict:
    return {"asin": asin, "title": asin, "price": price, "url": f"https://x/{asin}"}


class TestMonitorStateImport:
    def test_baselines_arrive_with_products_and_health(self, tmp_config):
        put(
            "monitor_state.json",
            {
                "version": 1,
                "monitors": {
                    "cheap": {
                        "initialized": True,
                        "products": {
                            "A1": product("A1", 3.5),
                            "A2": product("A2", None),
                        },
                        "last_success": "2026-01-02T10:00:00",
                        "last_error": None,
                        "future": {"dropped": True},
                    },
                    "failing": {
                        "initialized": False,
                        "products": {},
                        "last_error": "boom",
                    },
                },
            },
        )

        snapshot = load_monitor_snapshot("cheap")
        assert snapshot.initialized is True
        assert snapshot.products == {
            "A1": product("A1", 3.5),
            "A2": product("A2", None),
        }
        assert snapshot.last_success == "2026-01-02T10:00:00"
        assert load_monitor_snapshot("failing").last_error == "boom"
        assert set(monitor_summaries()) == {"cheap", "failing"}

    def test_an_unsupported_version_imports_nothing(self, tmp_config):
        put(
            "monitor_state.json",
            {"version": 2, "monitors": {"m": {"initialized": True}}},
        )
        assert monitor_summaries() == {}
        assert any("unsupported version" in n for n in summary()["notes"])

    def test_malformed_slots_are_skipped_and_good_siblings_kept(self, tmp_config):
        put(
            "monitor_state.json",
            {
                "version": 1,
                "monitors": {
                    "bad": [],
                    "worse": {"initialized": True, "products": []},
                    "good": {
                        "initialized": True,
                        "products": {"A1": product("A1", 1.0), "A2": "junk", 5: {}},
                    },
                },
            },
        )

        assert set(monitor_summaries()) == {"good"}
        # non-dict products are dropped; a dict is kept whatever it holds
        assert set(load_monitor_snapshot("good").products) == {"A1", "5"}

    def test_a_monitor_without_a_baseline_stays_uninitialized(self, tmp_config):
        put("monitor_state.json", {"version": 1, "monitors": {}})
        assert load_monitor_snapshot("anything").initialized is False


class TestSessionImport:
    def test_the_session_body_is_kept_byte_for_byte(self, tmp_config):
        body = json.dumps(
            {"title": "Café results", "results": [{"asin": "A1", "title": "Ünï"}]},
            ensure_ascii=False,
        )
        put("last_results.json", body)

        from tests.conftest import raw_session

        assert raw_session() == body

    def test_legacy_list_shaped_sessions_still_load(self, tmp_config):
        put("last_results.json", [{"asin": "B00LEGLIST", "title": "Old format"}])

        session = load_result_session()

        assert session.legacy is True
        assert [item["asin"] for item in session.visible_results] == ["B00LEGLIST"]

    def test_a_corrupt_session_is_skipped_and_kept(self, tmp_config):
        put("last_results.json", "{broken")

        from tests.conftest import raw_session

        assert raw_session() is None
        assert (tmp_config / "last_results.json.migrated").read_text() == "{broken"


class TestNotifyStateImport:
    def test_cooldown_records_arrive(self, tmp_config):
        put(
            "notify_state.json",
            {"B00NOTIFY1": {"price": 3.99, "date": "2026-01-02"}},
        )
        assert load_notify_state() == {
            "B00NOTIFY1": {"price": 3.99, "date": "2026-01-02"}
        }

    def test_malformed_records_are_skipped(self, tmp_config):
        put(
            "notify_state.json",
            {
                "GOOD": {"price": 1, "date": "2026-01-02"},
                "TEXT": "no",
                "NODATE": {"price": 1},
                "BADDATE": {"price": 1, "date": "someday"},
                "NOPRICE": {"date": "2026-01-02"},
            },
        )
        assert set(load_notify_state()) == {"GOOD"}
        assert summary()["skipped"] == {"notify-state entries that were malformed": 4}


class TestTrackStateImport:
    def run(self, n: int, **extra) -> dict:
        return {
            "at": f"2026-01-{n:02d}T10:00:00",
            "duration_s": 1.5,
            "wishlist_checked": n,
            "hits": 1,
            "error": None,
            **extra,
        }

    def test_everything_arrives(self, tmp_config):
        put(
            "track_state.json",
            {
                "refresh_cursors": {
                    "us": {"surfaced_on": "2026-01-02", "asin": "B00CURSOR1"}
                },
                "monitor_cursor": 3,
                "auth_error_notified": True,
                "install": {"every": "6h", "method": "systemd"},
                "run_history": [self.run(2), self.run(1)],
            },
        )

        state = load_track_state()

        assert state.refresh_cursors == {
            "us": {"surfaced_on": "2026-01-02", "asin": "B00CURSOR1"}
        }
        assert state.monitor_cursor == 3
        assert state.auth_error_notified is True
        assert [r["at"] for r in state.runs] == [
            "2026-01-02T10:00:00",
            "2026-01-01T10:00:00",
        ]
        assert load_install_record() == {"every": "6h", "method": "systemd"}

    def test_run_history_keeps_the_ten_newest_in_order(self, tmp_config):
        put(
            "track_state.json",
            {"run_history": [self.run(n) for n in range(15, 0, -1)]},
        )

        runs = load_track_state().runs

        assert [r["wishlist_checked"] for r in runs] == list(range(15, 5, -1))

    def test_sparse_failure_records_stay_sparse(self, tmp_config):
        put(
            "track_state.json",
            {
                "run_history": [
                    {"at": "2026-01-02T10:00:00", "duration_s": 0.1, "error": "boom"}
                ]
            },
        )
        assert load_track_state().runs == [
            {"at": "2026-01-02T10:00:00", "duration_s": 0.1, "error": "boom"}
        ]

    def test_the_old_single_last_run_becomes_the_history(self, tmp_config):
        put("track_state.json", {"last_run": self.run(7)})
        runs = load_track_state().runs
        assert len(runs) == 1
        assert runs[0]["wishlist_checked"] == 7

    def test_a_full_record_keeps_failures_and_flags(self, tmp_config):
        put(
            "track_state.json",
            {
                "run_history": [
                    self.run(
                        1,
                        webhook_sent=True,
                        monitor_failures=["m: boom"],
                        monitors_checked=2,
                    )
                ]
            },
        )
        run = load_track_state().runs[0]
        assert run["webhook_sent"] is True
        assert run["monitor_failures"] == ["m: boom"]
        assert run["monitors_checked"] == 2

    def test_garbage_is_tolerated(self, tmp_config):
        put(
            "track_state.json",
            {
                "refresh_cursors": {
                    "us": "nope",
                    "uk": {"surfaced_on": "bad", "asin": 1},
                },
                "monitor_cursor": "three",
                "install": "not a dict",
                "run_history": [
                    "x",
                    5,
                    None,
                    {"at": "2026-01-01T00:00:00", "hits": "many"},
                ],
            },
        )

        state = load_track_state()

        assert state.refresh_cursors == {}
        assert state.monitor_cursor == 0
        assert load_install_record() is None
        assert len(state.runs) == 1
        assert state.runs[0]["hits"] == 0

    def test_counts_too_large_for_the_database_become_zero(self, tmp_config):
        put(
            "track_state.json",
            {
                "monitor_cursor": 10**30,
                "run_history": [
                    {
                        "at": "2026-01-01T00:00:00",
                        "hits": 10**30,
                        "wishlist_checked": 1e300,
                    }
                ],
            },
        )

        state = load_track_state()

        assert state.monitor_cursor == 0
        assert state.runs[0]["hits"] == 0
        assert state.runs[0]["wishlist_checked"] == 0

    def test_a_run_history_that_is_not_a_list_is_ignored(self, tmp_config):
        put("track_state.json", {"run_history": "oops"})
        assert load_track_state().runs == []


# ---------------------------------------------------------------------------
# Caches and renaming
# ---------------------------------------------------------------------------


class TestCachesAndRenames:
    def test_caches_are_dropped_not_imported(self, tmp_config):
        put("taste_cache.json", {"version": 2, "built_at": "2026-01-01T00:00:00"})
        put(
            "categories_cache.us.json",
            {"ts": 1, "categories": [{"id": "1", "name": "A"}]},
        )
        put("categories_cache.json", {"ts": 1, "categories": []})

        from audible_deals import cache_store

        db.state()  # the first use of state.db is what runs the import

        assert cache_store.get("taste", "profile") is None
        assert cache_store.get("categories", "us") is None
        assert {
            "taste_cache.json.migrated",
            "categories_cache.us.json.migrated",
            "categories_cache.json.migrated",
        } <= names(tmp_config)

    def test_every_source_is_renamed_never_deleted(self, tmp_config):
        sources = {
            "seen_asins.json": '["A1"]',
            "dismissed_asins.json": '["D1"]',
            "refresh_eligibility.json": '{"version": 1, "marketplaces": {}}',
            "monitor_state.json": '{"version": 1, "monitors": {}}',
            "last_results.json": '{"title": "t", "results": []}',
            "notify_state.json": "{}",
            "track_state.json": "{}",
            "taste_cache.json": "{}",
        }
        for name, content in sources.items():
            put(name, content)
        history_file("B00RENAME1", {"us": [entry("2026-01-01", 1.0)]})

        db.state()

        for name, content in sources.items():
            assert not (tmp_config / name).exists()
            assert (tmp_config / f"{name}.migrated").read_text() == content
        assert (tmp_config / "history.migrated" / "B00RENAME1.json").exists()

    def test_an_existing_migrated_name_is_never_overwritten(self, tmp_config):
        put("seen_asins.json.migrated", "earlier backup")
        put("seen_asins.json", ["A1"])

        db.state()

        assert (tmp_config / "seen_asins.json.migrated").read_text() == "earlier backup"
        assert (tmp_config / "seen_asins.json.migrated.1").read_text() == '["A1"]'

    def test_files_that_appear_after_the_import_are_left_alone(self, tmp_config):
        put("seen_asins.json", ["A1"])
        db.state()
        db.close_all()
        late = put("seen_asins.json", ["LATE"])

        assert load_seen_asins() == {"A1"}
        assert late.read_text() == '["LATE"]'

    def test_the_announcement_is_one_line_and_mentions_the_backup(
        self, tmp_config, caplog
    ):
        put("seen_asins.json", ["A1", "A2"])
        history_file("B00NOTE001", {"us": [entry("2026-01-01", 1.0)]})

        with caplog.at_level(NOTE, logger="audible_deals.json_import"):
            db.state()

        records = [r for r in caplog.records if "Moved your saved data" in r.message]
        assert len(records) == 1
        # A note, not a warning: nothing is wrong, the user is just told.
        assert records[0].levelno == NOTE
        assert "1 price entries for 1 titles, 2 seen" in records[0].message
        assert "*.migrated" in records[0].message

    def test_the_summary_is_recorded_for_doctor(self, tmp_config):
        put("seen_asins.json", ["A1"])
        history_file(
            "B00SUM0001", {"us": [entry("2026-01-01", 1.0), entry("2026-01-02", 2.0)]}
        )

        result = summary()

        assert result["counts"]["history_entries"] == 2
        assert result["counts"]["history_asins"] == 1
        assert result["counts"]["seen"] == 1
        assert sorted(result["sources"]) == ["history", "seen_asins.json"]
        assert result["renames_pending"] == []


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailureHandling:
    def seed(self, tmp_config):
        put("seen_asins.json", ["A1", "A2"])
        put("track_state.json", {"run_history": [{"at": "2026-01-01T00:00:00"}]})
        history_file("B00FAIL001", {"us": [entry("2026-01-01", 1.0)]})

    def test_a_failure_while_writing_rolls_everything_back(
        self, tmp_config, monkeypatch
    ):
        self.seed(tmp_config)
        real = json_import._write

        def explode_after_writing(conn, data):
            real(conn, data)
            raise RuntimeError("disk full")

        monkeypatch.setattr(json_import, "_write", explode_after_writing)

        with pytest.raises(db.StateDatabaseError, match="disk full") as failure:
            db.state()
        assert "Your files were not changed" in str(failure.value)
        db.close_all()

        # sources untouched, nothing renamed, nothing half-imported
        assert (tmp_config / "seen_asins.json").exists()
        assert (tmp_config / "track_state.json").exists()
        assert (tmp_config / "history" / "B00FAIL001.json").exists()
        assert not [n for n in names(tmp_config) if n.endswith(".migrated")]
        import sqlite3

        raw = sqlite3.connect(paths.state_db_file())
        try:
            for table in ("seen", "price_history", "track_runs", "products"):
                assert raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            assert raw.execute("SELECT COUNT(*) FROM meta").fetchone()[0] == 0
        finally:
            raw.close()

        # ...and the next run retries from scratch and succeeds
        monkeypatch.setattr(json_import, "_write", real)
        assert load_seen_asins() == {"A1", "A2"}
        assert len(load_price_history("B00FAIL001")) == 1
        assert len(load_track_state().runs) == 1
        assert (tmp_config / "seen_asins.json.migrated").exists()

    def test_an_unreadable_source_aborts_without_touching_anything(
        self, tmp_config, monkeypatch
    ):
        self.seed(tmp_config)
        real = json_import.read_text

        def deny(path):
            if Path(path).name == "seen_asins.json":
                raise PermissionError(13, "Permission denied")
            return real(path)

        monkeypatch.setattr(json_import, "read_text", deny)

        with pytest.raises(db.StateDatabaseError, match="Cannot migrate"):
            db.state()
        db.close_all()

        assert (tmp_config / "seen_asins.json").exists()
        assert not [n for n in names(tmp_config) if n.endswith(".migrated")]

        monkeypatch.setattr(json_import, "read_text", real)
        assert load_seen_asins() == {"A1", "A2"}

    def test_a_rename_failure_after_the_commit_is_retried_without_reimporting(
        self, tmp_config, monkeypatch, caplog
    ):
        self.seed(tmp_config)
        real_rename = os.rename

        def flaky(source, target):
            if Path(source).name == "seen_asins.json":
                raise PermissionError(13, "in use")
            return real_rename(source, target)

        monkeypatch.setattr(json_import.os, "rename", flaky)
        with caplog.at_level(logging.WARNING):
            assert load_seen_asins() == {"A1", "A2"}

        assert (tmp_config / "seen_asins.json").exists()
        assert (tmp_config / "track_state.json.migrated").exists()
        assert summary()["renames_pending"] == ["seen_asins.json"]
        assert "could not be renamed yet" in caplog.text
        runs_before = len(load_track_state().runs)

        db.close_all()
        monkeypatch.setattr(json_import.os, "rename", real_rename)
        assert load_seen_asins() == {"A1", "A2"}

        assert not (tmp_config / "seen_asins.json").exists()
        assert (tmp_config / "seen_asins.json.migrated").exists()
        assert summary()["renames_pending"] == []
        assert len(load_track_state().runs) == runs_before  # imported once only
        assert db.meta_get(db.state(), json_import.META_RENAMES) is None

    def test_a_pending_rename_does_not_trigger_another_import_each_run(
        self, tmp_config, monkeypatch
    ):
        self.seed(tmp_config)
        real_rename = os.rename
        monkeypatch.setattr(
            json_import.os,
            "rename",
            lambda s, t: (_ for _ in ()).throw(PermissionError(13, "locked")),
        )
        db.state()
        db.close_all()

        for _ in range(2):
            assert load_seen_asins() == {"A1", "A2"}
            db.close_all()

        monkeypatch.setattr(json_import.os, "rename", real_rename)
        db.state()
        assert load_track_state().runs  # one run, not duplicated by the retries
        assert len(load_track_state().runs) == 1


# ---------------------------------------------------------------------------
# Encodings
# ---------------------------------------------------------------------------


class TestEncodings:
    def test_a_file_in_the_old_locale_encoding_is_read_via_the_fallback(
        self, tmp_config, monkeypatch
    ):
        monkeypatch.setattr(
            "audible_deals.storage.locale.getpreferredencoding", lambda do: "cp1252"
        )
        text = json.dumps({"title": "Café", "results": []}, ensure_ascii=False)
        put("last_results.json", text.encode("cp1252"))

        from tests.conftest import raw_session

        assert raw_session() == text

    def test_utf8_is_read_as_utf8(self, tmp_path):
        path = tmp_path / "x.json"
        path.write_bytes("café 🎧".encode())
        assert read_text(path) == "café 🎧"

    def test_invalid_bytes_still_fail_when_the_locale_is_utf8(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "audible_deals.storage.locale.getpreferredencoding", lambda do: "UTF-8"
        )
        path = tmp_path / "x.json"
        path.write_bytes(b"\xff\xfe\xfa not utf8")
        with pytest.raises(UnicodeDecodeError):
            read_text(path)


# ---------------------------------------------------------------------------
# Exactly once under concurrency
# ---------------------------------------------------------------------------

RACER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from audible_deals.logging_setup import configure_logging
    configure_logging(0)
    from audible_deals import db
    go = Path(sys.argv[1])
    while not go.exists():
        time.sleep(0.0005)
    db.state()
    print("opened")
    """
)


class TestConcurrentImport:
    def test_racing_processes_import_exactly_once(self, tmp_path):
        home = tmp_path / "race-home"
        home.mkdir()
        (home / "seen_asins.json").write_text(json.dumps([f"S{i}" for i in range(50)]))
        (home / "track_state.json").write_text(
            json.dumps(
                {
                    "run_history": [
                        {"at": f"2026-01-{n:02d}T00:00:00", "hits": n}
                        for n in (3, 2, 1)
                    ]
                }
            )
        )
        (home / "history").mkdir()
        for i in range(120):
            (home / "history" / f"B00RACE{i:03d}.json").write_text(
                json.dumps({"marketplaces": {"us": [entry("2026-01-01", 1.0 + i)]}})
            )
        go = tmp_path / "go"
        env = {**os.environ, "DEALS_HOME": str(home)}
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", RACER, str(go)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(5)
        ]
        go.write_text("go")
        results = [proc.communicate(timeout=120) for proc in procs]

        assert [proc.returncode for proc in procs] == [0] * 5, results
        announcements = sum(err.count("Moved your saved data") for _, err in results)
        assert announcements == 1, results

        import sqlite3

        raw = sqlite3.connect(home / "state.db")
        try:
            assert raw.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 50
            assert (
                raw.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 120
            )
            # track_runs rows are plain INSERTs, so a second import would double them
            assert raw.execute("SELECT COUNT(*) FROM track_runs").fetchone()[0] == 3
            marker = raw.execute(
                "SELECT value FROM meta WHERE key = 'json_import'"
            ).fetchone()
            assert marker is not None
        finally:
            raw.close()

        leftover = sorted(p.name for p in home.iterdir())
        assert "seen_asins.json.migrated" in leftover
        assert "seen_asins.json.migrated.1" not in leftover
        assert "history.migrated" in leftover and "history.migrated.1" not in leftover
        assert "seen_asins.json" not in leftover and "history" not in leftover


# ---------------------------------------------------------------------------
# Hostile content must never make the app unusable
# ---------------------------------------------------------------------------


def _random_json(rng, depth: int = 0):
    kinds = ["null", "bool", "int", "float", "nan", "str", "list", "dict"]
    kind = rng.choice(kinds if depth < 3 else kinds[:6])
    if kind == "null":
        return None
    if kind == "bool":
        return rng.random() < 0.5
    if kind == "int":
        return rng.choice([0, 1, -1, 7, 10**30, 2026])
    if kind == "float":
        return rng.choice([0.0, 3.99, -2.5, 1e300])
    if kind == "nan":
        return rng.choice([float("nan"), float("inf"), float("-inf")])
    if kind == "str":
        return rng.choice(
            [
                "",
                "x",
                "2026-01-02",
                "B00ABC1234",
                "us",
                "uk",
                "é\u0000",
                "\ud800",
                "\udfff x",
            ]
        )
    if kind == "list":
        return [_random_json(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    keys = [
        "version",
        "monitors",
        "products",
        "price",
        "date",
        "title",
        "us",
        "uk",
        "marketplaces",
        "run_history",
        "last_run",
        "install",
        "initialized",
        "refresh_cursors",
        "asin",
        "surfaced_on",
        "B00ABC1234",
        "at",
        "results",
    ]
    return {
        rng.choice(keys): _random_json(rng, depth + 1) for _ in range(rng.randint(0, 5))
    }


@pytest.mark.parametrize("seed", range(40))
def test_arbitrary_json_in_every_file_is_survivable(tmp_config, seed):
    import random

    rng = random.Random(seed)
    for name in (
        "seen_asins.json",
        "dismissed_asins.json",
        "refresh_eligibility.json",
        "monitor_state.json",
        "last_results.json",
        "notify_state.json",
        "track_state.json",
    ):
        (tmp_config / name).write_text(json.dumps(_random_json(rng)), encoding="utf-8")
    (tmp_config / "history").mkdir()
    for i in range(6):
        (tmp_config / "history" / f"B00FUZZ{i:03d}.json").write_text(
            json.dumps(_random_json(rng)), encoding="utf-8"
        )

    # Whatever the files held, the database opens and the readers work.
    db.state()
    load_seen_asins()
    load_dismissed_asins()
    load_refresh_eligibility()
    load_all_price_histories()
    load_notify_state()
    load_track_state()
    monitor_summaries()
    assert json_import.summary(db.state()) is not None
    assert not (tmp_config / "seen_asins.json").exists()  # renamed, not deleted
    assert (tmp_config / "seen_asins.json.migrated").exists()


def test_lone_surrogates_in_old_files_do_not_block_the_import(tmp_config):
    put(
        "history/B00SURR001.json",
        '{"marketplaces": {"us": [{"date": "2026-01-01", "price": 3.0, '
        '"title": "bad \\ud800 title"}]}}',
    )
    put("seen_asins.json", '["B00SEEN\\ud800"]')
    put(
        "monitor_state.json",
        '{"version": 1, "monitors": {"m\\ud800": {"initialized": true, "products": '
        '{"A\\ud800": {"title": "\\ud800"}}}}}',
    )

    assert load_price_history("B00SURR001")[0]["price"] == 3.0
    assert len(load_seen_asins()) == 1
    assert len(monitor_summaries()) == 1


def test_nan_in_old_monitor_products_and_install_records_is_kept(tmp_config):
    (tmp_config / "monitor_state.json").write_text(
        '{"version": 1, "monitors": {"m": {"initialized": true, "products": '
        '{"A1": {"asin": "A1", "price": NaN, "title": "t"}}}}}'
    )
    (tmp_config / "track_state.json").write_text(
        '{"install": {"every": "6h", "weird": Infinity}}'
    )

    snapshot = load_monitor_snapshot("m")

    assert snapshot.initialized is True
    assert set(snapshot.products) == {"A1"}
    assert load_install_record()["every"] == "6h"
