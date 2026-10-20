"""Global options (--locale, -v/--verbose) are accepted after the subcommand too."""

from __future__ import annotations

import logging

import click
import pytest
from click.testing import CliRunner

import audible_deals.cli.catalog as cli_catalog_mod
from audible_deals.cli import cli
from audible_deals.cli.global_options import attach_global_options
from tests.conftest import make_product


@pytest.fixture(autouse=True)
def reset_logging(monkeypatch):
    monkeypatch.delenv("DEALS_LOG_FILE", raising=False)
    monkeypatch.delenv("DEALS_DEBUG", raising=False)
    yield
    root = logging.getLogger("audible_deals")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.WARNING)


def _level() -> int:
    return logging.getLogger("audible_deals").getEffectiveLevel()


def _walk(command: click.Command, path: tuple[str, ...] = ()):
    """Yield (path, command) for every non-root command in the tree."""
    if isinstance(command, click.Group):
        ctx = click.Context(command)
        for name in command.list_commands(ctx):
            sub = command.get_command(ctx, name)
            assert sub is not None, name
            yield (*path, name), sub
            yield from _walk(sub, (*path, name))


def _flags(command: click.Command) -> set[str]:
    return {flag for param in command.params for flag in (*param.opts,)}


# ---------------------------------------------------------------------------
# Structure: every command, leaf and nested group alike, carries the options
# ---------------------------------------------------------------------------


def test_every_subcommand_accepts_locale_and_verbose():
    seen = list(_walk(cli))
    assert len(seen) > 30  # the whole tree was visited, nested groups included
    assert ("wishlist", "add") in {path for path, _ in seen}
    missing = [
        " ".join(path)
        for path, command in seen
        if not {"--locale", "-v", "--verbose"} <= _flags(command)
    ]
    assert missing == []


@pytest.mark.parametrize(
    "args",
    [["find"], ["wishlist", "add"], ["monitor", "add"], ["track", "run"]],
)
def test_help_documents_the_options(args):
    result = CliRunner().invoke(cli, [*args, "--help"])

    assert result.exit_code == 0, result.output
    assert "--locale" in result.output
    assert "--verbose" in result.output


def test_attach_is_idempotent_and_never_overrides_a_command_own_flag():
    @click.command()
    @click.option("--locale", "own_locale", default="mine")
    def probe(own_locale):
        click.echo(own_locale)

    attach_global_options(probe)
    attach_global_options(probe)

    locale_options = [p for p in probe.params if "--locale" in p.opts]
    assert [p.name for p in locale_options] == ["own_locale"]
    verbose_options = [p for p in probe.params if "--verbose" in p.opts]
    assert len(verbose_options) == 1
    assert CliRunner().invoke(probe, ["--locale", "x"]).output.strip() == "x"


# ---------------------------------------------------------------------------
# Probe tree: semantics independent of any real command
# ---------------------------------------------------------------------------


def _probe_cli():
    @click.group()
    @click.pass_context
    def root(ctx):
        ctx.ensure_object(dict)
        ctx.obj.update(locale="us", locale_explicit=False)

    @root.group()
    def group():
        pass

    @group.command("leaf")
    @click.pass_context
    def leaf(ctx):
        click.echo(
            f"{ctx.obj['locale']} {ctx.obj['locale_explicit']} {ctx.obj.get('verbose')}"
        )

    @root.command("top")
    @click.pass_context
    def top(ctx):
        click.echo(
            f"{ctx.obj['locale']} {ctx.obj['locale_explicit']} {ctx.obj.get('verbose')}"
        )

    attach_global_options(root)
    return root


def test_leaf_locale_marks_the_locale_explicit():
    result = CliRunner().invoke(_probe_cli(), ["top", "--locale", "uk"])

    assert result.output.strip() == "uk True None"


def test_nested_group_leaf_accepts_the_options():
    result = CliRunner().invoke(
        _probe_cli(), ["group", "leaf", "--locale", "de", "-vv"]
    )

    assert result.output.strip() == "de True 2"


def test_group_level_option_applies_to_its_subcommands():
    result = CliRunner().invoke(_probe_cli(), ["group", "--locale", "fr", "leaf"])

    assert result.output.strip() == "fr True None"


def test_without_the_option_nothing_changes():
    result = CliRunner().invoke(_probe_cli(), ["top"])

    assert result.output.strip() == "us False None"


def test_invalid_leaf_locale_is_a_usage_error_naming_valid_values():
    result = CliRunner().invoke(_probe_cli(), ["top", "--locale", "xx"])

    assert result.exit_code == 2
    assert "Invalid locale 'xx'" in result.output
    assert "au, ca, de, es, fr, in, jp, uk, us" in result.output


