"""Catalog scan commands: search, find, library, series, last."""

from __future__ import annotations

import datetime
import json as json_mod
import logging
import time
from pathlib import Path

import click

from audible_deals.cli.helpers import (
    _CL,
    _currency,
    _get_client,
    _resolve_categories,
    _resolve_output_quiet,
    _resolve_skip_asins,
    _safe_record_prices,
)
from audible_deals.cli.options import _common_filter_options, _complete_genre_names
from audible_deals.cli.pipeline import (
    _apply_filters,
    _apply_settings_filters,
    _build_scan_settings,
    _emit_output,
    _fetch_with_progress,
    _print_dry_run_summary,
    _record_and_cache,
)
from audible_deals.client import Product
from audible_deals.constants import (
    CLIENT_SORT_OPTIONS,
    DEEP_SORT_ORDERS,
    SORT_OPTIONS,
)
from audible_deals.display import (
    console,
    create_scan_progress,
    display_library_stats,
    display_products,
    display_series_gaps,
    display_summary,
)
from audible_deals.filtering import filter_products, sort_local
from audible_deals.parsing import parse_series_position
from audible_deals.price_history import price_history_context
from audible_deals.results_cache import (
    clear_last_results,
    clear_seen_asins,
    load_last_results,
)
from audible_deals.serialization import (
    deserialize_product,
    export_products,
    serialize_product,
)
from audible_deals.settings import Settings
from audible_deals.validation import looks_like_person_name

logger = logging.getLogger(__name__)


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

    return (
        _norm_date("--released-after", released_after),
        _norm_date("--released-before", released_before),
    )


