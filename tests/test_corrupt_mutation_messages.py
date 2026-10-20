"""A command that would change a corrupt file says so once, with the error.

The read-path warning ("... is corrupt, ignoring") describes carrying on with
defaults; before a write that is refused it only repeats the error that
follows, so mutation commands do not print it.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from audible_deals import paths
from audible_deals.cli import cli

MONITOR = '{"m1": {"name": "m1", "mode": "search", "query": "q"}}'

# (file that is corrupt, command line)
MUTATIONS = [
    ("config.json", ["config", "set", "max-price", "5"]),
    ("config.json", ["config", "reset", "max-price"]),
    ("config.json", ["config", "reset", "-y"]),
    ("config.json", ["--locale", "uk", "config", "set", "max-price", "5"]),
    ("config.json", ["config", "set", "secret-storage", "file", "-y"]),
    ("config.json", ["track", "install", "--webhook", "https://example.com/hook"]),
    ("profiles.json", ["profile", "save", "p1", "--max-price", "3"]),
    ("profiles.json", ["profile", "delete", "p1"]),
    ("monitors.json", ["monitor", "add", "m1", "--query", "x"]),
    ("monitors.json", ["monitor", "remove", "m1", "-y"]),
    ("monitors.json", ["monitor", "pause", "m1"]),
    ("monitors.json", ["monitor", "resume", "m1"]),
]


@pytest.fixture(autouse=True)
def _offline_webhook_check(monkeypatch):
    monkeypatch.setattr(
        "audible_deals.validation.validate_webhook_url", lambda *a, **k: None
    )


@pytest.mark.parametrize(
    ("filename", "command"), MUTATIONS, ids=[" ".join(m[1][:3]) for m in MUTATIONS]
)
def test_a_refused_update_prints_the_error_and_not_the_warning(
    tmp_config, filename, command
):
    (tmp_config / filename).write_text("{bad")
    before = (tmp_config / filename).read_text()

    result = CliRunner().invoke(cli, command)

    assert result.exit_code == 1, result.output
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1, lines
    assert lines[0].startswith("Error: Cannot update ")
    assert (tmp_config / filename).read_text() == before  # nothing was changed


@pytest.mark.parametrize(
    ("filename", "command"),
    [
        ("config.json", ["config", "list"]),
        ("config.json", ["config", "get", "max-price"]),
        ("config.json", ["track", "install", "--every", "2h"]),
        ("config.json", ["profile", "list"]),
        ("profiles.json", ["profile", "list"]),
        ("monitors.json", ["monitor", "list"]),
    ],
    ids=lambda value: " ".join(value) if isinstance(value, list) else value,
)
def test_commands_that_only_read_still_warn(tmp_config, monkeypatch, filename, command):
    import audible_deals.scheduler as scheduler

    monkeypatch.setattr(scheduler, "install", lambda *a, **k: "fake")
    (tmp_config / filename).write_text("{bad")

    result = CliRunner().invoke(cli, command)

    assert "Warning: " in result.stderr
    assert "is corrupt" in result.stderr


def test_the_error_names_the_file_and_what_to_do(tmp_config):
    paths.config_file().write_text("{bad")

    result = CliRunner().invoke(cli, ["config", "set", "max-price", "5"])

    assert str(paths.config_file()) in result.stderr
    assert "Fix the file or move it aside" in result.stderr
    assert "nothing was changed" in result.stderr


def test_a_command_that_updates_one_file_still_warns_about_a_corrupt_other_one(
    tmp_config,
):
    """Only the file about to be changed is covered by the error."""
    paths.config_file().write_text("{bad")
    paths.monitors_file().write_text(MONITOR)

    result = CliRunner().invoke(cli, ["monitor", "pause", "m1"])

    assert result.exit_code == 0, result.output
    assert "Warning: config at" in result.stderr  # config is read for this command
