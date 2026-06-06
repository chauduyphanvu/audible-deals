"""Shared helpers for CLI command modules."""

from __future__ import annotations

import logging
import sys

import click

from audible_deals.client import DealsClient, Product
from audible_deals.config_store import load_profiles
from audible_deals.constants import LOCALE_CURRENCY
from audible_deals.display import console
from audible_deals.price_history import record_prices
from audible_deals.results_cache import merge_seen_asins, resolve_last_references

logger = logging.getLogger(__name__)

_CL = click.core.ParameterSource.COMMANDLINE


def _collect_asins(asins: tuple[str, ...], last_refs: tuple[str, ...]) -> list[str]:
    """Combine positional ASINs with resolved --last references."""
    all_asins = list(asins)
    if last_refs:
        for ref_asin, desc in resolve_last_references(last_refs):
            console.print(f"[dim]{desc}[/dim]")
            all_asins.append(ref_asin)
    return all_asins


def _resolve_single_last_ref(last_ref: str) -> tuple[str, str]:
    resolved = resolve_last_references((last_ref,))
    if len(resolved) != 1:
        raise click.ClickException(
            f"--last {last_ref!r} expanded to {len(resolved)} results; this command accepts a single position."
        )
    return resolved[0]


def _get_client(locale: str) -> DealsClient:
    return DealsClient(locale=locale)


def _currency(ctx: click.Context) -> str:
    return LOCALE_CURRENCY.get(ctx.obj["locale"], "$")


def _resolve_skip_asins(
    dc: DealsClient, skip_owned: bool, exclude_seen: bool
) -> set[str] | None:
    """Build the set of ASINs to exclude from owned library and seen history."""
    skip_asins = dc.get_library_asins() if skip_owned else None
    return merge_seen_asins(skip_asins, exclude_seen)


def _safe_record_prices(products: list[Product]) -> None:
    """Record prices, warning on failure instead of crashing."""
    try:
        record_prices(products)
    except Exception as e:
        logger.exception("record_prices failed for %d products", len(products))
        console.print(f"[dim]Warning: could not record price history: {e}[/dim]")


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