@click.command()
@click.argument("query", required=False, default="")
@click.option(
    "--max-price",
    type=click.FloatRange(min=0),
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
    "--min-ratings", type=int, default=0, help="Minimum number of ratings (e.g. 100)"
)
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours")
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
    if not query and not genre and not category:
        raise click.UsageError("Provide a QUERY or use --genre / --category to browse.")
    if skip_plus and only_plus:
        raise click.UsageError("--skip-plus and --only-plus are mutually exclusive")
    released_after, released_before = _validate_history_filter_options(
        require_history, hist_below, min_price_drop, released_after, released_before
    )
    s = _build_scan_settings(
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
    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)
    if s.genre and category:
        raise click.UsageError("Use --genre or --category, not both.")

    dc = _get_client(ctx.obj["locale"])
    server_sort = SORT_OPTIONS.get(s.sort, "Relevance")
    sort_orders = DEEP_SORT_ORDERS if s.deep else [server_sort]

    with dc:
        category, category_name, exclude_category_ids = _resolve_categories(
            dc, s.genre, category, s.exclude_genre
        )

        if dry_run:
            _print_dry_run_summary(
                category_name=category_name,
                query=query,
                sort_orders=sort_orders,
                pages=s.pages,
            )
            return

        skip_asins = _resolve_skip_asins(dc, s.skip_owned, exclude_seen)

        queries = (
            [q.strip() for q in query.split("|") if q.strip()]
            if "|" in query
            else [query]
        )
        if not queries:
            raise click.UsageError("No keywords found after splitting on '|'.")

        if len(queries) > 1:
            all_products: list[Product] = []
            fetched_asins: set[str] = set()
            for q in queries:
                sub_products = _fetch_with_progress(
                    dc,
                    keywords=q,
                    category_ids=[category],
                    sort_orders=sort_orders,
                    pages=s.pages,
                    description=f"Searching '{q}'",
                )
                for p in sub_products:
                    if p.asin not in fetched_asins:
                        fetched_asins.add(p.asin)
                        all_products.append(p)
            scope = " | ".join(f"'{q}'" for q in queries)
            if category_name:
                scope += f" in {category_name}"
        else:
            if queries[0]:
                scope = f"'{queries[0]}'"
                if category_name:
                    scope += f" in {category_name}"
            elif category_name:
                scope = category_name
            else:
                scope = "catalog"

            all_products = _fetch_with_progress(
                dc,
                keywords=queries[0],
                category_ids=[category],
                sort_orders=sort_orders,
                pages=s.pages,
                description=f"Searching {scope}",
            )

    cur = _currency(ctx)
    if len(queries) > 1:
        combined_query = " | ".join(queries)
        search_title = f'Search: "{combined_query}"'
        if category_name:
            search_title += f" in {category_name}"
    elif queries[0]:
        search_title = f'Search: "{queries[0]}"'
        if category_name:
            search_title += f" in {category_name}"
    else:
        search_title = f"Search: {category_name or 'All'}"
    filtered, filter_breakdown, editions_removed, series_collapsed = (
        _apply_settings_filters(
            all_products,
            s,
            skip_asins=skip_asins,
            exclude_category_ids=exclude_category_ids,
            hist_below=hist_below,
            min_price_drop=min_price_drop,
            require_history=require_history,
            released_after=released_after,
            released_before=released_before,
        )
    )
    filtered, serialized, total_before_limit = _record_and_cache(
        filtered,
        title=search_title,
        limit=s.limit,
    )
    _emit_output(
        filtered,
        serialized,
        title=search_title,
        output=output,
        json_flag=json_flag,
        quiet=quiet,
        max_price=s.max_price,
        filter_breakdown=filter_breakdown,
        editions_removed=editions_removed,
        series_collapsed=series_collapsed,
        total_before_limit=total_before_limit,
        currency=cur,
        interactive=s.interactive,
        show_url=show_url,
    )
    display_query = queries[0] if len(queries) == 1 else None
    if (
        display_query
        and not s.author
        and not json_flag
        and not quiet
        and looks_like_person_name(display_query)
    ):
        console.print(
            f"\n  [dim]Tip: Use --author '{display_query}' for exact author filtering.[/dim]"
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
    type=click.FloatRange(min=0),
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
    type=int,
    default=1,
    help="Minimum number of ratings (default: 1, filters unreviewed)",
)
@click.option(
    "--min-hours",
    type=float,
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
    if skip_plus and only_plus:
        raise click.UsageError("--skip-plus and --only-plus are mutually exclusive")
    released_after, released_before = _validate_history_filter_options(
        require_history, hist_below, min_price_drop, released_after, released_before
    )
    s = _build_scan_settings(
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
    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)
    if s.genre and category:
        raise click.UsageError("Use --genre or --category, not both.")

    dc = _get_client(ctx.obj["locale"])
    server_sort = SORT_OPTIONS.get(s.sort, "BestSellers")
    sort_orders = DEEP_SORT_ORDERS if s.deep else [server_sort]

    with dc:
        category, category_name, exclude_category_ids = _resolve_categories(
            dc, s.genre, category, s.exclude_genre
        )

        if subcategories and not category:
            raise click.UsageError("--subcategories requires --genre or --category")

        child_ids: list[str] = []
        if subcategories and category:
            children = dc.get_categories(root=category)
            child_ids = [c["id"] for c in children if c.get("id")]

        if dry_run:
            sub_count = len(child_ids) if subcategories and child_ids else None
            _print_dry_run_summary(
                category_name=category_name,
                query=s.keywords,
                sort_orders=sort_orders,
                pages=s.pages,
                subcategory_count=sub_count,
            )
            return

        skip_asins = _resolve_skip_asins(dc, s.skip_owned, exclude_seen)

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
                console.print(
                    "[dim]No subcategories found; scanning the category directly.[/dim]"
                )
            scan_category_ids = [category]
            description = f"Scanning {desc_str}"

        all_products = _fetch_with_progress(
            dc,
            keywords=s.keywords,
            category_ids=scan_category_ids,
            sort_orders=sort_orders,
            pages=s.pages,
            description=description,
        )

    cur = _currency(ctx)
    find_title = f"Deals under {cur}{s.max_price:.2f}"
    if category_name:
        find_title += f" in {category_name}"
    if s.keywords:
        find_title += f' matching "{s.keywords}"'
    filtered, filter_breakdown, editions_removed, series_collapsed = (
        _apply_settings_filters(
            all_products,
            s,
            skip_asins=skip_asins,
            exclude_category_ids=exclude_category_ids,
            hist_below=hist_below,
            min_price_drop=min_price_drop,
            require_history=require_history,
            released_after=released_after,
            released_before=released_before,
        )
    )
    filtered, serialized, total_before_limit = _record_and_cache(
        filtered,
        title=find_title,
        limit=s.limit,
    )
    _emit_output(
        filtered,
        serialized,
        title=find_title,
        output=output,
        json_flag=json_flag,
        quiet=quiet,
        max_price=s.max_price,
        filter_breakdown=filter_breakdown,
        editions_removed=editions_removed,
        series_collapsed=series_collapsed,
        total_before_limit=total_before_limit,
        currency=cur,
        interactive=s.interactive,
        show_url=show_url,
    )


@click.command()
@click.option(
    "--sort",
    type=click.Choice(
        ["title", "rating", "length", "date", "price", "-price", "price-per-hour"]
    ),
    default="date",
    help="Sort order (default: date — newest first)",
)
@click.option(
    "-n",
    "--limit",
    type=click.IntRange(min=0),
    default=None,
    help="Show only the top N results",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Export to file (.json or .csv)",
)
@click.option(
    "--json", "json_flag", is_flag=True, default=False, help="Output as JSON to stdout"
)
@click.option(
    "-q", "--quiet", is_flag=True, default=False, help="Suppress table output"
)
@click.option("--author", default="", help="Filter by author name (substring match)")
@click.option(
    "--narrator",
    default="",
    help="Filter by narrator name (substring match, client-side)",
)
@click.option(
    "--genre",
    default="",
    help="Filter by genre/category (substring match on categories)",
)
@click.option("--min-rating", type=float, default=0.0, help="Minimum rating")
@click.option("--min-ratings", type=int, default=0, help="Minimum number of ratings")
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours")
@click.option(
    "--stats",
    is_flag=True,
    default=False,
    help="Show aggregate library statistics instead of the table",
)
@click.pass_context
def library(
    ctx,
    sort,
    limit,
    output,
    json_flag,
    quiet,
    author,
    narrator,
    genre,
    min_rating,
    min_ratings,
    min_hours,
    stats,
):
    """List all audiobooks in your Audible library.

    Fetches your full library with metadata — useful for exporting to
    a file for analysis or feeding to other tools.

    \b
    Examples:
        deals library
        deals library --json > my-books.json
        deals library -o library.csv
        deals library --sort rating -n 20
        deals library --author "Andy Weir"
        deals library --genre sci-fi --min-rating 4.0
    """
    logger.info(
        "library sort=%s limit=%s author=%r narrator=%r genre=%r",
        sort,
        limit,
        author,
        narrator,
        genre,
    )
    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)

    dc = _get_client(ctx.obj["locale"])
    all_products: list[Product] = []
    with dc:
        with create_scan_progress() as progress:
            task = progress.add_task("Fetching library", total=None, items=0)
            page_count = 0
            for page_products, page_num in dc.get_library_pages():
                all_products.extend(page_products)
                page_count = page_num
                progress.update(task, completed=page_num, items=len(all_products))
            progress.update(task, total=page_count, completed=page_count)

    filtered, filter_breakdown = filter_products(
        all_products,
        author=author,
        narrator=narrator,
        min_rating=min_rating,
        min_ratings=min_ratings,
        min_hours=min_hours,
        genre=genre,
    )

    filtered = sort_local(filtered, sort)
    stats_products = filtered  # stats always use the full filtered list
    total_before_limit = len(filtered)
    if limit is not None and limit > 0:
        filtered = filtered[:limit]

    cur = _currency(ctx)

    if output:
        export_products(filtered, output)
        console.print(f"[green]Exported {len(filtered)} items to {output}[/green]")
    if json_flag:
        serialized = [serialize_product(p) for p in filtered]
        click.echo(json_mod.dumps(serialized, indent=2, ensure_ascii=False))
    if not json_flag and not quiet:
        console.print()
        if stats:
            display_library_stats(stats_products, cur)
        else:
            title = "Your Library"
            display_products(filtered, title=title, currency=cur)
            if filter_breakdown:
                display_summary(
                    len(filtered),
                    filter_breakdown,
                    currency=cur,
                    total_before_limit=total_before_limit,
                    noun="books",
                )
            elif total_before_limit > len(filtered):
                console.print(
                    f"  [bold]{len(filtered)}[/bold] of {total_before_limit} books shown"
                )
            else:
                console.print(f"  [bold]{len(filtered)}[/bold] books in library")


