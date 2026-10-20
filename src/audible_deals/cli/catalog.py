"""Catalog search and deal-finding commands."""

from __future__ import annotations

import dataclasses
import logging
import shlex

import click

from audible_deals.catalog_workflow import (
    CatalogQueryError,
    ScanReport,
    bind_catalog_categories,
    build_find_scan_plan,
    build_search_scan_plan,
    estimate_cached_calls,
    execute_catalog_scan,
    normalized_search_text,
)
from audible_deals.cli.helpers import (
    _CL,
    _credit_price,
    _currency,
    _estimated_cached_calls,
    _get_client,
    _load_profile,
    _report_cache_use,
    _report_interruption,
    _resolve_categories,
    _resolve_output_quiet,
    _resolve_skip_snapshots,
    _scan_cache,
)
from audible_deals.cli.options import (
    filter_options,
    output_options,
    scope_values,
)
from audible_deals.constants import LOCALE_LANGUAGES, MAX_CATALOG_CALLS_PER_SCAN
from audible_deals.filter_specs import (
    FILTERS,
    Scope,
    active_filter_labels,
    specs_for,
)
from audible_deals.presentation.dry_run import (
    CatalogDryRunSummary,
    render_catalog_dry_run,
)
from audible_deals.presentation.terminal import (
    catalog_scan_progress,
    console,
    safe_markup,
)
from audible_deals.product import Product
from audible_deals.result_processing import (
    SettingsFilterRequest,
    process_settings_discovery,
    recipe_from_settings,
)
from audible_deals.result_publication import (
    ResultPublicationRequest,
    ResultSessionSpec,
    publish_discovery,
)
from audible_deals.settings import (
    Settings,
    SettingsResolutionRequest,
    filter_combination_error,
    resolve_settings,
)

logger = logging.getLogger(__name__)


def _enforce_catalog_call_limit(
    plan, allow_large_scan: bool, *, cached_calls: int = 0
) -> None:
    """Refuse a scan over the call cap; pages a stored copy answers are free."""
    estimate = None if plan.total_calls is None else plan.total_calls - cached_calls
    if (
        estimate is not None
        and estimate > MAX_CATALOG_CALLS_PER_SCAN
        and not allow_large_scan
    ):
        raise click.UsageError(
            f"Scan would use {estimate} catalog calls; limit is "
            f"{MAX_CATALOG_CALLS_PER_SCAN}. Re-run with --allow-large-scan."
        )


