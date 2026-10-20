"""--color auto|always|never, before or after the subcommand."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from click.testing import CliRunner

from audible_deals.cli import cli
from audible_deals.presentation import terminal

ANSI = "\x1b["
DRY = ("find", "--max-price", "5", "--dry-run")


def run(*args):
    return CliRunner().invoke(cli, list(args))


@pytest.fixture
def plain_console(tmp_config):
    """The patched test console (not a terminal): no colors unless forced."""
    return terminal.console


class TestExplicitModes:
    def test_the_default_is_plain_when_output_is_not_a_terminal(self, plain_console):
        assert ANSI not in run(*DRY).output

    @pytest.mark.parametrize(
        "args",
        [
            ("--color", "always", *DRY),
            (*DRY, "--color", "always"),
            (*DRY, "--color=always"),
        ],
        ids=["before", "after", "after-equals"],
    )
    def test_always_colors_even_when_piped(self, plain_console, args):
        result = run(*args)

        assert result.exit_code == 0, result.output
        assert ANSI in result.output

    @pytest.mark.parametrize(
        "args",
        [("--color", "never", *DRY), (*DRY, "--color", "never")],
        ids=["before", "after"],
    )
    def test_never_removes_every_escape(self, plain_console, args):
        # Even from a console that would color (as a real terminal would).
        plain_console.no_color = False
        terminal.set_color_mode("always")
        assert ANSI in run(*DRY).output

        result = run(*args)

        assert result.exit_code == 0, result.output
        assert ANSI not in result.output
        assert "Dry run" in result.output

    def test_auto_leaves_what_the_environment_decided(self, plain_console):
        terminal.set_color_mode("always")
        assert ANSI in run(*DRY, "--color", "auto").output

    def test_the_later_value_wins(self, plain_console):
        result = run("--color", "never", *DRY, "--color", "always")
        assert ANSI in result.output
        result = run("--color", "always", *DRY, "--color", "never")
        assert ANSI not in result.output

    def test_an_unknown_value_is_a_usage_error(self, plain_console):
        result = run(*DRY, "--color", "sometimes")
        assert result.exit_code == 2
        assert "auto" in result.output and "always" in result.output

    def test_nested_commands_accept_it(self, plain_console):
        result = run("config", "list", "--color", "never")
        assert result.exit_code == 0
        result = run("wishlist", "list", "--color", "never")
        assert result.exit_code == 0

    def test_it_is_in_the_help_of_every_level(self, plain_console):
        assert "--color" in run("--help").output
        assert "--color" in run("find", "--help").output
        assert "--color" in run("wishlist", "add", "--help").output


class TestBeatsTheEnvironment:
    def _in_subprocess(self, tmp_path, env_var, args):
        env = {
            k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "FORCE_COLOR")
        }
        env.update({"DEALS_HOME": str(tmp_path), env_var: "1", "TERM": "xterm"})
        return subprocess.run(
            [sys.executable, "-m", "audible_deals", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def test_the_environment_alone_is_honored(self, tmp_path):
        assert ANSI in self._in_subprocess(tmp_path, "FORCE_COLOR", DRY).stdout
        assert ANSI not in self._in_subprocess(tmp_path, "NO_COLOR", DRY).stdout

    @pytest.mark.parametrize("position", ["before", "after"])
    def test_always_beats_no_color(self, tmp_path, position):
        flag = ["--color", "always"]
        args = [*flag, *DRY] if position == "before" else [*DRY, *flag]

        result = self._in_subprocess(tmp_path, "NO_COLOR", args)

        assert result.returncode == 0, result.stderr
        assert ANSI in result.stdout

    @pytest.mark.parametrize("position", ["before", "after"])
    def test_never_beats_force_color(self, tmp_path, position):
        flag = ["--color", "never"]
        args = [*flag, *DRY] if position == "before" else [*DRY, *flag]

        result = self._in_subprocess(tmp_path, "FORCE_COLOR", args)

        assert result.returncode == 0, result.stderr
        assert ANSI not in result.stdout
        assert "Dry run" in result.stdout


def test_rich_still_has_the_color_system_attribute_we_set(plain_console):
    # set_color_mode relies on this private attribute; a Rich upgrade that
    # renames it must fail here, not silently stop honoring --color.
    assert hasattr(plain_console, "_color_system")
    assert hasattr(plain_console, "no_color")
