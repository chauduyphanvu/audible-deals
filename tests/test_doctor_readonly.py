"""`deals doctor` looks; it never creates the data directory or a database."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from audible_deals import db, paths
from audible_deals.cli import cli


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A data directory that does not exist yet (a machine that never ran deals)."""
    directory = tmp_path / "never-used"
    monkeypatch.setenv("DEALS_HOME", str(directory))
    return directory


def doctor_rows(*args):
    result = CliRunner().invoke(cli, ["doctor", "--json", *args])
    payload = json.loads(result.stdout)
    return result, {check["name"]: check for check in payload["checks"]}


class TestAFreshMachine:
    def test_nothing_is_created(self, fresh):
        text = CliRunner().invoke(cli, ["doctor"])
        data = CliRunner().invoke(cli, ["doctor", "--json"])

        assert text.exit_code in (0, 1) and data.exit_code in (0, 1)
        assert not fresh.exists()  # not even the directory

    def test_the_databases_are_reported_as_not_created_yet(self, fresh):
        _result, rows = doctor_rows()

        assert rows["Config directory"]["status"] == "warn"
        assert "will be created" in rows["Config directory"]["detail"]
        for name in ("State database", "Cache database"):
            assert rows[name]["status"] == "pass"
            assert "not created yet" in rows[name]["detail"]
            assert "bytes" not in rows[name]["detail"]
        assert rows["JSON migration"]["detail"] == "Nothing to migrate"
        assert not fresh.exists()

    def test_the_checks_that_read_state_see_it_as_empty(self, fresh):
        _result, rows = doctor_rows()

        assert rows["Price history"]["detail"] == "Not recorded yet (optional)"
        assert rows["Seen-ASINs state"]["detail"] == "No entries"
        assert rows["Dismissed-ASINs state"]["detail"] == "No entries"
        assert rows["Background tracking"]["detail"].startswith("Not installed")

    def test_old_json_files_are_not_imported_or_renamed_by_a_check(self, fresh):
        fresh.mkdir()
        (fresh / "seen_asins.json").write_text('["B0SEEN0001"]')
        (fresh / "history").mkdir()

        _result, rows = doctor_rows()

        assert (fresh / "seen_asins.json").exists()
        assert not (fresh / "state.db").exists()
        assert not list(fresh.glob("*.migrated"))
        assert "seen_asins.json" in rows["State database"]["detail"]
        assert "import these old files" in rows["State database"]["detail"]

    def test_the_next_command_still_creates_and_migrates_as_usual(self, fresh):
        fresh.mkdir()
        (fresh / "seen_asins.json").write_text('["B0SEEN0001"]')
        doctor_rows()

        from audible_deals.results_cache import load_seen_asins

        assert load_seen_asins() == {"B0SEEN0001"}
        assert (fresh / "state.db").exists()
        assert (fresh / "seen_asins.json.migrated").exists()
        conn = db.state()
        (_seq, _name, file) = conn.execute("PRAGMA database_list").fetchone()
        assert file.endswith("state.db")  # a real file, not the throwaway copy


class TestADataDirectoryThatExists:
    def test_the_databases_are_checked_as_before(self, tmp_config):
        db.state()
        db.cache()
        db.close_all()

        _result, rows = doctor_rows()

        assert "integrity check ok" in rows["State database"]["detail"]
        assert "bytes" in rows["Cache database"]["detail"]

    def test_a_directory_without_databases_gets_none_either(self, tmp_config):
        assert not paths.state_db_file().exists()

        doctor_rows()

        assert not paths.state_db_file().exists()
        assert not paths.cache_db_file().exists()
        assert not (tmp_config / ".migrate.lock").exists()


class TestTheMode:
    def test_missing_databases_are_empty_and_in_memory(self, tmp_config):
        with db.without_creating():
            conn = db.state()
            assert conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0
            conn.execute("INSERT INTO seen (asin) VALUES ('X')")  # goes nowhere
            cache = db.cache()
            assert (
                cache.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0] == 0
            )

        assert not paths.state_db_file().exists()
        assert not paths.cache_db_file().exists()
        assert db.state().execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0

    def test_existing_databases_are_opened_normally(self, tmp_config):
        conn = db.state()
        with db.transaction(conn):
            conn.execute("INSERT INTO seen (asin) VALUES ('KEPT')")
        db.close_all()

        with db.without_creating():
            rows = db.state().execute("SELECT asin FROM seen").fetchall()

        assert rows == [("KEPT",)]