def _series_gaps_report(
    filtered: list[Product],
    invested_sorted: list[tuple[str, list[Product]]],
    candidate_series: dict[str, str],
    *,
    json_flag: bool,
    quiet: bool,
    currency: str,
) -> None:
    """Emit the per-series gap report (JSON or table) from filtered candidates."""
    by_series: dict[str, list[Product]] = {}
    for p in filtered:
        sname = candidate_series.get(p.asin, "")
        if sname:
            by_series.setdefault(sname, []).append(p)

    atl_asins, _ = price_history_context(filtered)

    gaps: list[dict] = []
    for sname, books in sorted(invested_sorted, key=lambda x: x[0]):
        missing_products = by_series.get(sname, [])
        if not missing_products:
            continue
        missing_products.sort(key=lambda p: parse_series_position(p.series_position))
        missing_entries = [
            {
                "asin": p.asin,
                "title": p.title,
                "position": p.series_position or "",
                "price": p.price,
                "atl": p.asin in atl_asins,
            }
            for p in missing_products
        ]
        gaps.append(
            {
                "series": sname,
                "owned": len(books),
                "total_known": len(books) + len(missing_entries),
                "missing": missing_entries,
            }
        )

    if json_flag:
        stripped = [
            {
                **g,
                "missing": [
                    {k: v for k, v in m.items() if k != "atl"} for m in g["missing"]
                ],
            }
            for g in gaps
        ]
        click.echo(json_mod.dumps(stripped, indent=2, ensure_ascii=False))
    elif not quiet:
        display_series_gaps(gaps, currency=currency)


