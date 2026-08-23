"""Small full-routing smoke checks for the Click command tree."""

import subprocess
import sys

import click
from click.testing import CliRunner

from audible_deals.cli import cli
from tests.conftest import make_product


def test_expected_top_level_commands_are_registered_and_resolve():
    expected = {
        "categories",
        "compare",
        "completions",
        "config",
        "detail",
        "doctor",
        "find",
        "for-me",
        "history",
        "import-auth",
        "last",
        "library",
        "login",
        "monitor",
        "notify",
        "open",
        "profile",
        "recap",
        "search",
        "series",
        "track",
        "watch",
        "wishlist",
    }
    ctx = click.Context(cli)
    assert expected <= set(cli.list_commands(ctx))
    for name in expected:
        command = cli.get_command(ctx, name)
        assert command is not None
        assert command.name == name


def test_root_help_does_not_load_command_modules_or_audible_sdk():
    code = """
import sys
from click.testing import CliRunner
from audible_deals.cli import cli

result = CliRunner().invoke(cli, ["--help"])
assert result.exit_code == 0, result.output
assert "audible" not in sys.modules
assert not any(name.startswith("audible_deals.cli.") for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_root_help_smoke(tmp_config):
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Audible deal finder" in result.output


def test_representative_success_smoke(tmp_config, mock_client):
    mock_client.search_pages.return_value = iter(
        [([make_product(asin="SMOKE1", price=3.99)], 1, 1)]
    )
    result = CliRunner().invoke(
        cli, ["find", "--pages", "1", "--all-languages", "--quiet", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert "SMOKE1" in result.output


def test_representative_failure_smoke(tmp_config):
    result = CliRunner().invoke(cli, ["detail", "not/an/asin"])
    assert result.exit_code != 0
    assert "Invalid ASIN" in result.output
