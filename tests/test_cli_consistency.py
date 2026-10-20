"""The same thing is spelled the same way on every command."""

from __future__ import annotations

import re
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

import audible_deals
from audible_deals.cli import cli
from audible_deals.config_store import (
    load_config,
    load_monitors,
    save_config,
    save_monitors,
)
from audible_deals.constants import WEBHOOK_FORMATS
from tests.conftest import seed_history


def _walk(command=None, path=("deals",)):
    command = command or cli
    yield path, command
    if isinstance(command, click.Group):
        ctx = click.Context(command)
        for name in command.list_commands(ctx):
            yield from _walk(command.get_command(ctx, name), (*path, name))


def _find(*path):
    return next(cmd for p, cmd in _walk() if p == ("deals", *path))


def _option(command, name):
    return next(
        (p for p in command.params if isinstance(p, click.Option) and p.name == name),
        None,
    )


#: Every command that stops to ask "are you sure?". Adding a click.confirm
#: anywhere else fails test_every_confirmation_is_listed until it is added here
#: with its -y/--yes.
CONFIRMING = [
    ("config", "reset"),
    ("history",),
    ("monitor", "remove"),
    ("wishlist", "purge"),
    ("wishlist", "repair"),
]


@pytest.mark.parametrize("path", CONFIRMING, ids=[" ".join(p) for p in CONFIRMING])
def test_confirming_commands_take_dash_y_and_yes(path):
    option = _option(_find(*path), "yes")

    assert option is not None, f"deals {' '.join(path)} has no --yes"
    assert set(option.opts) == {"--yes", "-y"}


def test_every_confirmation_is_listed():
    root = Path(audible_deals.__file__).parent
    asked = sum(
        len(re.findall(r"click\.confirm\(", path.read_text(encoding="utf-8")))
        for path in root.rglob("*.py")
    )

    # config reset, history, monitor remove, and two in wishlist.
    assert asked == len(CONFIRMING)


class TestYesSkipsThePrompt:
    def test_config_reset_all(self, tmp_config):
        save_config({"max_price": 5.0})

        result = CliRunner().invoke(cli, ["config", "reset", "-y"])

        assert result.exit_code == 0, result.output
        assert load_config() == {}

    def test_config_reset_all_still_asks_without_it(self, tmp_config):
        save_config({"max_price": 5.0})

        result = CliRunner().invoke(cli, ["config", "reset"], input="n\n")

        assert "Cancelled" in result.output
        assert load_config() == {"max_price": 5.0}

    def test_monitor_remove(self, tmp_config):
        save_monitors(
            {
                "cheap": {
                    "version": 1,
                    "name": "cheap",
                    "enabled": True,
                    "locale": "us",
                    "mode": "find",
                    "query": "",
                    "settings": {"pages": 1, "limit": None},
                }
            }
        )

        result = CliRunner().invoke(cli, ["monitor", "remove", "cheap", "-y"])

        assert result.exit_code == 0, result.output
        assert load_monitors() == {}

    def test_history_purge(self, tmp_config):
        seed_history("OLDHIST001", [("2020-01-01", 5.0)])

        result = CliRunner().invoke(cli, ["history", "--purge-older-than", "90", "-y"])

        assert result.exit_code == 0, result.output
        assert "Removed 1 stale history files" in result.output


def test_every_webhook_format_option_offers_the_shared_choices():
    found = []
    for path, command in _walk():
        option = _option(command, "webhook_format")
        if option is not None:
            found.append(path)
            assert isinstance(option.type, click.Choice), path
            assert tuple(option.type.choices) == tuple(WEBHOOK_FORMATS), path

    assert {p[1:] for p in found} >= {
        ("notify",),
        ("recap",),
        ("track", "install"),
        ("monitor", "add"),
    }


def test_color_choices_match_the_terminal_module():
    from audible_deals.presentation.terminal import COLOR_MODES

    for path, command in _walk():
        option = _option(command, "color")
        if option is not None:
            assert tuple(option.type.choices) == tuple(COLOR_MODES), path


class TestCooldown:
    def test_both_automation_commands_accept_zero(self):
        notify = _option(_find("notify"), "cooldown")
        track = _option(_find("track", "run"), "cooldown")

        assert notify.type.min == track.type.min == 0

    def test_the_defaults_differ_on_purpose(self):
        # `notify` reports every deal unless asked; the unattended `track run`
        # defaults to one day. docs/automation.md explains why.
        assert _option(_find("notify"), "cooldown").default is None
        assert _option(_find("track", "run"), "cooldown").default == 1

    def test_the_difference_is_documented(self):
        text = (
            Path(audible_deals.__file__).parents[2] / "docs" / "automation.md"
        ).read_text(encoding="utf-8")

        assert "`--cooldown` differs on purpose" in text