@click.command()
@click.option(
    "--min-books",
    type=click.IntRange(min=1),
    default=2,
    help="Minimum books owned in a series to consider it 'invested' (default: 2)",
)
@click.option(
    "--max-series",
    type=click.IntRange(min=1),
    default=20,
    help="Maximum number of series to scan (default: 20, most-invested first)",
)
@click.option(
    "--series",
    "series_filter",
    default="",
    help="Filter to a specific series name (substring match)",
)
@click.option(
    "--max-price", type=click.FloatRange(min=0), default=None, help="Max price filter"
)
@click.option("--min-rating", type=float, default=0.0, help="Minimum rating (e.g. 4.0)")
@click.option("--min-ratings", type=int, default=0, help="Minimum number of ratings")
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours")
@click.option(
    "--on-sale", is_flag=True, default=False, help="Only show discounted items"
)
@click.option(
    "--sort",
    type=click.Choice(
        [
            "price",
            "-price",
            "discount",
            "price-per-hour",
            "rating",
            "length",
            "date",
            "title",
        ]
    ),
    default="price-per-hour",
    help="Sort order (default: price-per-hour)",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(min=0),
    default=25,
    help="Show only the top N results (0 for unlimited, default: 25)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Export results to file (.json or .csv)",
)
@click.option(
    "--json",
    "json_flag",
    is_flag=True,
    default=False,
    help="Output results as JSON to stdout",
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    default=False,
    help="Suppress table output (useful with --output)",
)
@click.option(
    "--interactive",
    "-i",
    is_flag=True,
    default=False,
    help="Browse results interactively",
)
@click.option(
    "--pages",
    type=click.IntRange(min=1),
    default=3,
    help="Pages to scan per series search (default: 3)",
)
@click.option(
    "--gaps",
    "gaps_mode",
    is_flag=True,
    default=False,
    help="Show missing books per series instead of a flat deals table (price/rating filters apply to missing books)",
)
@click.pass_context
def series(
    ctx,
    min_books,
    max_series,
    series_filter,
    max_price,
    min_rating,
    min_ratings,
    min_hours,
    on_sale,
    sort,
    limit,
    output,
    json_flag,
    quiet,
    interactive,
    pages,
    gaps_mode,
):
    """Find continuation books in series you're invested in.

    Scans your library for series where you own multiple books, then
    searches the catalog for other books in those series that you don't
    own yet. Great for catching up on series during sales.

    \b
    Examples:
        deals series
        deals series --min-books 3 --max-price 10
        deals series --series "Expeditionary Force" --on-sale
        deals series --sort discount -n 50
        deals series --json -o series-deals.json
        deals series --gaps
        deals series --gaps --json
    """
    if gaps_mode and output:
        raise click.UsageError("--gaps is not compatible with --output/-o")
    if gaps_mode and interactive:
        raise click.UsageError("--gaps is not compatible with --interactive/-i")
    if gaps_mode and ctx.get_parameter_source("limit") == _CL:
        raise click.UsageError("--limit/-n is ignored in --gaps mode")
    if gaps_mode and ctx.get_parameter_source("sort") == _CL:
        raise click.UsageError("--sort is ignored in --gaps mode")

    logger.info(
        "series min_books=%s max_series=%s filter=%r max_price=%s sort=%s gaps=%s",
        min_books,
        max_series,
        series_filter,
        max_price,
        sort,
        gaps_mode,
    )
    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)

    s = Settings.resolve(
        ctx,
        config=ctx.obj.get("config", {}),
        profile=None,
        cli_flags=dict(
            max_price=max_price,
            min_rating=min_rating,
            min_ratings=min_ratings,
            min_hours=min_hours,
            on_sale=on_sale,
            limit=limit,
            sort=sort,
            pages=pages,
        ),
    )
    max_price, min_rating, min_ratings = s.max_price, s.min_rating, s.min_ratings
    min_hours, on_sale, limit = s.min_hours, s.on_sale, s.limit
    sort, pages = s.sort, s.pages

    dc = _get_client(ctx.obj["locale"])
    cur = _currency(ctx)

    with dc:
        # 1. Fetch library
        if not quiet and not json_flag:
            console.print("[dim]Fetching library...[/dim]")
        lib_products = dc.get_library()
        owned_asins = {p.asin for p in lib_products}

        # 2. Identify invested series (user owns min_books+ books)
        series_map: dict[str, list[Product]] = {}  # series_name -> [products]
        for p in lib_products:
            if not p.series_name:
                continue
            series_map.setdefault(p.series_name, []).append(p)

        invested = {
            name: books for name, books in series_map.items() if len(books) >= min_books
        }

        if series_filter:
            filter_lower = series_filter.lower()
            invested = {
                name: books
                for name, books in invested.items()
                if filter_lower in name.lower()
            }

        if not invested:
            if series_filter:
                console.print(
                    f"[dim]No invested series matching '{series_filter}' "
                    f"(need {min_books}+ owned books).[/dim]"
                )
            else:
                console.print(
                    f"[dim]No series with {min_books}+ owned books found.[/dim]"
                )
            return

        # Sort by most-invested (most owned books) first, then limit
        invested_sorted = sorted(
            invested.items(), key=lambda x: len(x[1]), reverse=True
        )
        if len(invested_sorted) > max_series:
            if not quiet and not json_flag:
                console.print(
                    f"[dim]Found {len(invested_sorted)} invested series, scanning top {max_series} (use --max-series to adjust).[/dim]"
                )
            invested_sorted = invested_sorted[:max_series]
        elif not quiet and not json_flag:
            console.print(
                f"[dim]Found {len(invested_sorted)} invested series. Searching for continuation books...[/dim]"
            )

        # 3. Fetch catalog entries for each series
        all_candidates: list[Product] = []
        candidate_series: dict[str, str] = {}  # asin -> series_name
        seen_asins: set[str] = set(owned_asins)

        with create_scan_progress() as progress:
            task = progress.add_task(
                f"Scanning {len(invested_sorted)} series",
                total=len(invested_sorted),
                items=0,
            )

            for series_idx, (sname, owned_books) in enumerate(invested_sorted):
                series_asin = next(
                    (ob.series_asin for ob in owned_books if ob.series_asin), ""
                )

                if series_asin:
                    # Direct lookup via series ASIN
                    series_products = dc.get_series_products(series_asin)
                else:
                    # Fallback: keyword search when no series ASIN available
                    series_products = []
                    author_hint = next(
                        (ob.authors[0] for ob in owned_books if ob.authors), ""
                    )
                    keywords = f"{sname} {author_hint}".strip()
                    sname_lower = sname.lower()
                    for page_products, _, _ in dc.search_pages(
                        keywords=keywords,
                        sort_by="Relevance",
                        max_pages=pages,
                    ):
                        for p in page_products:
                            if p.series_name and p.series_name.lower() == sname_lower:
                                series_products.append(p)

                for p in series_products:
                    if p.asin in seen_asins:
                        continue
                    seen_asins.add(p.asin)
                    all_candidates.append(p)
                    candidate_series[p.asin] = sname

                progress.update(
                    task, completed=series_idx + 1, items=len(all_candidates)
                )

                # Rate limit between series lookups
                if series_idx < len(invested_sorted) - 1:
                    time.sleep(0.3)

    # 4. Post-process using shared pipeline
    series_title = f"Series Continuation Books ({len(invested_sorted)} series)"
    filtered, filter_breakdown, editions_removed, series_collapsed = _apply_filters(
        all_candidates,
        max_price=max_price,
        min_rating=min_rating,
        min_ratings=min_ratings,
        min_hours=min_hours,
        narrator="",
        author="",
        exclude_authors=(),
        exclude_narrators=(),
        language="",
        on_sale=on_sale,
        skip_asins=None,
        exclude_category_ids=set(),
        first_in_series_only=False,
        sort=sort,
        drop_zero_length=False,
    )

    if gaps_mode:
        # Record prices so history keeps accruing, but skip cache/limit/table pipeline.
        _safe_record_prices(filtered)
        _series_gaps_report(
            filtered,
            invested_sorted,
            candidate_series,
            json_flag=json_flag,
            quiet=quiet,
            currency=cur,
        )
        return

    filtered, serialized, total_before_limit = _record_and_cache(
        filtered,
        title=series_title,
        limit=limit,
    )
    _emit_output(
        filtered,
        serialized,
        title=series_title,
        output=output,
        json_flag=json_flag,
        quiet=quiet,
        max_price=max_price,
        filter_breakdown=filter_breakdown,
        editions_removed=editions_removed,
        series_collapsed=series_collapsed,
        total_before_limit=total_before_limit,
        currency=cur,
        interactive=interactive,
    )