def _resolve_scan_settings(
    ctx, profile_name: str | None, scope: Scope, flags: dict[str, object]
) -> Settings:
    """Merge the command's flags with its profile, config and defaults."""
    cli_flags = scope_values(scope, flags)
    if scope is Scope.SEARCH:
        cli_flags["keywords"] = ""  # search takes its query positionally
    explicit_options = {
        spec.key
        for spec in specs_for(scope)
        if ctx.get_parameter_source(spec.param_name(scope)) == _CL
    }
    try:
        settings = resolve_settings(
            SettingsResolutionRequest(
                config=ctx.obj.get("config", {}),
                profile=_load_profile(profile_name),
                cli_flags=cli_flags,
                explicit_options=explicit_options,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    if problem := filter_combination_error(settings):
        raise click.UsageError(problem)
    if not settings.language and not settings.all_languages:
        settings = dataclasses.replace(
            settings, language=LOCALE_LANGUAGES.get(ctx.obj["locale"], "")
        )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "resolved scan settings: %s",
            {spec.key: getattr(settings, spec.key) for spec in FILTERS},
        )
    return settings


def _resolve_genre_category(ctx, genre: str, category: str) -> str:
    """Reconcile a (possibly profile-supplied) genre with a --category override.

    The conflict only fires when --genre is given on the command line; an
    explicit --category cleanly overrides a profile-supplied genre. Returns
    the genre to use for category resolution.
    """
    if not category:
        return genre
    if ctx.get_parameter_source("genre") == _CL:
        raise click.UsageError("Use --genre or --category, not both.")
    return ""


def _matching_author_queries(queries: list[str], products: list[Product]) -> list[str]:
    """Return queries evidenced by an exact normalized product author match."""
    normalized_authors = {
        normalized_search_text(author)
        for product in products
        for author in product.authors
    }
    matches: list[str] = []
    seen: set[str] = set()
    for query in queries:
        normalized = normalized_search_text(query)
        if normalized and normalized in normalized_authors and normalized not in seen:
            matches.append(query)
            seen.add(normalized)
    return matches


def _search_title(queries: list[str], category_name: str) -> str:
    if len(queries) > 1:
        combined_query = " | ".join(queries)
        title = f'Search: "{combined_query}"'
    elif queries[0]:
        title = f'Search: "{queries[0]}"'
    else:
        return f"Search: {category_name or 'All'}"
    if category_name:
        title += f" in {category_name}"
    return title


@click.command()
@click.argument("query", required=False, default="")
@click.option("--category", default="", help="Category ID to search within")
@filter_options(Scope.SEARCH)
@click.option(
    "--allow-large-scan",
    is_flag=True,
    help=f"Allow scans over {MAX_CATALOG_CALLS_PER_SCAN} catalog calls.",
)
@output_options(
    "output",
    "json",
    "quiet",
    "show_url",
    "profile",
    "dry_run",
    "refresh",
    validate_output_early=True,
)
@click.pass_context
def search(
    ctx,
    query,
    category,
    allow_large_scan,
    output,
    json_flag,
    quiet,
    show_url,
    profile_name,
    dry_run,
    refresh,
    **flags,
):
    """Search the Audible catalog by keyword."""
    logger.info(
        "search query=%r genre=%r category=%r max_price=%s pages=%s sort=%s deep=%s",
        query,
        flags["genre"],
        category,
        flags["max_price"],
        flags["pages"],
        flags["sort"],
        flags["deep"],
    )
    s = _resolve_scan_settings(ctx, profile_name, Scope.SEARCH, flags)
    if not query and not s.genre and not category:
        raise click.UsageError("Provide a QUERY or use --genre / --category to browse.")
    if dry_run and output:
        raise click.UsageError("--dry-run cannot be combined with --output/-o")
    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)
    effective_genre = _resolve_genre_category(ctx, s.genre, category)
    try:
        plan = build_search_scan_plan(query, sort=s.sort, deep=s.deep, pages=s.pages)
    except CatalogQueryError as exc:
        raise click.UsageError(str(exc)) from None
    queries = list(plan.queries)
    credit_price = _credit_price(ctx)
    cache = _scan_cache(ctx, refresh)
    cached_calls = _estimated_cached_calls(
        ctx, plan, cache, genre=effective_genre, category=category
    )

    if dry_run:
        requested_category = category or effective_genre
        category_label = (
            f"{requested_category} (resolved during scan)" if requested_category else ""
        )
        render_catalog_dry_run(
            CatalogDryRunSummary(
                plan=plan,
                category_name=category_label,
                query=" | ".join(queries),
                result_sort=s.sort,
                limit=s.limit,
                profile_name=profile_name,
                active_filters=tuple(active_filter_labels(s)),
                cached_calls=cached_calls,
            ),
            json_flag=json_flag,
            json_writer=click.echo,
        )
        return

    _enforce_catalog_call_limit(plan, allow_large_scan, cached_calls=cached_calls)
    dc = _get_client(ctx.obj["locale"])
    scan_report = ScanReport()

    with dc:
        category, category_name, exclude_category_ids = _resolve_categories(
            dc, effective_genre, category, s.exclude_genre
        )

        skip_asins, owned_snapshot, seen_snapshot, dismissed_snapshot = (
            _resolve_skip_snapshots(dc, s.skip_owned, s.exclude_seen, cache)
        )

        plan = bind_catalog_categories(plan, [category])
        description = (
            f"Searching '{queries[0]}'"
            if len(queries) == 1
            else f"Searching {len(queries)} queries"
        )
        with catalog_scan_progress(plan, description, disable=json_flag) as progress:
            all_products = execute_catalog_scan(
                dc,
                plan,
                progress,
                cache=cache,
                report=scan_report,
                allow_partial=True,
            )
    _report_interruption(scan_report, json_flag=json_flag)
    _report_cache_use(cache, json_flag=json_flag, quiet=quiet)

    cur = _currency(ctx)
    search_title = _search_title(queries, category_name)
    result = process_settings_discovery(
        SettingsFilterRequest(
            products=tuple(all_products),
            settings=s,
            skip_asins=skip_asins,
            exclude_category_ids=exclude_category_ids,
            credit_price=credit_price,
        )
    )
    publish_discovery(
        ResultPublicationRequest(
            result=result,
            title=search_title,
            limit=s.limit,
            output=output,
            json_flag=json_flag,
            quiet=quiet,
            max_price=s.max_price,
            currency=cur,
            interactive=s.interactive,
            show_url=show_url,
            credit_price=credit_price,
            candidates=tuple(all_products),
            cached_asins=cache.cached_asins() if cache is not None else frozenset(),
            session_spec=ResultSessionSpec(
                producer="search",
                locale=ctx.obj["locale"],
                recipe=recipe_from_settings(s),
                source={
                    "command": shlex.join(
                        [
                            "deals",
                            "search",
                            query,
                            *(["--category", category] if category else []),
                            "--pages",
                            str(s.pages),
                            *(["--deep"] if s.deep else []),
                        ]
                    ),
                    "query": query,
                    "category": category,
                    "genre": effective_genre,
                    "pages": s.pages,
                    "deep": s.deep,
                },
                constraints={
                    "drop_zero_length": True,
                    "always_skip_asins": sorted(dismissed_snapshot),
                    "owned_asins": sorted(owned_snapshot),
                    "owned_snapshot_available": s.skip_owned,
                    "seen_asins": sorted(seen_snapshot),
                    "seen_snapshot_available": True,
                    "excluded_category_ids": sorted(exclude_category_ids),
                    "category_snapshot_available": bool(s.exclude_genre),
                },
            ),
            json_writer=click.echo,
        )
    )
    if not s.author and not json_flag and not quiet:
        for author_query in _matching_author_queries(queries, all_products):
            console.print(
                f"\n  [dim]Tip: Use --author '{safe_markup(author_query)}' "
                "for exact author filtering.[/dim]"
            )
    if scan_report.interrupted:
        ctx.exit(130)


