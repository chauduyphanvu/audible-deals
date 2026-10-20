"""Shared click options and shell-completion callbacks.

The filter options of every command come from the filter registry
(:mod:`audible_deals.filter_specs`) through :func:`filter_options`; the
output/run options that are not filters (``--json``, ``-o``, ``-q``,
``--show-url``, ``--dry-run``, ``--allow-large-scan``, ``--profile``, ``-i``
on commands without a persisted preference) come from :func:`output_options`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import click

from audible_deals.config_store import load_profiles
from audible_deals.constants import GENRE_ALIASES
from audible_deals.filter_specs import FilterSpec, Kind, Scope, specs_for
from audible_deals.serialization import validate_export_path
from audible_deals.validation import FiniteFloatRange, IsoDate


def _validate_output_path(ctx, param, value):
    validate_export_path(value)
    return value


def _complete_profile_names(ctx, param, incomplete):
    from click.shell_completion import CompletionItem

    try:
        names = load_profiles().keys()
    except Exception:
        return []
    return [CompletionItem(n) for n in names if n.startswith(incomplete)]


def _complete_genre_names(ctx, param, incomplete):
    from click.shell_completion import CompletionItem

    return [CompletionItem(k) for k in GENRE_ALIASES if k.startswith(incomplete)]


_COMPLETERS: dict[str, Callable] = {"genre": _complete_genre_names}


def _click_type(spec: FilterSpec, scope: Scope) -> click.ParamType | None:
    kind = spec.scope_kind(scope)
    if kind is Kind.FLOAT or kind is Kind.RATING:
        return FiniteFloatRange(spec.lower_bound, spec.upper_bound)
    if kind is Kind.INT or kind is Kind.PERCENT:
        return click.IntRange(min=spec.lower_bound, max=spec.upper_bound)
    if kind is Kind.DATE:
        return IsoDate()
    choices = spec.scope_choices(scope)
    if choices is not None:
        return click.Choice(list(choices))
    return None


def _click_option(spec: FilterSpec, scope: Scope) -> Callable:
    kind = spec.scope_kind(scope)
    kwargs: dict[str, Any] = {
        "help": spec.scope_help(scope) or None,
        "hidden": spec.variant(scope).hidden,
    }
    if spec.metavar:
        kwargs["metavar"] = spec.metavar
    if spec.complete:
        kwargs["shell_complete"] = _COMPLETERS[spec.complete]
    if kind is Kind.MULTI_TEXT:
        kwargs["multiple"] = True
    elif kind is Kind.FLAG:
        kwargs["is_flag"] = True
        kwargs["default"] = spec.scope_default(scope)
    else:
        kwargs["default"] = spec.scope_default(scope)
    click_type = _click_type(spec, scope)
    if click_type is not None:
        kwargs["type"] = click_type
    return click.option(*spec.option_strings(scope), spec.param_name(scope), **kwargs)


def filter_options(scope: Scope) -> Callable:
    """Decorator adding the click options of every filter available in *scope*."""
    options = [_click_option(spec, scope) for spec in specs_for(scope)]

    def decorator(func: Callable) -> Callable:
        # click decorators stack bottom-up, so apply in reverse for registry order
        for option in reversed(options):
            func = option(func)
        return func

    return decorator


def scope_values(scope: Scope, params: Mapping[str, Any]) -> dict[str, Any]:
    """Registry keys -> the values a command received for *scope*'s options."""
    return {
        spec.key: params[spec.param_name(scope)]
        for spec in specs_for(scope)
        if spec.param_name(scope) in params
    }


_OUTPUT_HELP = {
    "output": "Export results to file (.json or .csv)",
    "json": "Output results as JSON to stdout",
    "quiet": "Suppress table output (useful with --output)",
    "show_url": "Show Audible URL for each item in the table",
    "dry_run": "Show what would be scanned without making API calls",
    "interactive": "Browse results interactively",
    "refresh": "Ignore cached catalog pages and fetch live prices",
    "exit_code": "Exit with status 10 when nothing matched (0 when something did)",
    "profile": "Load a saved search profile (overrides defaults, CLI flags take precedence)",
}


def json_option(help: str | None = None) -> Callable:
    """``--json``: machine-readable output on stdout, everything else on stderr."""
    return click.option(
        "--json",
        "json_flag",
        is_flag=True,
        default=False,
        help=help or _OUTPUT_HELP["json"],
    )


def output_options(
    *names: str, validate_output_early: bool = False
) -> Callable[[Callable], Callable]:
    """Decorator adding the output/run options that are not filters.

    *names* picks from ``output``, ``json``, ``quiet``, ``show_url``,
    ``dry_run``, ``refresh``, ``exit_code``, ``interactive`` (the plain ``-i`` flag of
    commands without a persisted preference) and ``profile``. ``validate_output_early`` checks the
    ``-o`` path while parsing instead of in the command body.
    """
    unknown = set(names) - set(_OUTPUT_HELP)
    if unknown:
        raise ValueError(f"unknown output option(s): {sorted(unknown)}")
    builders: dict[str, Callable] = {
        "output": lambda: click.option(
            "--output",
            "-o",
            type=click.Path(path_type=Path),
            default=None,
            callback=_validate_output_path if validate_output_early else None,
            help=_OUTPUT_HELP["output"],
        ),
        "json": json_option,
        "quiet": lambda: click.option(
            "--quiet",
            "-q",
            is_flag=True,
            default=False,
            help=_OUTPUT_HELP["quiet"],
        ),
        "show_url": lambda: click.option(
            "--show-url",
            is_flag=True,
            default=False,
            help=_OUTPUT_HELP["show_url"],
        ),
        "dry_run": lambda: click.option(
            "--dry-run",
            is_flag=True,
            default=False,
            help=_OUTPUT_HELP["dry_run"],
        ),
        "exit_code": lambda: click.option(
            "--exit-code",
            "exit_code",
            is_flag=True,
            default=False,
            help=_OUTPUT_HELP["exit_code"],
        ),
        "refresh": lambda: click.option(
            "--refresh",
            is_flag=True,
            default=False,
            help=_OUTPUT_HELP["refresh"],
        ),
        "interactive": lambda: click.option(
            "--interactive",
            "-i",
            is_flag=True,
            default=False,
            help=_OUTPUT_HELP["interactive"],
        ),
        "profile": lambda: click.option(
            "--profile",
            "profile_name",
            default=None,
            help=_OUTPUT_HELP["profile"],
            shell_complete=_complete_profile_names,
        ),
    }
    options = [builders[name]() for name in names]

    def decorator(func: Callable) -> Callable:
        for option in reversed(options):
            func = option(func)
        return func

    return decorator
