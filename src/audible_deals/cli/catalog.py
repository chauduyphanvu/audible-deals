"""Catalog search and deal-finding commands."""

from __future__ import annotations

import datetime
import logging
import shlex
import unicodedata

import click

from audible_deals.cli.helpers import (
    _CL,
    _credit_price,
    _currency,
    _get_client,
    _resolve_categories,
    _resolve_output_quiet,
    _resolve_skip_snapshots,
)
from audible_deals.cli.options import (
    _check_plus_flags,
    _common_filter_options,
    _complete_genre_names,
)
from audible_deals.cli.pipeline import (
    _FetchProgressState,
    _apply_settings_filters,
    _build_scan_settings,
    _fetch_with_progress,
    _print_dry_run_summary,
    _record_and_emit,
    settings_result_recipe,
)
from audible_deals.constants import (
    CLIENT_SORT_OPTIONS,
    DEEP_SORT_ORDERS,
    SORT_OPTIONS,
)
from audible_deals.client import DealsClient
from audible_deals.display import console, create_scan_progress
from audible_deals.product import Product

logger = logging.getLogger(__name__)


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


def _normalized_search_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _rank_relevance(products: list[Product], query: str) -> list[Product]:
    """Stably rank exact and phrase matches ahead of the marketplace order."""
    normalized_query = _normalized_search_text(query)
    if not normalized_query:
        return products

    def tier(product: Product) -> int:
        title = _normalized_search_text(product.title)
        authors = [_normalized_search_text(author) for author in product.authors]
        if title == normalized_query or normalized_query in authors:
            return 0
        if normalized_query in title:
            return 1
        if any(normalized_query in author for author in authors):
            return 2
        return 3

    return sorted(products, key=tier)


def _fetch_search_phrase(
    dc: DealsClient,
    query: str,
    *,
    category_ids: list[str],
    sort_orders: list[str],
    pages: int,
    description: str,
    _progress=None,
    _task=None,
    _state: _FetchProgressState | None = None,
) -> list[Product]:
    if _progress is None:
        broad_calls = len(category_ids) * len(sort_orders) * pages
        probe_calls = len(category_ids) if query else 0
        state = _FetchProgressState(total=broad_calls + probe_calls)
        with create_scan_progress() as progress:
            task = progress.add_task(description, total=state.total, items=0)
            products = _fetch_search_phrase(
                dc,
                query,
                category_ids=category_ids,
                sort_orders=sort_orders,
                pages=pages,
                description=description,
                _progress=progress,
                _task=task,
                _state=state,
            )
            progress.update(
                task,
                total=state.completed,
                completed=state.completed,
                items=len(state.item_asins),
            )
            return products

    if _task is None or _state is None:
        raise ValueError("progress, task, and state must be provided together")

    broad = _fetch_with_progress(
        dc,
        keywords=query,
        category_ids=category_ids,
        sort_orders=sort_orders,
        pages=pages,
        description=description,
        progress=_progress,
        task=_task,
        state=_state,
    )
    if not query:
        return broad

    exact: list[Product] = []
    for category_id in category_ids:
        completed_before = _state.completed
        try:
            exact.extend(
                _fetch_with_progress(
                    dc,
                    keywords="",
                    title=query,
                    category_ids=[category_id],
                    sort_orders=["Relevance"],
                    pages=1,
                    description=description,
                    progress=_progress,
                    task=_task,
                    state=_state,
                )
            )
        except Exception as exc:
            if _state.completed == completed_before:
                _state.completed += 1
                _progress.update(
                    _task,
                    total=_state.total,
                    completed=_state.completed,
                    items=len(_state.item_asins),
                )
            logger.info(
                "Exact-title probe failed for %r in category %r; "
                "using broad results: %s",
                query,
                category_id,
                exc,
            )

    merged: list[Product] = []
    seen_asins: set[str] = set()
    for product in exact + broad:
        if product.asin not in seen_asins:
            seen_asins.add(product.asin)
            merged.append(product)
    return _rank_relevance(merged, query)


def _fetch_multi_query(
    dc: DealsClient,
    queries: list[str],
    *,
    category: str,
    sort_orders: list[str],
    pages: int,
) -> list[Product]:
    """Fetch each query separately, deduplicating by ASIN across queries."""
    all_products: list[Product] = []
    fetched_asins: set[str] = set()
    broad_calls = len(queries) * len(sort_orders) * pages
    probe_calls = sum(bool(query) for query in queries)
    state = _FetchProgressState(total=broad_calls + probe_calls)
    description = (
        f"Searching '{queries[0]}'"
        if len(queries) == 1
        else f"Searching {len(queries)} queries"
    )
    with create_scan_progress() as progress:
        task = progress.add_task(description, total=state.total, items=0)
        for q in queries:
            sub_products = _fetch_search_phrase(
                dc,
                category_ids=[category],
                sort_orders=sort_orders,
                pages=pages,
                query=q,
                description=f"Searching '{q}'",
                _progress=progress,
                _task=task,
                _state=state,
            )
            for p in sub_products:
                if p.asin not in fetched_asins:
                    fetched_asins.add(p.asin)
                    all_products.append(p)
        progress.update(
            task,
            total=state.completed,
            completed=state.completed,
            items=len(fetched_asins),
        )
    return all_products


def _matching_author_queries(queries: list[str], products: list[Product]) -> list[str]:
    """Return queries evidenced by an exact normalized product author match."""
    normalized_authors = {
        _normalized_search_text(author)
        for product in products
        for author in product.authors
    }
    matches: list[str] = []
    seen: set[str] = set()
    for query in queries:
        normalized = _normalized_search_text(query)
        if normalized and normalized in normalized_authors and normalized not in seen:
            matches.append(query)
            seen.add(normalized)
    return matches