@click.command()
@click.option(
    "--category", default="", help="Category ID (use 'deals categories' to find IDs)"
)
@filter_options(Scope.FIND)
@click.option(
    "--subcategories/--no-subcategories",
    default=False,
    help="Scan each subcategory of the genre separately for deeper coverage (multiplies API calls)",
)
@click.option(
    "--allow-large-scan",
    is_flag=True,
    help=f"Allow scans over {MAX_CATALOG_CALLS_PER_SCAN} catalog calls.",
)
@output_options(
    "output",
    "json",
    "quiet",
    "show_url",
    "profile",
    "dry_run",
    "refresh",
    validate_output_early=True,
)
@click.pass_context
def find(
    ctx,
    category,
    subcategories,
    allow_large_scan,
    output,
    json_flag,
    quiet,
    show_url,
    profile_name,
    dry_run,
    refresh,
    **flags,
):
    """Find deals: browse the catalog filtered by price and genre.

    Scans multiple pages of the catalog, then filters client-side for
    items under your price threshold. Price and discount sorting happen
    after fetching since the Audible API doesn't support price sort.

    Use --deep to scan with multiple sort orders (BestSellers, newest,
    highest rated) for broader coverage at the cost of more API calls.

    \b
    Examples:
        deals find --genre "sci-fi" --max-price 5
        deals find --genre thriller --sort discount --on-sale --deep
        deals find --profile my-scifi
        deals find --author "Andy Weir" --max-price 10
        deals find --genre sci-fi --exclude-author "Sarah J. Maas" --max-price 5
        deals find --genre "sci-fi" --subcategories --max-price 5
    """
    logger.info(
        "find genre=%r category=%r keywords=%r max_price=%s pages=%s sort=%s deep=%s",
        flags["genre"],
        category,
        flags["keywords"],
        flags["max_price"],
        flags["pages"],
        flags["sort"],
        flags["deep"],
    )
    s = _resolve_scan_settings(ctx, profile_name, Scope.FIND, flags)
    if dry_run and output:
        raise click.UsageError("--dry-run cannot be combined with --output/-o")
    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)
    effective_genre = _resolve_genre_category(ctx, s.genre, category)

    if subcategories and not (category or effective_genre):
        raise click.UsageError("--subcategories requires --genre or --category")
    plan = build_find_scan_plan(
        s.keywords,
        category_ids=None if subcategories else ("",),
        sort=s.sort,
        deep=s.deep,
        pages=s.pages,
    )
    credit_price = _credit_price(ctx)
    cache = _scan_cache(ctx, refresh)
    cached_calls = (
        0
        if subcategories
        else _estimated_cached_calls(
            ctx, plan, cache, genre=effective_genre, category=category
        )
    )
    if dry_run:
        requested_category = category or effective_genre
        render_catalog_dry_run(
            CatalogDryRunSummary(
                plan=plan,
                category_name=f"{requested_category} (resolved during scan)"
                if requested_category
                else "",
                query=s.keywords,
                result_sort=s.sort,
                limit=s.limit,
                profile_name=profile_name,
                active_filters=tuple(active_filter_labels(s)),
                cached_calls=cached_calls,
            ),
            json_flag=json_flag,
            json_writer=click.echo,
        )
        return

    if not subcategories:
        _enforce_catalog_call_limit(plan, allow_large_scan, cached_calls=cached_calls)
    dc = _get_client(ctx.obj["locale"])
    scan_report = ScanReport()

    with dc:
        category, category_name, exclude_category_ids = _resolve_categories(
            dc, effective_genre, category, s.exclude_genre
        )

        child_ids: list[str] = []
        if subcategories and category:
            children = dc.get_categories(root=category)
            child_ids = [c["id"] for c in children if c.get("id")]

        skip_asins, owned_snapshot, seen_snapshot, dismissed_snapshot = (
            _resolve_skip_snapshots(dc, s.skip_owned, s.exclude_seen, cache)
        )

        desc_parts = []
        if s.keywords:
            desc_parts.append(f'"{s.keywords}"')
        if category:
            desc_parts.append(category_name or category)
        if not desc_parts:
            desc_parts.append("entire catalog")
        desc_str = ", ".join(desc_parts)

        if subcategories and child_ids:
            scan_category_ids = child_ids
            description = f"Scanning {desc_str} ({len(child_ids)} subcategories)"
        else:
            if subcategories:
                message = "No subcategories found; scanning the category directly."
                if json_flag:
                    click.echo(message, err=True)
                else:
                    console.print(f"[dim]{message}[/dim]")
            scan_category_ids = [category]
            description = f"Scanning {desc_str}"

        plan = bind_catalog_categories(plan, scan_category_ids)
        if subcategories:
            _enforce_catalog_call_limit(
                plan,
                allow_large_scan,
                cached_calls=estimate_cached_calls(plan, ctx.obj["locale"], cache),
            )
        with catalog_scan_progress(plan, description, disable=json_flag) as progress:
            all_products = execute_catalog_scan(
                dc,
                plan,
                progress,
                cache=cache,
                report=scan_report,
                allow_partial=True,
            )
    _report_interruption(scan_report, json_flag=json_flag)
    _report_cache_use(cache, json_flag=json_flag, quiet=quiet)

    cur = _currency(ctx)
    find_title = f"Deals under {cur}{s.max_price:.2f}"
    if category_name:
        find_title += f" in {category_name}"
    if s.keywords:
        find_title += f' matching "{s.keywords}"'
    result = process_settings_discovery(
        SettingsFilterRequest(
            products=tuple(all_products),
            settings=s,
            skip_asins=skip_asins,
            exclude_category_ids=exclude_category_ids,
            credit_price=credit_price,
        )
    )
    publish_discovery(
        ResultPublicationRequest(
            result=result,
            title=find_title,
            limit=s.limit,
            output=output,
            json_flag=json_flag,
            quiet=quiet,
            max_price=s.max_price,
            currency=cur,
            interactive=s.interactive,
            show_url=show_url,
            credit_price=credit_price,
            candidates=tuple(all_products),
            cached_asins=cache.cached_asins() if cache is not None else frozenset(),
            session_spec=ResultSessionSpec(
                producer="find",
                locale=ctx.obj["locale"],
                recipe=recipe_from_settings(s),
                source={
                    "command": shlex.join(
                        [
                            "deals",
                            "find",
                            *(["--category", category] if category else []),
                            *(["--keywords", s.keywords] if s.keywords else []),
                            "--pages",
                            str(s.pages),
                            *(["--deep"] if s.deep else []),
                            *(["--subcategories"] if subcategories else []),
                        ]
                    ),
                    "query": s.keywords,
                    "category": category,
                    "genre": effective_genre,
                    "pages": s.pages,
                    "deep": s.deep,
                    "subcategories": subcategories,
                },
                constraints={
                    "drop_zero_length": True,
                    "always_skip_asins": sorted(dismissed_snapshot),
                    "owned_asins": sorted(owned_snapshot),
                    "owned_snapshot_available": s.skip_owned,
                    "seen_asins": sorted(seen_snapshot),
                    "seen_snapshot_available": True,
                    "excluded_category_ids": sorted(exclude_category_ids),
                    "category_snapshot_available": bool(s.exclude_genre),
                },
            ),
            json_writer=click.echo,
        )
    )
    if scan_report.interrupted:
        ctx.exit(130)
