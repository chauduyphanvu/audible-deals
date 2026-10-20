"""A corrupt user-owned file must never be silently replaced by a write.

Regression coverage for the "corrupt-file wipe": ``load_json_file`` used to
return an empty value for an unparseable ``profiles.json`` / ``config.json`` /
``monitors.json`` and the read-modify-write helpers then saved over it,
deleting everything the user had stored.
"""

from __future__ import annotations

import json
import logging

import pytest
from click.testing import CliRunner

from audible_deals import paths
from audible_deals.automation_models import MonitorDefinition
from audible_deals.cli import cli
from audible_deals.config_store import (
    config_transaction,
    load_monitors,
    load_profiles,
    profiles_transaction,
)
from audible_deals.monitor_service import (
    add_monitor,
    remove_monitor,
    set_monitor_enabled,
)
from audible_deals.results_cache import load_dismissed_asins, save_dismissed_asins
from audible_deals.storage import StateFileError, ensure_updatable, load_json_file

# The exact repro from the bug report: a truncated, hand-edited profiles file.
TRUNCATED = '{"a": {},'


@pytest.fixture
def lock_dir(tmp_config):
    """The isolated data directory, whose private run lock monitor mutators take."""
    return tmp_config


def _write(path, content):
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    return path.read_bytes()


def _definition(name="cheap"):
    return MonitorDefinition.from_dict(
        {
            "version": 1,
            "name": name,
            "enabled": True,
            "locale": "us",
            "mode": "find",
            "query": "",
            "settings": {"pages": 1, "limit": 0, "sort": "price"},
        }
    )


# ---------------------------------------------------------------------------
# CLI mutation paths refuse and leave the file byte-for-byte unchanged
# ---------------------------------------------------------------------------


def test_profile_save_refuses_to_overwrite_corrupt_profiles(tmp_config):
    before = _write(paths.profiles_file(), TRUNCATED)

    result = CliRunner().invoke(cli, ["profile", "save", "new", "--genre", "x"])

    assert result.exit_code == 1
    assert "Cannot update profiles" in result.output
    assert str(paths.profiles_file()) in result.output
    # The parse position is part of the message so the user can find the typo.
    assert "line 1, column 10" in result.output
    assert "Traceback" not in result.output
    assert paths.profiles_file().read_bytes() == before


def test_profile_delete_refuses_corrupt_profiles(tmp_config):
    before = _write(paths.profiles_file(), TRUNCATED)

    result = CliRunner().invoke(cli, ["profile", "delete", "a"])

    assert result.exit_code == 1
    assert "Cannot update profiles" in result.output
    assert paths.profiles_file().read_bytes() == before


@pytest.mark.parametrize(
    ("args", "stdin"),
    [
        (["config", "set", "max-price", "4"], None),
        (["config", "reset", "max-price"], None),
        (["config", "reset"], "y\n"),
    ],
)
def test_config_mutations_refuse_corrupt_config(tmp_config, args, stdin):
    before = _write(paths.config_file(), '{"max_price": 3,')

    result = CliRunner().invoke(cli, args, input=stdin)

    assert result.exit_code == 1
    assert "Cannot update config" in result.output
    assert str(paths.config_file()) in result.output
    assert "Traceback" not in result.output
    assert paths.config_file().read_bytes() == before


@pytest.mark.parametrize(
    "args",
    [
        ["monitor", "pause", "cheap"],
        ["monitor", "resume", "cheap"],
        ["monitor", "remove", "cheap", "--yes"],
    ],
)
def test_monitor_mutations_refuse_corrupt_monitors(lock_dir, args):
    before = _write(paths.monitors_file(), '{"cheap": ')

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 1
    assert "Cannot update monitors" in result.output
    assert str(paths.monitors_file()) in result.output
    assert paths.monitors_file().read_bytes() == before


def test_monitor_add_refuses_corrupt_monitors(lock_dir):
    from audible_deals.config_store import save_profiles

    save_profiles({"p": {"genre": "sci-fi"}})
    before = _write(paths.monitors_file(), "{oops")

    result = CliRunner().invoke(cli, ["monitor", "add", "new", "--profile", "p"])

    assert result.exit_code == 1
    assert "Cannot update monitors" in result.output
    assert paths.monitors_file().read_bytes() == before


# ---------------------------------------------------------------------------
# Service-level guarantees
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda: add_monitor(_definition("new")),
        lambda: remove_monitor("cheap"),
        lambda: set_monitor_enabled("cheap", False),
    ],
    ids=["add", "remove", "enable"],
)
def test_monitor_services_raise_state_file_error(lock_dir, mutate):
    before = _write(paths.monitors_file(), "[not, json")

    with pytest.raises(StateFileError) as excinfo:
        mutate()

    assert excinfo.value.path == paths.monitors_file()
    assert paths.monitors_file().read_bytes() == before


