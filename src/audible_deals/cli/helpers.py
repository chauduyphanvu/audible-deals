"""Shared helpers for CLI command modules."""

from __future__ import annotations

import sys

import click

from audible_deals.client import DealsClient
from audible_deals.config_store import load_profiles
from audible_deals.constants import LOCALE_CURRENCY
from audible_deals.presentation.terminal import console
from audible_deals.results_cache import (
    load_seen_asins,
    merge_seen_asins,
)
from audible_deals.selectors import resolve_selectors

_CL = click.core.ParameterSource.COMMANDLINE


def _get_client(locale: str) -> DealsClient:
    return DealsClient(locale=locale)


def _currency(ctx: click.Context) -> str:
    return LOCALE_CURRENCY.get(ctx.obj["locale"], "$")


def _credit_price(ctx: click.Context) -> float | None:
    """Configured per-credit price ('deals config set credit-price'), if any."""
    value = ctx.obj.get("config", {}).get("credit_price")
    return float(value) if value is not None else None


def _resolve_skip_asins(
    dc: DealsClient, skip_owned: bool, exclude_seen: bool
) -> set[str] | None:
    """Build the set of ASINs to exclude from owned library and seen history."""
    skip_asins = dc.get_library_asins() if skip_owned else None
    return merge_seen_asins(skip_asins, exclude_seen)


def _resolve_skip_snapshots(
    dc: DealsClient, skip_owned: bool, exclude_seen: bool
) -> tuple[set[str] | None, set[str], set[str]]:
    """Resolve exclusions and retain the components needed for cached clearing."""
    owned = dc.get_library_asins() if skip_owned else set()
    seen = load_seen_asins()
    combined = owned | (seen if exclude_seen else set())
    return (combined or None), owned, seen


def _resolve_cli_selectors(
    ctx: click.Context,
    selectors: tuple[str, ...],
    last_refs: tuple[str | int, ...] = (),
    *,
    single: bool = False,
    announce: bool = True,
):
    """Resolve CLI product selectors and apply marketplace inference."""
    explicit = ctx.obj["locale"] if ctx.obj.get("locale_explicit") else None
    resolved, inferred_locale = resolve_selectors(
        selectors,
        last_refs=last_refs,
        single=single,
        explicit_locale=explicit,
    )
    locale = inferred_locale or ctx.obj["locale"]
    for item in resolved:
        if announce and item.description:
            console.print(f"[dim]{item.description}[/dim]")
    return resolved, locale


def _load_profile(profile_name: str | None) -> dict | None:
    if not profile_name:
        return None
    profiles = load_profiles()
    if profile_name not in profiles:
        raise click.ClickException(
            f"Profile '{profile_name}' not found. "
            "Use 'deals profile list' to see available profiles."
        )
    return profiles[profile_name]


def _resolve_categories(
    dc: DealsClient,
    genre: str,
    category: str,
    exclude_genre: tuple[str, ...],
) -> tuple[str, str, set[str]]:
    """Resolve genre/category names to IDs.

    Returns (category_id, category_name, exclude_category_ids).
    """
    category_name = ""
    exclude_category_ids: set[str] = set()
    if genre:
        try:
            category, category_name = dc.resolve_genre(genre)
        except ValueError as e:
            raise click.ClickException(str(e))
    elif category:
        try:
            category_name = dc.get_category_name(category)
        except ValueError as e:
            raise click.ClickException(str(e))
    for eg in exclude_genre:
        try:
            eid, _ = dc.resolve_genre(eg)
            exclude_category_ids.add(eid)
        except ValueError as e:
            raise click.ClickException(str(e))
    return category, category_name, exclude_category_ids


def _resolve_output_quiet(ctx: click.Context, output, json_flag, quiet) -> bool:
    """Output file implies quiet (unless -q was given explicitly); JSON output moves console chatter to stderr."""
    if output and ctx.get_parameter_source("quiet") != _CL:
        quiet = True
    if json_flag:
        console.file = sys.stderr
    return quiet