@click.command("last")
@click.option(
    "--sort",
    type=click.Choice(
        [
            "price",
            "-price",
            "discount",
            "price-per-hour",
            "value",
            "rating",
            "length",
            "date",
            "relevance",
        ]
    ),
    default=None,
    help="Re-sort results",
)
@click.option(
    "--max-price", type=click.FloatRange(min=0), default=None, help="Max price filter"
)
@click.option(
    "--max-price-per-hour",
    "max_pph",
    type=click.FloatRange(min=0),
    default=None,
    help="Max price per hour (e.g. 0.50)",
)
@click.option("--min-rating", type=float, default=0.0, help="Minimum rating")
@click.option("--min-ratings", type=int, default=0, help="Minimum number of ratings")
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours")
@click.option(
    "--narrator",
    default="",
    help="Filter by narrator name (substring match, client-side)",
)
@click.option("--author", default="", help="Filter by author name (substring match)")
@click.option("--series", default="", help="Filter by series name (substring match)")
@click.option(
    "--publisher", default="", help="Filter by publisher name (substring match)"
)
@click.option(
    "--exclude-author",
    "exclude_authors",
    multiple=True,
    help="Exclude author (substring match, repeatable)",
)
@click.option(
    "--exclude-narrator",
    "exclude_narrators",
    multiple=True,
    help="Exclude narrator (substring match, repeatable)",
)
@click.option("--language", default="", help="Language filter")
@click.option(
    "--on-sale", is_flag=True, default=False, help="Only show discounted items"
)
@click.option(
    "--min-discount",
    type=click.IntRange(min=0, max=100),
    default=0,
    help="Minimum discount percentage (e.g. 70)",
)
@click.option(
    "--first-in-series",
    is_flag=True,
    default=False,
    help="Show only first book per series",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(min=0),
    default=None,
    help="Show only the top N results",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Export results to file (.json or .csv)",
)
@click.option(
    "--json",
    "json_flag",
    is_flag=True,
    default=False,
    help="Output results as JSON to stdout",
)
@click.option(
    "--quiet", "-q", is_flag=True, default=False, help="Suppress table output"
)
@click.option(
    "--show-url",
    is_flag=True,
    default=False,
    help="Show Audible URL for each item in the table",
)
@click.option(
    "--interactive",
    "-i",
    is_flag=True,
    default=False,
    help="Browse results interactively",
)
@click.option(
    "--clear", is_flag=True, default=False, help="Delete the cached results and exit"
)
@click.option(
    "--clear-seen",
    is_flag=True,
    default=False,
    help="Clear the cumulative seen-ASINs list and exit",
)
@click.option(
    "--count",
    "count_only",
    is_flag=True,
    default=False,
    help="Show total cached result count (ignores filters)",
)
@click.option(
    "--skip-plus/--no-skip-plus",
    default=False,
    help="Exclude Audible Plus catalog titles",
)
@click.option(
    "--only-plus/--no-only-plus",
    default=False,
    help="Show only Audible Plus catalog titles",
)
@click.option(
    "--exclude-keyword",
    "exclude_keywords",
    multiple=True,
    help="Drop results with title/subtitle matching keyword (repeatable)",
)
@click.pass_context
def last_cmd(
    ctx,
    sort,
    max_price,
    max_pph,
    min_rating,
    min_ratings,
    min_hours,
    narrator,
    author,
    series,
    publisher,
    exclude_authors,
    exclude_narrators,
    language,
    on_sale,
    min_discount,
    first_in_series,
    limit,
    output,
    json_flag,
    quiet,
    show_url,
    interactive,
    clear,
    clear_seen,
    count_only,
    skip_plus,
    only_plus,
    exclude_keywords,
):
    """Re-display results from the last search or find, with optional re-filtering.

    No API calls are made — results are read from the local cache.

    \b
    Examples:
        deals last
        deals last --sort discount
        deals last --max-price 3 --min-rating 4
        deals last --narrator "R.C. Bray" --min-ratings 100
        deals last --author "Andy Weir"
        deals last --clear
        deals last --clear-seen
    """
    logger.info(
        "last sort=%s max_price=%s clear=%s clear_seen=%s count=%s",
        sort,
        max_price,
        clear,
        clear_seen,
        count_only,
    )
    if skip_plus and only_plus:
        raise click.UsageError("--skip-plus and --only-plus are mutually exclusive")
    did_clear = False
    if clear_seen:
        if clear_seen_asins():
            console.print("[green]Seen ASINs list cleared.[/green]")
        else:
            console.print("[dim]No seen ASINs to clear.[/dim]")
        did_clear = True
    if clear:
        if clear_last_results():
            console.print("[green]Last results cache cleared.[/green]")
        else:
            console.print("[dim]No cached results to clear.[/dim]")
        did_clear = True
    if did_clear:
        return
    if count_only:
        cached_title, data = load_last_results()
        click.echo(len(data))
        return
    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)
    cached_title, data = load_last_results()
    products = [p for d in data if (p := deserialize_product(d)) is not None]

    effective_sort = sort or ""  # preserve original cache order when no --sort given
    cur = _currency(ctx)
    filtered, filter_breakdown, editions_removed, series_collapsed = _apply_filters(
        products,
        max_price=max_price,
        min_rating=min_rating,
        min_ratings=min_ratings,
        min_hours=min_hours,
        narrator=narrator,
        author=author,
        exclude_authors=exclude_authors,
        exclude_narrators=exclude_narrators,
        language=language,
        on_sale=on_sale,
        skip_asins=None,
        exclude_category_ids=set(),
        first_in_series_only=first_in_series,
        sort=effective_sort,
        max_pph=max_pph,
        min_discount=min_discount,
        series=series,
        publisher=publisher,
        skip_plus=skip_plus,
        only_plus=only_plus,
        exclude_keywords=exclude_keywords,
    )
    filtered, serialized, total_before_limit = _record_and_cache(
        filtered,
        title=cached_title,
        write_cache=False,
        limit=limit,
    )
    _emit_output(
        filtered,
        serialized,
        title=cached_title,
        output=output,
        json_flag=json_flag,
        quiet=quiet,
        max_price=max_price,
        filter_breakdown=filter_breakdown,
        editions_removed=editions_removed,
        series_collapsed=series_collapsed,
        total_before_limit=total_before_limit,
        currency=cur,
        interactive=interactive,
        show_url=show_url,
    )
