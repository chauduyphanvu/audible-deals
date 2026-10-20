"""SQLite plumbing: connections, transactions, migrations, corruption, concurrency."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import textwrap
import threading

import pytest
from click.testing import CliRunner

from audible_deals import db, paths
from audible_deals.cli import cli


def _pragma(conn, name):
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


class TestOpening:
    def test_state_database_is_created_with_the_expected_settings(self, tmp_config):
        conn = db.state()

        assert paths.state_db_file().exists()
        assert _pragma(conn, "journal_mode") == "wal"
        assert _pragma(conn, "foreign_keys") == 1
        assert _pragma(conn, "busy_timeout") == db.BUSY_TIMEOUT_MS == 5000
        assert _pragma(conn, "user_version") == db.schema_version("state") >= 1

    def test_cache_database_is_separate_and_disposable(self, tmp_config):
        conn = db.cache()

        assert paths.cache_db_file().exists()
        assert paths.cache_db_file() != paths.state_db_file()
        assert _pragma(conn, "user_version") == db.schema_version("cache")
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "cache_entries" in tables
        assert "price_history" not in tables

    def test_the_connection_is_shared_within_a_process(self, tmp_config):
        assert db.state() is db.state()
        assert db.cache() is db.cache()

    def test_a_different_data_directory_opens_a_different_database(
        self, tmp_config, tmp_path_factory, monkeypatch
    ):
        first = db.state()
        other = tmp_path_factory.mktemp("other-home")
        monkeypatch.setenv("DEALS_HOME", str(other))

        second = db.state()

        assert second is not first
        assert (other / "state.db").exists()
        with pytest.raises(Exception, match="closed"):
            first.execute("SELECT 1")

    def test_close_all_lets_the_next_call_reopen(self, tmp_config):
        first = db.state()
        db.close_all()
        assert db.state() is not first

    def test_missing_data_directory_is_created(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEALS_HOME", str(tmp_path / "a" / "b"))
        db.state()
        assert (tmp_path / "a" / "b" / "state.db").exists()

    def test_the_expected_tables_exist(self, tmp_config):
        tables = {
            row[0]
            for row in db.state().execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "meta",
            "price_history",
            "products",
            "seen",
            "dismissed",
            "refresh_eligibility",
            "monitor_state",
            "monitor_products",
            "result_sessions",
            "notify_state",
            "refresh_cursors",
            "track_runs",
            "track_state",
        } <= tables

    def test_importing_the_module_does_not_load_sqlite(self):
        code = "import sys, audible_deals.db; print('sqlite3' in sys.modules)"
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        assert out.stdout.strip() == "False"

    def test_help_and_version_never_touch_the_database(self, tmp_path):
        code = textwrap.dedent(
            """
            import sys
            from click.testing import CliRunner
            from audible_deals.cli import cli
            for args in (["--version"], ["--help"], ["find", "--help"]):
                CliRunner().invoke(cli, args)
            print("sqlite3" in sys.modules)
            """
        )
        env = {"DEALS_HOME": str(tmp_path / "untouched"), "PATH": ""}
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env={**env, "SYSTEMROOT": ""},
        )
        assert out.stdout.strip().splitlines()[-1] == "False"
        assert not (tmp_path / "untouched").exists()


class TestTransactions:
    def test_commit_and_rollback(self, tmp_config):
        conn = db.state()
        with db.transaction(conn):
            conn.execute("INSERT INTO seen (asin) VALUES ('KEEP')")
        with pytest.raises(RuntimeError), db.transaction(conn):
            conn.execute("INSERT INTO seen (asin) VALUES ('DROP')")
            raise RuntimeError("boom")

        assert [row[0] for row in conn.execute("SELECT asin FROM seen")] == ["KEEP"]
        assert not conn.in_transaction

    def test_nested_use_joins_the_outer_transaction(self, tmp_config):
        conn = db.state()
        with pytest.raises(RuntimeError), db.transaction(conn):
            conn.execute("INSERT INTO seen (asin) VALUES ('A')")
            with db.transaction(conn):
                conn.execute("INSERT INTO seen (asin) VALUES ('B')")
            raise RuntimeError("outer fails")

        assert conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0

    def test_writers_are_serialized_between_connections(self, tmp_config):
        import sqlite3

        conn = db.state()
        other = sqlite3.connect(
            paths.state_db_file(), timeout=0.1, isolation_level=None
        )
        try:
            with db.transaction(conn):
                conn.execute("INSERT INTO seen (asin) VALUES ('A')")
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    other.execute("BEGIN IMMEDIATE")
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
        finally:
            other.close()

    def test_readers_see_a_consistent_snapshot(self, tmp_config):
        conn = db.state()
        with db.transaction(conn):
            conn.execute("INSERT INTO seen (asin) VALUES ('A')")
        with db.transaction(conn, write=False):
            before = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
            assert before == 1

    def test_a_busy_database_raises_a_clear_error(self, tmp_config):
        import sqlite3

        conn = db.state()
        blocker = sqlite3.connect(paths.state_db_file(), isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            conn.execute("PRAGMA busy_timeout = 50")
            with pytest.raises(db.StateDatabaseError, match="busy"):
                with db.transaction(conn):
                    pass  # pragma: no cover
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()

    def test_threads_can_share_the_connection(self, tmp_config):
        conn = db.state()
        errors = []

        def add(prefix):
            try:
                for i in range(25):
                    with db.transaction(conn):
                        conn.execute(
                            "INSERT INTO seen (asin) VALUES (?)", (f"{prefix}{i}",)
                        )
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=add, args=(name,)) for name in "ABCD"]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not errors
        assert conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 100

    def test_foreign_keys_cascade_monitor_products(self, tmp_config):
        conn = db.state()
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO monitor_state (name, initialized) VALUES ('m', 1)"
            )
            conn.execute(
                "INSERT INTO monitor_products (monitor, asin, price, data) "
                "VALUES ('m', 'A1', 1.0, '{}')"
            )
            conn.execute("DELETE FROM monitor_state WHERE name = 'm'")
        assert conn.execute("SELECT COUNT(*) FROM monitor_products").fetchone()[0] == 0

    def test_foreign_keys_reject_orphan_products(self, tmp_config):
        import sqlite3

        conn = db.state()
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction(conn):
                conn.execute(
                    "INSERT INTO monitor_products (monitor, asin, price, data) "
                    "VALUES ('nobody', 'A1', 1.0, '{}')"
                )

    def test_chunked_splits_without_losing_items(self):
        assert [list(c) for c in db.chunked(list(range(5)), 2)] == [[0, 1], [2, 3], [4]]
        assert list(db.chunked([], 2)) == []


class TestMigrations:
    def test_a_later_schema_step_is_applied_to_an_existing_database(
        self, tmp_config, monkeypatch
    ):
        conn = db.state()
        with db.transaction(conn):
            conn.execute("INSERT INTO seen (asin) VALUES ('KEEP')")
        db.close_all()
        monkeypatch.setitem(
            db._MIGRATIONS,
            "state",
            (
                *db._STATE_MIGRATIONS,
                ("ALTER TABLE seen ADD COLUMN note TEXT",),
            ),
        )

        conn = db.state()

        assert _pragma(conn, "user_version") == len(db._STATE_MIGRATIONS) + 1
        assert conn.execute("SELECT asin, note FROM seen").fetchall() == [
            ("KEEP", None)
        ]

    def test_a_current_database_is_not_migrated_again(self, tmp_config, monkeypatch):
        db.state()
        db.close_all()
        calls = []
        monkeypatch.setattr(db, "_apply_schema", lambda *args: calls.append(args))

        db.state()

        assert calls == []

    def test_a_failed_step_leaves_the_previous_version(self, tmp_config, monkeypatch):
        import sqlite3

        db.state()
        db.close_all()
        with monkeypatch.context() as patch:
            patch.setitem(
                db._MIGRATIONS,
                "state",
                (
                    *db._STATE_MIGRATIONS,
                    ("CREATE TABLE half_done (x)", "THIS IS NOT SQL"),
                ),
            )
            with pytest.raises(db.StateDatabaseError):
                db.state()

        conn = sqlite3.connect(paths.state_db_file())
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == len(
                db._STATE_MIGRATIONS
            )
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            assert "half_done" not in names
        finally:
            conn.close()

    def test_a_database_from_a_newer_release_is_refused_untouched(self, tmp_config):
        import sqlite3

        db.state()
        db.close_all()
        raw = sqlite3.connect(paths.state_db_file(), isolation_level=None)
        raw.execute("INSERT INTO seen (asin) VALUES ('FUTURE')")
        raw.execute(f"PRAGMA user_version = {db.schema_version('state') + 5}")
        raw.close()

        with pytest.raises(db.StateDatabaseError, match="newer audible-deals"):
            db.state()

        assert not list(tmp_config.glob("state.db.corrupt-*"))
        raw = sqlite3.connect(paths.state_db_file())
        try:
            assert raw.execute("SELECT asin FROM seen").fetchall() == [("FUTURE",)]
        finally:
            raw.close()

    def test_meta_helpers_round_trip(self, tmp_config):
        conn = db.state()
        with db.transaction(conn):
            db.meta_set(conn, "k", "v1")
            db.meta_set(conn, "k", "v2")
        assert db.meta_get(conn, "k") == "v2"
        assert db.meta_get(conn, "absent") is None


class TestCorruption:
    def test_an_unreadable_state_database_is_moved_aside_and_recreated(
        self, tmp_config, caplog
    ):
        paths.state_db_file().write_bytes(b"definitely not sqlite" * 50)

        with caplog.at_level(logging.WARNING, logger="audible_deals.db"):
            conn = db.state()

        assert conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0
        moved = list(tmp_config.glob("state.db.corrupt-*"))
        assert len(moved) == 1
        assert moved[0].read_bytes() == b"definitely not sqlite" * 50
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert moved[0].name in warnings[0]

    def test_stale_wal_files_do_not_poison_the_fresh_database(self, tmp_config):
        paths.state_db_file().write_bytes(b"x" * 4096)
        (tmp_config / "state.db-wal").write_bytes(b"wal")
        (tmp_config / "state.db-shm").write_bytes(b"shm")

        conn = db.state()

        assert db.quick_check(conn) == ["ok"]
        with db.transaction(conn):
            conn.execute("INSERT INTO seen (asin) VALUES ('A')")
        assert conn.execute("SELECT asin FROM seen").fetchall() == [("A",)]
        assert len(list(tmp_config.glob("state.db.corrupt-*"))) >= 1

    def test_two_quarantines_never_overwrite_each_other(self, tmp_config):
        for payload in (b"first" * 800, b"second" * 800):
            db.close_all()
            paths.state_db_file().write_bytes(payload)
            db.state()

        payloads = sorted(p.read_bytes() for p in tmp_config.glob("state.db.corrupt-*"))
        assert payloads == [b"first" * 800, b"second" * 800]

    def test_an_unreadable_cache_database_is_simply_recreated(self, tmp_config, caplog):
        paths.cache_db_file().write_bytes(b"garbage" * 500)

        with caplog.at_level(logging.WARNING, logger="audible_deals.db"):
            conn = db.cache()

        assert conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0] == 0
        assert not list(tmp_config.glob("cache.db.corrupt-*"))
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_quick_check_reports_a_healthy_database(self, tmp_config):
        assert db.quick_check(db.state()) == ["ok"]

    def test_quick_check_reports_damage(self, tmp_config):
        conn = db.state()
        with db.transaction(conn):
            for i in range(200):
                conn.execute("INSERT INTO seen (asin) VALUES (?)", (f"ASIN{i:05d}",))
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close_all()
        data = bytearray(paths.state_db_file().read_bytes())
        # Zero out the interior of a data page to break the b-tree.
        for offset in range(8192, min(len(data), 12288)):
            data[offset] = 0xFF
        paths.state_db_file().write_bytes(bytes(data))
        import sqlite3

        raw = sqlite3.connect(paths.state_db_file())
        try:
            try:
                result = [row[0] for row in raw.execute("PRAGMA quick_check")]
            except sqlite3.DatabaseError as exc:
                result = [str(exc)]
        finally:
            raw.close()
        assert result != ["ok"]

    def test_file_size_includes_wal_sidecars(self, tmp_config):
        conn = db.state()
        with db.transaction(conn):
            conn.execute("INSERT INTO seen (asin) VALUES ('A')")
        assert (
            db.file_size(paths.state_db_file()) >= paths.state_db_file().stat().st_size
        )


class TestConcurrentOpens:
    SCRIPT = textwrap.dedent(
        """
        import sys
        from audible_deals import db
        conn = db.state()
        with db.transaction(conn):
            conn.execute("INSERT OR IGNORE INTO seen (asin) VALUES (?)", (sys.argv[1],))
        print("ok")
        """
    )

    def test_processes_racing_to_create_the_database_all_succeed(self, tmp_path):
        env = {"DEALS_HOME": str(tmp_path / "race"), "PATH": ""}
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", self.SCRIPT, f"P{i}"],
                env={**env, "SYSTEMROOT": ""},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for i in range(6)
        ]
        results = [proc.communicate(timeout=60) for proc in procs]

        assert [proc.returncode for proc in procs] == [0] * 6, results
        import sqlite3

        raw = sqlite3.connect(tmp_path / "race" / "state.db")
        try:
            assert raw.execute("PRAGMA user_version").fetchone()[
                0
            ] == db.schema_version("state")
            assert raw.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 6
        finally:
            raw.close()


class TestFailuresAreReportedCleanly:
    def test_a_database_failure_is_reported_without_a_traceback(
        self, tmp_config, monkeypatch
    ):
        import sqlite3

        def broken():
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr("audible_deals.price_history.db.state", broken)
        result = CliRunner().invoke(cli, ["history", "B00BROKEN1"])

        assert result.exit_code != 0
        assert "Local database error" in result.output
        assert "deals doctor" in result.output
        assert "Traceback" not in result.output


class TestDoctor:
    def test_doctor_reports_a_database_it_cannot_use_without_crashing(self, tmp_config):
        import sqlite3

        from audible_deals import db

        db.state()
        db.close_all()
        raw = sqlite3.connect(paths.state_db_file(), isolation_level=None)
        raw.execute(f"PRAGMA user_version = {db.schema_version('state') + 3}")
        raw.close()
        (tmp_config / "monitors.json").write_text(
            json.dumps({"m": {"mode": "find", "settings": {"pages": 1}}})
        )

        result = CliRunner().invoke(cli, ["doctor"])
        output = " ".join(result.output.split())

        assert result.exit_code == 1
        assert "State database" in output and "FAIL" in output
        assert "newer audible-deals" in output
        assert "Traceback" not in result.output
        assert "Saved-search monitors" in output

    def test_doctor_reports_a_healthy_database(self, tmp_config):
        result = CliRunner().invoke(cli, ["doctor"])
        output = " ".join(result.output.split())

        assert "State database" in output
        assert "integrity check ok" in output
        assert "JSON migration" in output and "Nothing to migrate" in output
        assert "Cache database" in output
