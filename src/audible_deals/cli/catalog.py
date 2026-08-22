"""Catalog search and deal-finding commands."""

from __future__ import annotations

import datetime
import dataclasses
import logging
import shlex

import click

from audible_deals.cli.helpers import (
    _CL,
    _credit_price,
    _currency,
    _get_client,
    _load_profile,
    _resolve_categories,
    _resolve_output_quiet,
    _resolve_skip_snapshots,
)
from audible_deals.cli.options import (
    _check_plus_flags,
    _common_filter_options,
    _complete_genre_names,
)
from audible_deals.constants import (
    CLIENT_SORT_OPTIONS,
    LOCALE_LANGUAGES,
    SORT_OPTIONS,
)
from audible_deals.catalog_workflow import (
    CatalogQueryError,
    bind_catalog_categories,
    build_find_scan_plan,
    build_search_scan_plan,
    execute_catalog_scan,
    normalized_search_text,
)
from audible_deals.presentation.terminal import catalog_scan_progress, console
from audible_deals.presentation.dry_run import (
    CatalogDryRunSummary,
    render_catalog_dry_run,
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
    resolve_settings,
)
from audible_deals.validation import NONNEGATIVE_FLOAT, NONNEGATIVE_INT

logger = logging.getLogger(__name__)


def _resolve_scan_settings(ctx, profile_name: str | None, **kwargs) -> Settings:
    explicit_options = {key for key in kwargs if ctx.get_parameter_source(key) == _CL}
    try:
        settings = resolve_settings(
            SettingsResolutionRequest(
                config=ctx.obj.get("config", {}),
                profile=_load_profile(profile_name),
                cli_flags=dict(kwargs),
                explicit_options=explicit_options,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    if not settings.language and not settings.all_languages:
        settings = dataclasses.replace(
            settings, language=LOCALE_LANGUAGES.get(ctx.obj["locale"], "")
        )
    if logger.isEnabledFor(logging.DEBUG):
        debug_keys = (
            "genre",
            "keywords",
            "max_price",
            "max_pph",
            "sort",
            "pages",
            "limit",
            "min_rating",
            "min_ratings",
            "min_hours",
            "min_discount",
            "language",
            "on_sale",
            "deep",
            "first_in_series",
            "skip_owned",
        )
        logger.debug(
            "resolved scan settings: %s",
            {key: getattr(settings, key) for key in debug_keys},
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


def _active_filter_labels(
    s,
    *,
    max_effective_price: float | None,
    exclude_seen: bool,
    hist_below: int | None,
    min_price_drop: float,
    require_history: bool,
    released_after: str,
    released_before: str,
) -> list[str]:
    """Return active resolved filters in a stable, human-readable order."""
    filters: list[str] = []

    def add(name: str, value, active: bool = True) -> None:
        if active:
            filters.append(f"{name}={value}")

    add("max-price", s.max_price, s.max_price is not None)
    add("max-price-per-hour", s.max_pph, s.max_pph is not None)
    add(
        "max-effective-price",
        max_effective_price,
        max_effective_price is not None,
    )
    add("min-rating", s.min_rating, s.min_rating > 0)
    add("min-ratings", s.min_ratings, s.min_ratings > 0)
    add("min-hours", s.min_hours, s.min_hours > 0)
    add("min-discount", s.min_discount, s.min_discount > 0)
    add("language", s.language, bool(s.language))
    add("author", s.author, bool(s.author))
    add("narrator", s.narrator, bool(s.narrator))
    add("series", s.series, bool(s.series))
    add("publisher", s.publisher, bool(s.publisher))
    add("on-sale", "yes", s.on_sale)
    add("first-in-series", "yes", s.first_in_series)
    add("skip-owned", "yes", s.skip_owned)
    add("skip-plus", "yes", s.skip_plus)
    add("only-plus", "yes", s.only_plus)
    add("exclude-genres", ",".join(s.exclude_genre), bool(s.exclude_genre))
    add("exclude-authors", ",".join(s.exclude_authors), bool(s.exclude_authors))
    add(
        "exclude-narrators",
        ",".join(s.exclude_narrators),
        bool(s.exclude_narrators),
    )
    add("exclude-keywords", ",".join(s.exclude_keywords), bool(s.exclude_keywords))
    add("exclude-seen", "yes", exclude_seen)
    add("hist-below", hist_below, hist_below is not None)
    add("min-price-drop", min_price_drop, min_price_drop > 0)
    add("require-history", "yes", require_history)
    add("released-after", released_after, bool(released_after))
    add("released-before", released_before, bool(released_before))
    return filters


def _validate_history_filter_options(
    require_history: bool,
    hist_below: int | None,
    min_price_drop: float,
    released_after: str,
    released_before: str,
) -> tuple[str, str]:
    """Validate history/date filter options; return normalized (released_after, released_before)."""
    if require_history and hist_below is None and not min_price_drop:
        raise click.UsageError(
            "--require-history requires --hist-below or --min-price-drop"
        )

    def _norm_date(opt: str, value: str) -> str:
        if not value:
            return value
        try:
            return datetime.date.fromisoformat(value).isoformat()
        except ValueError:
            raise click.UsageError(
                f"{opt}: invalid date {value!r} (expected YYYY-MM-DD)"
            )

    normalized_after = _norm_date("--released-after", released_after)
    normalized_before = _norm_date("--released-before", released_before)
    if normalized_after and normalized_before and normalized_after > normalized_before:
        raise click.UsageError(
            "--released-after cannot be later than --released-before"
        )
    return normalized_after, normalized_before


@click.command()
@click.argument("query", required=False, default="")
@click.option(
    "--max-price",
    type=NONNEGATIVE_FLOAT,
    default=None,
    help="Max price filter (e.g. 5.00)",
)
@click.option("--category", default="", help="Category ID to search within")
@click.option(
    "--genre",
    default="",
    help="Genre name to search within (fuzzy match, e.g. 'sci-fi')",
    shell_complete=_complete_genre_names,
)
@click.option(
    "--sort",
    type=click.Choice(list(SORT_OPTIONS.keys()) + sorted(CLIENT_SORT_OPTIONS)),
    default="relevance",
    help="Sort order (price/discount/price-per-hour/value are client-side)",
)
@click.option(
    "--min-ratings",
    type=NONNEGATIVE_INT,
    default=0,
    help="Minimum number of ratings (e.g. 100)",
)
@click.option(
    "--min-hours", type=NONNEGATIVE_FLOAT, default=0.0, help="Minimum length in hours"
)
@click.option(
    "--pages",
    type=click.IntRange(min=1),
    default=3,
    help="Number of pages to scan (50 items/page)",
)
@_common_filter_options
@click.pass_context
def search(
    ctx,
    query,
    max_price,
    max_pph,
    max_effective_price,
    category,
    genre,
    exclude_genre,
    sort,
    min_rating,
    min_ratings,
    min_hours,
    narrator,
    author,
    series,
    publisher,
    exclude_authors,
    exclude_narrators,
    on_sale,
    min_discount,
    deep,
    pages,
    language,
    all_languages,
    first_in_series,
    skip_owned,
    exclude_seen,
    limit,
    output,
    json_flag,
    quiet,
    show_url,
    interactive,
    profile_name,
    dry_run,
    skip_plus,
    only_plus,
    exclude_keywords,
    hist_below,
    min_price_drop,
    require_history,
    released_after,
    released_before,
):
    """Search the Audible catalog by keyword."""
    logger.info(
        "search query=%r genre=%r category=%r max_price=%s pages=%s sort=%s deep=%s",
        query,
        genre,
        category,
        max_price,
        pages,
        sort,
        deep,
    )
    released_after, released_before = _validate_history_filter_options(
        require_history, hist_below, min_price_drop, released_after, released_before
    )
    s = _resolve_scan_settings(
        ctx,
        profile_name,
        max_price=max_price,
        max_pph=max_pph,
        sort=sort,
        min_rating=min_rating,
        min_ratings=min_ratings,
        min_hours=min_hours,
        min_discount=min_discount,
        language=language,
        narrator=narrator,
        author=author,
        pages=pages,
        limit=limit,
        on_sale=on_sale,
        deep=deep,
        first_in_series=first_in_series,
        all_languages=all_languages,
        skip_owned=skip_owned,
        interactive=interactive,
        genre=genre,
        exclude_genre=exclude_genre,
        exclude_authors=exclude_authors,
        exclude_narrators=exclude_narrators,
        keywords="",
        series=series,
        publisher=publisher,
        skip_plus=skip_plus,
        only_plus=only_plus,
        exclude_keywords=exclude_keywords,
    )
    _check_plus_flags(s.skip_plus, s.only_plus)
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
                active_filters=tuple(
                    _active_filter_labels(
                        s,
                        max_effective_price=max_effective_price,
                        exclude_seen=exclude_seen,
                        hist_below=hist_below,
                        min_price_drop=min_price_drop,
                        require_history=require_history,
                        released_after=released_after,
                        released_before=released_before,
                    )
                ),
            ),
            json_flag=json_flag,
            json_writer=click.echo,
        )
        return

    dc = _get_client(ctx.obj["locale"])

    with dc:
        category, category_name, exclude_category_ids = _resolve_categories(
            dc, effective_genre, category, s.exclude_genre
        )

        skip_asins, owned_snapshot, seen_snapshot = _resolve_skip_snapshots(
            dc, s.skip_owned, exclude_seen
        )

        plan = bind_catalog_categories(plan, [category])
        description = (
            f"Searching '{queries[0]}'"
            if len(queries) == 1
            else f"Searching {len(queries)} queries"
        )
        with catalog_scan_progress(plan, description, disable=json_flag) as progress:
            all_products = execute_catalog_scan(dc, plan, progress)

    cur = _currency(ctx)
    search_title = _search_title(queries, category_name)
    result = process_settings_discovery(
        SettingsFilterRequest(
            products=tuple(all_products),
            settings=s,
            skip_asins=skip_asins,
            exclude_category_ids=exclude_category_ids,
            hist_below=hist_below,
            min_price_drop=min_price_drop,
            require_history=require_history,
            released_after=released_after,
            released_before=released_before,
            max_effective_price=max_effective_price,
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
            session_spec=ResultSessionSpec(
                producer="search",
                locale=ctx.obj["locale"],
                recipe=recipe_from_settings(
                    s,
                    max_effective_price=max_effective_price,
                    hist_below=hist_below,
                    min_price_drop=min_price_drop,
                    require_history=require_history,
                    released_after=released_after,
                    released_before=released_before,
                    exclude_seen=exclude_seen,
                    exclude_genres=s.exclude_genre,
                ),
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
                f"\n  [dim]Tip: Use --author '{author_query}' for exact author filtering.[/dim]"
            )


@click.command()
@click.option(
    "--category", default="", help="Category ID (use 'deals categories' to find IDs)"
)
@click.option(
    "--genre",
    default="",
    help="Genre name (fuzzy match, e.g. 'sci-fi', 'mystery', 'romance')",
    shell_complete=_complete_genre_names,
)
@click.option(
    "--keywords", default="", help="Optional keyword filter within the category"
)
@click.option(
    "--max-price",
    type=NONNEGATIVE_FLOAT,
    default=5.00,
    help="Max price threshold (default: $5.00)",
)
@click.option(
    "--sort",
    type=click.Choice(sorted(CLIENT_SORT_OPTIONS) + list(SORT_OPTIONS.keys())),
    default="price-per-hour",
    help="Sort order (price/discount/price-per-hour/value are client-side)",
)
@click.option(
    "--min-ratings",
    type=NONNEGATIVE_INT,
    default=1,
    help="Minimum number of ratings (default: 1, filters unreviewed)",
)
@click.option(
    "--min-hours",
    type=NONNEGATIVE_FLOAT,
    default=0.0,
    help="Minimum length in hours (filters out shorts)",
)
@click.option(
    "--pages",
    type=click.IntRange(min=1),
    default=10,
    help="Pages to scan per sort order (50 items/page, default: 10)",
)
@click.option(
    "--subcategories/--no-subcategories",
    default=False,
    help="Scan each subcategory of the genre separately for deeper coverage (multiplies API calls)",
)
@_common_filter_options
@click.pass_context
def find(
    ctx,
    category,
    genre,
    exclude_genre,
    keywords,
    max_price,
    max_pph,
    max_effective_price,
    sort,
    min_rating,
    min_ratings,
    min_hours,
    narrator,
    author,
    series,
    publisher,
    exclude_authors,
    exclude_narrators,
    on_sale,
    min_discount,
    deep,
    pages,
    subcategories,
    language,
    all_languages,
    first_in_series,
    skip_owned,
    exclude_seen,
    limit,
    output,
    json_flag,
    quiet,
    show_url,
    profile_name,
    interactive,
    dry_run,
    skip_plus,
    only_plus,
    exclude_keywords,
    hist_below,
    min_price_drop,
    require_history,
    released_after,
    released_before,
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
        genre,
        category,
        keywords,
        max_price,
        pages,
        sort,
        deep,
    )
    released_after, released_before = _validate_history_filter_options(
        require_history, hist_below, min_price_drop, released_after, released_before
    )
    s = _resolve_scan_settings(
        ctx,
        profile_name,
        max_price=max_price,
        max_pph=max_pph,
        sort=sort,
        min_rating=min_rating,
        min_ratings=min_ratings,
        min_hours=min_hours,
        min_discount=min_discount,
        language=language,
        narrator=narrator,
        author=author,
        pages=pages,
        limit=limit,
        on_sale=on_sale,
        deep=deep,
        first_in_series=first_in_series,
        all_languages=all_languages,
        skip_owned=skip_owned,
        interactive=interactive,
        genre=genre,
        exclude_genre=exclude_genre,
        exclude_authors=exclude_authors,
        exclude_narrators=exclude_narrators,
        keywords=keywords,
        series=series,
        publisher=publisher,
        skip_plus=skip_plus,
        only_plus=only_plus,
        exclude_keywords=exclude_keywords,
    )
    _check_plus_flags(s.skip_plus, s.only_plus)
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
                active_filters=tuple(
                    _active_filter_labels(
                        s,
                        max_effective_price=max_effective_price,
                        exclude_seen=exclude_seen,
                        hist_below=hist_below,
                        min_price_drop=min_price_drop,
                        require_history=require_history,
                        released_after=released_after,
                        released_before=released_before,
                    )
                ),
            ),
            json_flag=json_flag,
            json_writer=click.echo,
        )
        return

    dc = _get_client(ctx.obj["locale"])

    with dc:
        category, category_name, exclude_category_ids = _resolve_categories(
            dc, effective_genre, category, s.exclude_genre
        )

        child_ids: list[str] = []
        if subcategories and category:
            children = dc.get_categories(root=category)
            child_ids = [c["id"] for c in children if c.get("id")]

        skip_asins, owned_snapshot, seen_snapshot = _resolve_skip_snapshots(
            dc, s.skip_owned, exclude_seen
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
        with catalog_scan_progress(plan, description, disable=json_flag) as progress:
            all_products = execute_catalog_scan(dc, plan, progress)

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
            hist_below=hist_below,
            min_price_drop=min_price_drop,
            require_history=require_history,
            released_after=released_after,
            released_before=released_before,
            max_effective_price=max_effective_price,
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
            session_spec=ResultSessionSpec(
                producer="find",
                locale=ctx.obj["locale"],
                recipe=recipe_from_settings(
                    s,
                    max_effective_price=max_effective_price,
                    hist_below=hist_below,
                    min_price_drop=min_price_drop,
                    require_history=require_history,
                    released_after=released_after,
                    released_before=released_before,
                    exclude_seen=exclude_seen,
                    exclude_genres=s.exclude_genre,
                ),
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