def test_transactions_do_not_run_the_body_or_save(tmp_config):
    before = _write(paths.profiles_file(), TRUNCATED)
    ran = False

    with pytest.raises(StateFileError, match="line 1, column 10"):
        with profiles_transaction() as profiles:
            ran = True
            profiles["new"] = {"genre": "x"}

    assert not ran
    assert paths.profiles_file().read_bytes() == before


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"\xff\xfe\x00bad", "corrupt"),  # not text / not JSON
        ("[1, 2, 3]", "expected a JSON object but found a JSON array"),
        ('"just a string"', "expected a JSON object but found a JSON string"),
        ("null", "expected a JSON object but found a JSON null"),
    ],
)
def test_config_transaction_refuses_unusable_shapes(tmp_config, content, expected):
    before = _write(paths.config_file(), content)

    with pytest.raises(StateFileError) as excinfo:
        with config_transaction() as config:
            config["x"] = 1

    assert expected in str(excinfo.value)
    assert paths.config_file().read_bytes() == before


def test_unreadable_file_is_refused_with_the_os_reason(tmp_config):
    # A directory where the file should be: exists, but cannot be read.
    paths.profiles_file().mkdir()

    with pytest.raises(StateFileError, match="could not be read"):
        with profiles_transaction():
            pass


def test_missing_and_blank_files_are_fine_to_write(tmp_config):
    # Nothing to lose: a missing file and a blank one are both writable.
    result = CliRunner().invoke(cli, ["profile", "save", "first", "--genre", "x"])
    assert result.exit_code == 0, result.output

    paths.profiles_file().write_text("  \n")
    result = CliRunner().invoke(cli, ["profile", "save", "second", "--genre", "y"])
    assert result.exit_code == 0, result.output
    assert list(load_profiles()) == ["second"]


def test_valid_files_still_round_trip(tmp_config):
    paths.profiles_file().write_text(json.dumps({"keep": {"genre": "sci-fi"}}))

    result = CliRunner().invoke(cli, ["profile", "save", "new", "--genre", "x"])

    assert result.exit_code == 0, result.output
    assert set(load_profiles()) == {"keep", "new"}


def test_dismissals_are_not_replaced_when_the_file_is_corrupt(tmp_config):
    before = _write(paths.dismissed_asins_file(), '["B000000001", ')

    with pytest.raises(StateFileError, match="dismissed ASINs"):
        save_dismissed_asins({"B000000002"})

    assert paths.dismissed_asins_file().read_bytes() == before


def test_ensure_updatable_accepts_matching_documents(tmp_path):
    path = tmp_path / "x.json"
    ensure_updatable(path, dict, "x")  # missing
    path.write_text("{}")
    ensure_updatable(path, dict, "x")
    path.write_text("[]")
    ensure_updatable(path, list, "x")


# ---------------------------------------------------------------------------
# Read-only loads: one warning line, treated as empty, no traceback unless -vv
# ---------------------------------------------------------------------------


def test_read_only_load_warns_once_without_traceback(tmp_path, caplog):
    path = tmp_path / "profiles.json"
    path.write_text(TRUNCATED)

    with caplog.at_level(logging.WARNING, logger="audible_deals.storage"):
        assert load_json_file(path, dict, "profiles") == {}
        assert load_json_file(path, dict, "profiles") == {}  # second read: silent

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    record = warnings[0]
    assert "\n" not in record.getMessage()
    assert "corrupt" in record.getMessage()
    assert str(path) in record.getMessage()
    assert "line 1, column 10" in record.getMessage()
    assert record.exc_info is None
    assert path.read_text() == TRUNCATED


def test_read_only_load_includes_traceback_when_debugging(tmp_path, caplog):
    path = tmp_path / "profiles.json"
    path.write_text(TRUNCATED)

    with caplog.at_level(logging.DEBUG, logger="audible_deals.storage"):
        load_json_file(path, dict, "profiles")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None


def test_cli_read_command_survives_corrupt_profiles_without_traceback(tmp_config):
    before = _write(paths.profiles_file(), TRUNCATED)

    result = CliRunner().invoke(cli, ["profile", "list"])

    assert result.exit_code == 0
    assert "No profiles saved" in result.output
    assert "Traceback" not in result.output
    assert result.output.count("is corrupt") == 1
    assert paths.profiles_file().read_bytes() == before


def test_cli_double_verbose_shows_the_traceback(tmp_config):
    _write(paths.profiles_file(), TRUNCATED)

    result = CliRunner().invoke(cli, ["-vv", "profile", "list"])

    assert result.exit_code == 0
    assert "Traceback (most recent call last)" in result.output


def test_read_only_monitor_listing_leaves_corrupt_file_alone(tmp_config):
    before = _write(paths.monitors_file(), "{bad")

    assert load_monitors() == {}
    result = CliRunner().invoke(cli, ["monitor", "list"])

    assert result.exit_code == 0
    assert paths.monitors_file().read_bytes() == before


def test_dismissed_asins_read_path_is_still_tolerant(tmp_config):
    _write(paths.dismissed_asins_file(), "[")

    assert load_dismissed_asins() == set()
