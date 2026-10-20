"""Shared helpers for CLI command modules."""

from __future__ import annotations

import sys

import click

from audible_deals.catalog_workflow import (
    ScanReport,
    bind_catalog_categories,
    estimate_cached_calls,
)
from audible_deals.client import DealsClient, cached_top_categories
from audible_deals.config_store import load_profiles
from audible_deals.constants import LOCALE_CURRENCY
from audible_deals.genres import match_genre
from audible_deals.presentation.common import age_str
from audible_deals.presentation.terminal import console, safe_markup, safe_text
from audible_deals.result_models import CatalogScanPlan
from audible_deals.results_cache import (
    load_dismissed_asins,
    load_seen_asins,
    merge_seen_asins,
)
from audible_deals.scan_cache import ScanCache
from audible_deals.selectors import resolve_selectors
from audible_deals.settings import profile_validation_error
from audible_deals.validation import validate_finite_number

_CL = click.core.ParameterSource.COMMANDLINE


def _get_client(locale: str) -> DealsClient:
    return DealsClient(locale=locale)


def _currency(ctx: click.Context) -> str:
    return LOCALE_CURRENCY.get(ctx.obj["locale"], "$")


def _credit_price(ctx: click.Context) -> float | None:
    """Configured per-credit price ('deals config set credit-price'), if any."""
    value = ctx.obj.get("config", {}).get("credit_price")
    if value is None:
        return None
    try:
        validate_finite_number("credit_price", value, 0)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    return float(value)


def _resolve_skip_asins(
    dc: DealsClient, skip_owned: bool, exclude_seen: bool
) -> set[str] | None:
    """Build the set of ASINs to exclude from discovery."""
    skip_asins = dc.get_library_asins() if skip_owned else None
    skip_asins = merge_seen_asins(skip_asins, exclude_seen)
    dismissed = load_dismissed_asins()
    if skip_asins is None:
        return dismissed or None
    return skip_asins | dismissed


def _resolve_skip_snapshots(
    dc: DealsClient,
    skip_owned: bool,
    exclude_seen: bool,
    cache: ScanCache | None = None,
) -> tuple[set[str] | None, set[str], set[str], set[str]]:
    """Resolve exclusions and retain the components needed for cached clearing.

    With a *cache*, the owned list may come from the stored copy (a day old at
    most) instead of a fresh library fetch.
    """
    if not skip_owned:
        owned: set[str] = set()
    elif cache is None:
        owned = dc.get_library_asins()
    else:
        owned = dc.get_library_asins(cache=cache)
    seen = load_seen_asins()
    dismissed = load_dismissed_asins()
    combined = owned | (seen if exclude_seen else set()) | dismissed
    return (combined or None), owned, seen, dismissed


def _scan_cache(ctx: click.Context, refresh: bool) -> ScanCache | None:
    """The catalog cache for this run, or ``None`` when ``cache-ttl`` is 0."""
    return ScanCache.from_config(ctx.obj.get("config", {}), refresh=refresh)


def _offline_category(locale: str, genre: str, category: str) -> str | None:
    """The category id a scan will use, if it is known without a request.

    ``None`` means "cannot tell yet" (a genre that needs the category list and
    the list is not stored, or does not match).
    """
    if category:
        return category
    if not genre:
        return ""
    categories = cached_top_categories(locale)
    if not categories:
        return None
    try:
        return match_genre(genre, categories)[0]
    except ValueError:
        return None


def _estimated_cached_calls(
    ctx: click.Context,
    plan: CatalogScanPlan,
    cache: ScanCache | None,
    *,
    genre: str,
    category: str,
) -> int:
    """How many of the plan's pages a stored copy would answer (a floor)."""
    if cache is None or cache.refresh or plan.category_ids is None:
        return 0
    category_id = _offline_category(ctx.obj["locale"], genre, category)
    if category_id is None:
        return 0
    return estimate_cached_calls(
        bind_catalog_categories(plan, [category_id]), ctx.obj["locale"], cache
    )


def _announce(message: str, *, json_flag: bool, style: str = "dim") -> None:
    """A status line that stays out of JSON on stdout."""
    if json_flag:
        click.echo(safe_text(message), err=True)
    else:
        console.print(f"[{style}]{safe_markup(message)}[/{style}]")


def _report_cache_use(cache: ScanCache | None, *, json_flag: bool, quiet: bool) -> None:
    """Tell the user which data came from the cache, and how to get live data."""
    if cache is None or quiet:
        return
    use = cache.use()
    if not use.any:
        return
    parts = []
    if use.pages_cached and use.oldest_age_s is not None:
        parts.append(
            f"prices up to {age_str(use.oldest_age_s)} old "
            f"({use.pages_cached} of {use.pages_cached + use.pages_live} pages)"
        )
    if use.library_age_s is not None:
        parts.append(f"owned titles {age_str(use.library_age_s)} old")
    _announce(
        f"Using cached results: {', '.join(parts)}. "
        "Add --refresh for live prices; cached prices are not added to your history.",
        json_flag=json_flag,
    )


def _report_interruption(report: ScanReport, *, json_flag: bool) -> None:
    if report.interrupted:
        _announce(
            f"Scan interrupted after {report.pages_done}/{report.pages_total} "
            "pages; showing what was gathered so far.",
            json_flag=json_flag,
            style="yellow",
        )


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
            console.print(f"[dim]{safe_markup(item.description)}[/dim]")
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
    profile = profiles[profile_name]
    if error := profile_validation_error(profile):
        raise click.ClickException(
            f"Profile '{profile_name}' is malformed: {error}. "
            "Save it again or delete it."
        )
    return profile


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
            raise click.ClickException(str(e)) from e
    elif category:
        try:
            category_name = dc.get_category_name(category)
        except ValueError as e:
            raise click.ClickException(str(e)) from e
    for eg in exclude_genre:
        try:
            eid, _ = dc.resolve_genre(eg)
            exclude_category_ids.add(eid)
        except ValueError as e:
            raise click.ClickException(str(e)) from e
    return category, category_name, exclude_category_ids


def _resolve_output_quiet(ctx: click.Context, output, json_flag, quiet) -> bool:
    """Output file implies quiet (unless -q was given explicitly); JSON output moves console chatter to stderr."""
    if output and ctx.get_parameter_source("quiet") != _CL:
        quiet = True
    if json_flag:
        console.file = sys.stderr
    return quiet


def _report_partial_series_outcomes(
    failures: tuple[str, ...],
    incomplete: tuple[str, ...],
    total: int,
    *,
    json_flag: bool,
) -> None:
    if not failures and not incomplete:
        return
    completed = total - len(failures)
    outcomes = []
    if failures:
        outcomes.append(f"{len(failures)} failed")
    if incomplete:
        outcomes.append(f"{len(incomplete)} incomplete")
    summary = (
        f"Partial results: {completed}/{total} series scanned; {'; '.join(outcomes)}."
    )
    details = (*failures, *incomplete)
    if json_flag:
        click.echo(summary, err=True)
        for detail in details:
            click.echo(f"  {safe_text(detail)}", err=True)
    else:
        console.print(f"[yellow]{safe_markup(summary)}[/yellow]")
        for detail in details:
            console.print(f"[dim]  {safe_markup(detail)}[/dim]")
