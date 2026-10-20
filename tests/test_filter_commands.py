"""The registry-generated click options of every command.

``find``, ``search``, ``last``, ``for-me``, ``series``, ``profile save`` and
``monitor add`` get their filter flags from ``filter_specs.FILTERS``; these
tests fail when a command and the registry stop agreeing, and cover profile
saving through the CLI.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from audible_deals.cli import cli
from audible_deals.config_store import load_profiles
from audible_deals.filter_specs import (
    FILTERS,
    RESOLVING_SCOPES,
    Scope,
    scope_defaults,
    specs_for,
)
from audible_deals.settings import profile_validation_error

ROOT = Path(__file__).resolve().parents[1]

# Scope -> the command that must expose exactly that scope's registry flags.
SCOPE_COMMANDS: dict[Scope, tuple[str, ...]] = {
    Scope.FIND: ("find",),
    Scope.SEARCH: ("search",),
    Scope.LAST: ("last",),
    Scope.FOR_ME: ("for-me",),
    Scope.SERIES: ("series",),
    Scope.PROFILE: ("profile", "save"),
    Scope.MONITOR: ("monitor", "add"),
}


def _command(path: tuple[str, ...]) -> click.Command:
    command: click.Command = cli
    for name in path:
        assert isinstance(command, click.Group)
        found = command.get_command(click.Context(command), name)
        assert found is not None, path
        command = found
    return command


def _options(command: click.Command) -> dict[str, click.Option]:
    return {p.name: p for p in command.params if isinstance(p, click.Option) and p.name}


def _flags(option: click.Option) -> set[str]:
    return {*option.opts, *option.secondary_opts}


# ---------------------------------------------------------------------------
# Click commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scope", SCOPE_COMMANDS, ids=lambda s: s.value)
def test_each_command_exposes_exactly_its_scopes_filters(scope):
    options = _options(_command(SCOPE_COMMANDS[scope]))
    expected = {spec.param_name(scope): spec for spec in specs_for(scope)}
    for param, spec in expected.items():
        assert param in options, f"{scope.value}: {spec.flag} missing"
        flags = _flags(options[param])
        assert spec.flag in flags
        assert set(spec.aliases) <= flags
    # Nothing else on the command may claim a registry flag, except options
    # that deliberately reuse a spelling for something else.
    allowed = {
        (Scope.SERIES, "series_filter"),  # which series to scan, not a product filter
        (Scope.SERIES, "interactive"),  # plain -i on commands without a preference
        (Scope.LAST, "interactive"),
    }
    registry_flags = {flag for spec in FILTERS for flag in (spec.flag, *spec.aliases)}
    for param, option in options.items():
        if param in expected or (scope, param) in allowed:
            continue
        stray = _flags(option) & registry_flags
        assert not stray, f"{scope.value}: {param} duplicates registry flags {stray}"


def test_scope_defaults_reach_the_click_options():
    for scope in RESOLVING_SCOPES:
        options = _options(_command(SCOPE_COMMANDS[scope]))
        for spec in specs_for(scope):
            option = options[spec.param_name(scope)]
            if spec.is_multi:
                continue
            assert option.default == spec.scope_default(scope), (scope, spec.key)


def test_patch_scopes_never_inject_defaults():
    for scope in (Scope.LAST, Scope.PROFILE, Scope.MONITOR):
        options = _options(_command(SCOPE_COMMANDS[scope]))
        for spec in specs_for(scope):
            if not spec.is_multi:
                assert options[spec.param_name(scope)].default is None, spec.key


def test_find_and_search_keep_their_historic_defaults():
    find = _options(_command(("find",)))
    search = _options(_command(("search",)))
    assert find["max_price"].default == 5.0
    assert find["min_ratings"].default == 1
    assert find["pages"].default == 10
    assert find["sort"].default == "price-per-hour"
    assert search["max_price"].default is None
    assert search["min_ratings"].default == 0
    assert search["pages"].default == 3
    assert search["sort"].default == "relevance"
    assert scope_defaults(Scope.FIND) == {"max_price": 5.0, "min_ratings": 1}
    assert scope_defaults(Scope.SEARCH) == {"sort": "relevance", "pages": 3}


def test_help_text_exists_for_every_filter_of_every_command():
    for scope, path in SCOPE_COMMANDS.items():
        options = _options(_command(path))
        for spec in specs_for(scope):
            if spec.variant(scope).hidden:
                continue
            assert options[spec.param_name(scope)].help, (path, spec.key)


@pytest.mark.parametrize(
    "path", [("find",), ("search",), ("last",), ("profile", "save")]
)
def test_new_registry_flags_are_available_where_they_were_missing(path):
    flags = {
        flag for option in _options(_command(path)).values() for flag in _flags(option)
    }
    assert {"--max-effective-price", "--hist-below", "--min-price-drop"} <= flags
    assert {"--require-history", "--released-after", "--released-before"} <= flags


def test_profile_save_and_monitor_add_gained_the_missing_filters():
    profile_flags = {
        f for o in _options(_command(("profile", "save"))).values() for f in _flags(o)
    }
    monitor_flags = {
        f for o in _options(_command(("monitor", "add"))).values() for f in _flags(o)
    }
    for flag in (
        "--max-effective-price",
        "--hist-below",
        "--min-price-drop",
        "--require-history",
        "--released-after",
        "--released-before",
        "--exclude-seen",
    ):
        assert flag in profile_flags, flag
    for flag in (
        "--max-effective-price",
        "--max-price-per-hour",
        "--hist-below",
        "--min-price-drop",
        "--require-history",
        "--released-after",
        "--released-before",
        "--exclude-genre",
        "--series",
        "--publisher",
        "--all-languages",
    ):
        assert flag in monitor_flags, flag
    assert "--exclude-seen" not in monitor_flags  # monitors never use "seen"


def test_last_clear_filter_keeps_every_published_name():
    expected = {
        "max-price",
        "max-pph",
        "max-effective-price",
        "min-rating",
        "min-ratings",
        "min-hours",
        "narrator",
        "author",
        "series",
        "publisher",
        "exclude-authors",
        "exclude-narrators",
        "language",
        "on-sale",
        "min-discount",
        "first-in-series",
        "skip-plus",
        "only-plus",
        "exclude-keywords",
        "hist-below",
        "min-price-drop",
        "require-history",
        "released-after",
        "released-before",
        "skip-owned",
        "exclude-seen",
        "exclude-genres",
        # spellings that match the CLI flag
        "max-price-per-hour",
        "exclude-author",
        "exclude-narrator",
        "exclude-keyword",
        "exclude-genre",
    }
    option = _options(_command(("last",)))["clear_filters"]
    assert isinstance(option.type, click.Choice)
    # New filters add names; none of the published spellings may disappear.
    assert expected <= set(option.type.choices)


def test_docs_mention_every_registry_flag():
    docs = (ROOT / "docs" / "commands.md").read_text(encoding="utf-8")
    for spec in FILTERS:
        assert spec.flag in docs, f"docs/commands.md does not document {spec.flag}"
        for alias in spec.aliases:
            assert alias in docs, f"docs/commands.md does not document {alias}"


_PROFILE_SAMPLES: dict[str, list[str]] = {
    "float": ["2.5"],
    "int": ["3"],
    "percent": ["25"],
    "rating": ["4.5"],
    "text": ["sample"],
    "multi_text": ["one"],
    "date": ["2024-02-03"],
}


@pytest.mark.parametrize("spec", specs_for(Scope.PROFILE), ids=lambda s: s.key)
def test_every_profile_filter_can_be_saved_and_validates(spec, tmp_config):
    if spec.valid is not None:
        sample = [next(v for v in sorted(spec.valid) if not v.startswith("-"))]
    elif spec.is_bool:
        sample = []
    else:
        sample = _PROFILE_SAMPLES[spec.kind.value]
    args = ["profile", "save", "p", spec.flag, *sample]

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0, result.output
    saved = load_profiles()["p"]
    assert list(saved) == [spec.key]
    assert profile_validation_error(saved) is None
    shown = CliRunner().invoke(cli, ["profile", "show", "p"])
    assert shown.exit_code == 0 and spec.flag_name in shown.output


def test_profile_save_rejects_inverted_release_window(tmp_config):
    result = CliRunner().invoke(
        cli,
        [
            "profile",
            "save",
            "p",
            "--released-after",
            "2025-01-02",
            "--released-before",
            "2025-01-01",
        ],
    )

    assert result.exit_code == 2
    assert "cannot be later" in result.output
    assert load_profiles() == {}


def test_profile_save_still_rejects_unknown_sorts_with_the_old_message(tmp_config):
    result = CliRunner().invoke(cli, ["profile", "save", "p", "--sort", "nope"])

    assert result.exit_code == 1
    assert "Invalid sort 'nope'. Choose from:" in result.output