def monitor_scan_plan(s, mode: str, query: str) -> tuple[list[str], list[str]]:
    """Return catalog query/sort segments for a monitor without any side effects."""
    if mode == "search":
        queries = [part.strip() for part in query.split("|") if part.strip()]
        if not queries:
            raise click.UsageError("--query must contain at least one keyword.")
        fallback = "Relevance"
    else:
        queries = [s.keywords]
        fallback = "BestSellers"
    return queries, DEEP_SORT_ORDERS if s.deep else [SORT_OPTIONS.get(s.sort, fallback)]


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
    _check_plus_flags(s.skip_plus, s.only_plus)
    if not query and not s.genre and not category:
        raise click.UsageError("Provide a QUERY or use --genre / --category to browse.")
    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)
    effective_genre = _resolve_genre_category(ctx, s.genre, category)
    server_sort = SORT_OPTIONS.get(s.sort, "Relevance")
    sort_orders = DEEP_SORT_ORDERS if s.deep else [server_sort]

    queries = (
        [q.strip() for q in query.split("|") if q.strip()] if "|" in query else [query]
    )
    if "|" in query and not queries:
        raise click.UsageError("No keywords found after splitting on '|'.")

    if dry_run:
        requested_category = category or effective_genre
        category_label = (
            f"{requested_category} (resolved during scan)" if requested_category else ""
        )
        _print_dry_run_summary(
            category_name=category_label,
            query=" | ".join(queries),
            sort_orders=sort_orders,
            pages=s.pages,
            query_count=len(queries),
            title_probe_count=sum(bool(query) for query in queries),
            result_sort=s.sort,
            limit=s.limit,
            profile_name=profile_name,
            active_filters=_active_filter_labels(
                s,
                max_effective_price=max_effective_price,
                exclude_seen=exclude_seen,
                hist_below=hist_below,
                min_price_drop=min_price_drop,
                require_history=require_history,
                released_after=released_after,
                released_before=released_before,
            ),
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

        all_products = _fetch_multi_query(
            dc,
            queries,
            category=category,
            sort_orders=sort_orders,
            pages=s.pages,
        )

    cur = _currency(ctx)
    credit_price = _credit_price(ctx)
    search_title = _search_title(queries, category_name)
    filtered, filter_breakdown, editions_removed, series_collapsed, histories = (
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
            max_effective_price=max_effective_price,
            credit_price=credit_price,
        )
    )
    _record_and_emit(
        filtered,
        filter_breakdown,
        editions_removed,
        series_collapsed,
        title=search_title,
        limit=s.limit,
        output=output,
        json_flag=json_flag,
        quiet=quiet,
        max_price=s.max_price,
        currency=cur,
        interactive=s.interactive,
        show_url=show_url,
        histories=histories,
        credit_price=credit_price,
        candidates=all_products,
        producer="search",
        locale=ctx.obj["locale"],
        recipe=settings_result_recipe(
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
    _check_plus_flags(s.skip_plus, s.only_plus)
    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)
    effective_genre = _resolve_genre_category(ctx, s.genre, category)
    server_sort = SORT_OPTIONS.get(s.sort, "BestSellers")
    sort_orders = DEEP_SORT_ORDERS if s.deep else [server_sort]

    if subcategories and not (category or effective_genre):
        raise click.UsageError("--subcategories requires --genre or --category")
    if dry_run:
        requested_category = category or effective_genre
        _print_dry_run_summary(
            category_name=f"{requested_category} (resolved during scan)"
            if requested_category
            else "",
            query=s.keywords,
            sort_orders=sort_orders,
            pages=s.pages,
            subcategories_unknown=subcategories,
            title_probe_count=1 if s.keywords else 0,
            result_sort=s.sort,
            limit=s.limit,
            profile_name=profile_name,
            active_filters=_active_filter_labels(
                s,
                max_effective_price=max_effective_price,
                exclude_seen=exclude_seen,
                hist_below=hist_below,
                min_price_drop=min_price_drop,
                require_history=require_history,
                released_after=released_after,
                released_before=released_before,
            ),
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
                console.print(
                    "[dim]No subcategories found; scanning the category directly.[/dim]"
                )
            scan_category_ids = [category]
            description = f"Scanning {desc_str}"

        all_products = _fetch_search_phrase(
            dc,
            query=s.keywords,
            category_ids=scan_category_ids,
            sort_orders=sort_orders,
            pages=s.pages,
            description=description,
        )

    cur = _currency(ctx)
    credit_price = _credit_price(ctx)
    find_title = f"Deals under {cur}{s.max_price:.2f}"
    if category_name:
        find_title += f" in {category_name}"
    if s.keywords:
        find_title += f' matching "{s.keywords}"'
    filtered, filter_breakdown, editions_removed, series_collapsed, histories = (
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
            max_effective_price=max_effective_price,
            credit_price=credit_price,
        )
    )
    _record_and_emit(
        filtered,
        filter_breakdown,
        editions_removed,
        series_collapsed,
        title=find_title,
        limit=s.limit,
        output=output,
        json_flag=json_flag,
        quiet=quiet,
        max_price=s.max_price,
        currency=cur,
        interactive=s.interactive,
        show_url=show_url,
        histories=histories,
        credit_price=credit_price,
        candidates=all_products,
        producer="find",
        locale=ctx.obj["locale"],
        recipe=settings_result_recipe(
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
    )