def test_options_work_when_a_command_runs_without_the_root_group():
    @click.command()
    def bare():
        click.echo("ran")

    attach_global_options(bare)
    result = CliRunner().invoke(bare, ["--locale", "uk", "-v"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "ran"


# ---------------------------------------------------------------------------
# Real CLI: --locale
# ---------------------------------------------------------------------------


@pytest.fixture
def recorded_locales(mock_client, monkeypatch):
    locales: list[str] = []

    def get_client(locale):
        locales.append(locale)
        return mock_client

    monkeypatch.setattr(cli_catalog_mod, "_get_client", get_client)
    products = [make_product(asin="GLOB0001", price=3.0)]
    mock_client.search_pages.side_effect = lambda *a, **kw: iter(
        [(products, 1, len(products))]
    )
    return locales


def test_find_accepts_locale_after_the_subcommand(tmp_config, recorded_locales):
    result = CliRunner().invoke(
        cli, ["find", "--locale", "uk", "--pages", "1", "--max-price", "10"]
    )

    assert result.exit_code == 0, result.output
    assert recorded_locales == ["uk"]


def test_search_accepts_locale_after_the_subcommand(tmp_config, recorded_locales):
    result = CliRunner().invoke(cli, ["search", "dune", "--locale", "ca"])

    assert result.exit_code == 0, result.output
    assert recorded_locales == ["ca"]


def test_leaf_locale_wins_over_root_locale(tmp_config, recorded_locales):
    result = CliRunner().invoke(
        cli,
        [
            "--locale",
            "us",
            "find",
            "--locale",
            "au",
            "--pages",
            "1",
            "--max-price",
            "9",
        ],
    )

    assert result.exit_code == 0, result.output
    assert recorded_locales == ["au"]


def test_leaf_locale_wins_over_configured_locale(tmp_config, recorded_locales):
    CliRunner().invoke(cli, ["config", "set", "locale", "uk"])

    configured = CliRunner().invoke(cli, ["search", "dune"])
    overridden = CliRunner().invoke(cli, ["search", "dune", "--locale", "de"])

    assert configured.exit_code == 0, configured.output
    assert overridden.exit_code == 0, overridden.output
    assert recorded_locales == ["uk", "de"]


def test_invalid_locale_after_subcommand_matches_the_root_validation(tmp_config):
    leaf = CliRunner().invoke(cli, ["find", "--locale", "xx"])
    root = CliRunner().invoke(cli, ["--locale", "xx", "find"])

    assert leaf.exit_code == root.exit_code == 2
    assert (
        "Invalid locale 'xx'. Valid: au, ca, de, es, fr, in, jp, uk, us" in leaf.output
    )
    assert (
        "Invalid locale 'xx'. Valid: au, ca, de, es, fr, in, jp, uk, us" in root.output
    )


def test_locale_works_on_a_command_inside_a_group(tmp_config):
    result = CliRunner().invoke(cli, ["config", "list", "--locale", "uk"])

    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Real CLI: -v / --verbose
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ([], logging.WARNING),
        (["-v"], logging.INFO),
        (["-vv"], logging.DEBUG),
        (["--verbose", "--verbose"], logging.DEBUG),
    ],
)
def test_verbose_after_a_top_level_subcommand(tmp_config, flags, expected):
    result = CliRunner().invoke(cli, ["config", "list", *flags])

    assert result.exit_code == 0, result.output
    assert _level() == expected


@pytest.mark.parametrize(
    "args",
    [
        ["wishlist", "list"],
        ["profile", "list"],
        ["monitor", "list"],
        ["track", "status"],
    ],
)
def test_verbose_after_a_nested_subcommand(tmp_config, args):
    result = CliRunner().invoke(cli, [*args, "-vv"])

    assert result.exit_code == 0, result.output
    assert _level() == logging.DEBUG


def test_find_accepts_verbose_after_the_subcommand(tmp_config, recorded_locales):
    result = CliRunner().invoke(
        cli, ["find", "-v", "--pages", "1", "--max-price", "10"]
    )

    assert result.exit_code == 0, result.output
    assert _level() == logging.INFO


def test_leaf_verbose_wins_over_root_verbose(tmp_config):
    assert CliRunner().invoke(cli, ["-vv", "config", "list", "-v"]).exit_code == 0
    assert _level() == logging.INFO

    assert CliRunner().invoke(cli, ["-v", "config", "list", "-vv"]).exit_code == 0
    assert _level() == logging.DEBUG


def test_root_verbose_still_applies_when_the_leaf_does_not_repeat_it(tmp_config):
    assert CliRunner().invoke(cli, ["-vv", "config", "list"]).exit_code == 0
    assert _level() == logging.DEBUG


def test_deals_debug_env_still_forces_debug(tmp_config, monkeypatch):
    monkeypatch.setenv("DEALS_DEBUG", "1")

    assert CliRunner().invoke(cli, ["config", "list", "-v"]).exit_code == 0
    assert _level() == logging.DEBUG
