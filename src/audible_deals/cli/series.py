"""Series continuation-deal command."""

from __future__ import annotations

import json as json_mod
import logging
import shlex
from pathlib import Path

import click

from audible_deals.cli.helpers import (
    _CL,
    _credit_price,
    _currency,
    _get_client,
    _report_partial_series_outcomes,
    _resolve_output_quiet,
)
from audible_deals.client import CatalogSearchRequest, DealsClient
from audible_deals.parsing import parse_series_position
from audible_deals.price_history import price_history_context
from audible_deals.presentation.reports import display_series_gaps
from audible_deals.presentation.terminal import (
    console,
    create_scan_progress,
    safe_markup,
)
from audible_deals.product import Product
from audible_deals.result_models import FilterContext
from audible_deals.results_cache import load_dismissed_asins
from audible_deals.result_processing import (
    DiscoveryProcessingRequest,
    process_discovery,
    result_recipe,
)
from audible_deals.result_publication import (
    ResultPublicationRequest,
    ResultSessionSpec,
    publish_discovery,
    record_prices_safely,
)
from audible_deals.serialization import validate_export_path
from audible_deals.series_identity import (
    group_series_books,
    normalize_identity_text,
    series_book_identity,
)
from audible_deals.settings import (
    SettingsResolutionRequest,
    resolve_settings,
)
from audible_deals.validation import NONNEGATIVE_FLOAT, NONNEGATIVE_INT, RATING_FLOAT

logger = logging.getLogger(__name__)

_PLACEHOLDER_TITLE = "Series Advisor Placeholder"


def _representative_series_name(books: list[Product]) -> str:
    return next((book.series_name for book in books if book.series_name), "") or next(
        (book.series_asin for book in books if book.series_asin), ""
    )


def _representative_series_asin(books: list[Product]) -> str:
    return next((book.series_asin for book in books if book.series_asin), "")


def _invested_series(
    lib_products: list[Product], *, min_books: int, series_filter: str
) -> dict[str, list[Product]]:
    """Group library books by series, keeping those with min_books+ owned."""
    invested = {
        identity: books
        for identity, books in group_series_books(lib_products).items()
        if len(books) >= min_books
    }

    if series_filter:
        normalized_filter = normalize_identity_text(series_filter)
        invested = {
            identity: books
            for identity, books in invested.items()
            if normalized_filter
            in normalize_identity_text(_representative_series_name(books))
        }
    return invested


def _fetch_series_candidates(
    dc: DealsClient,
    invested_sorted: list[tuple[str, list[Product]]],
    owned_asins: set[str],
    *,
    pages: int,
    show_progress: bool = True,
) -> tuple[list[Product], dict[str, tuple[str, ...]], tuple[str, ...], tuple[str, ...]]:
    """Fetch catalog entries for each invested series, skipping owned books.

    Returns candidates, their series mapping, failed series, and incomplete series.
    """
    best_candidates: dict[tuple[str, str], Product] = {}
    failures: list[str] = []
    incomplete: list[str] = []
    series_asins = [
        _representative_series_asin(owned_books) for _, owned_books in invested_sorted
    ]
    fallback_indices = [
        index for index, series_asin in enumerate(series_asins) if not series_asin
    ]
    fallback_requests = []
    for index in fallback_indices:
        _, owned_books = invested_sorted[index]
        series_name = _representative_series_name(owned_books)
        author_hint = next(
            (book.authors[0] for book in owned_books if book.authors), ""
        )
        fallback_requests.append(
            CatalogSearchRequest(
                keywords=f"{series_name} {author_hint}".strip(),
                sort_by="Relevance",
                max_pages=pages,
                optional=True,
            )
        )
    with create_scan_progress(disable=not show_progress) as progress:
        task = progress.add_task(
            f"Scanning {len(invested_sorted)} series",
            total=len(invested_sorted),
            items=0,
        )
        direct_batch = dc.get_series_products_many(
            [series_asin for series_asin in series_asins if series_asin]
        )
        fallback_results = dc.search_segments(fallback_requests)
        fallback_by_index = dict(zip(fallback_indices, fallback_results))

        for series_idx, ((target_identity, owned_books), series_asin) in enumerate(
            zip(invested_sorted, series_asins)
        ):
            sname = _representative_series_name(owned_books)
            try:
                if series_asin:
                    failure = direct_batch.failures.get(series_asin)
                    if failure is not None:
                        raise failure
                    series_products = list(direct_batch.products.get(series_asin, ()))
                    product_failures = direct_batch.product_failures.get(
                        series_asin, ()
                    )
                    missing = direct_batch.missing_asins.get(series_asin, ())
                    issues = []
                    if product_failures:
                        issues.append(
                            f"{len(product_failures)} product batch request(s) failed"
                        )
                    if missing:
                        issues.append(f"{len(missing)} product(s) unavailable")
                    if issues:
                        detail = f"{sname}: {', '.join(issues)}"
                        logger.warning("incomplete series scan: %s", detail)
                        incomplete.append(detail)
                else:
                    series_products = []
                    segment = fallback_by_index[series_idx]
                    if segment.error is not None:
                        raise segment.error
                    normalized_name = normalize_identity_text(sname)
                    for page_products, _, _ in segment.pages:
                        for p in page_products:
                            if (
                                p.series_name
                                and normalize_identity_text(p.series_name)
                                == normalized_name
                            ):
                                series_products.append(p)
            except Exception as exc:
                logger.warning("series scan failed for %s", sname, exc_info=True)
                failures.append(f"{sname}: {type(exc).__name__}: {exc}")
                progress.update(
                    task, completed=series_idx + 1, items=len(best_candidates)
                )
                continue

            owned_book_identities = {series_book_identity(book) for book in owned_books}
            for p in series_products:
                book_identity = series_book_identity(p)
                if p.asin in owned_asins or book_identity in owned_book_identities:
                    continue
                key = (target_identity, book_identity)
                existing = best_candidates.get(key)
                if existing is None or (
                    p.price is not None
                    and (existing.price is None or p.price < existing.price)
                ):
                    best_candidates[key] = p

            progress.update(task, completed=series_idx + 1, items=len(best_candidates))

    all_candidates: list[Product] = []
    candidate_series_lists: dict[str, list[str]] = {}
    for (target_identity, _), product in best_candidates.items():
        targets = candidate_series_lists.setdefault(product.asin, [])
        first_occurrence = not targets
        if target_identity not in targets:
            targets.append(target_identity)
        if first_occurrence:
            all_candidates.append(product)
    candidate_series = {
        asin: tuple(targets) for asin, targets in candidate_series_lists.items()
    }
    return all_candidates, candidate_series, tuple(failures), tuple(incomplete)


def _series_gaps_report(
    filtered: list[Product],
    invested_sorted: list[tuple[str, list[Product]]],
    candidate_series: dict[str, tuple[str, ...]],
    *,
    json_flag: bool,
    quiet: bool,
    currency: str,
) -> None:
    """Emit the per-series gap report (JSON or table) from filtered candidates."""
    by_series: dict[str, dict[str, Product]] = {}
    for p in filtered:
        for target_identity in candidate_series.get(p.asin, ()):
            by_series.setdefault(target_identity, {}).setdefault(
                series_book_identity(p), p
            )

    atl_asins, _ = price_history_context(filtered)

    gaps: list[dict] = []
    for target_identity, books in sorted(
        invested_sorted, key=lambda x: _representative_series_name(x[1])
    ):
        sname = _representative_series_name(books)
        missing_by_identity = by_series.get(target_identity, {})
        missing_products = list(missing_by_identity.values())
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
        owned_identities = {series_book_identity(book) for book in books}
        gaps.append(
            {
                "series": sname,
                "owned": len(owned_identities),
                "total_known": len(owned_identities | set(missing_by_identity)),
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
        click.echo(
            json_mod.dumps(stripped, indent=2, ensure_ascii=False, allow_nan=False)
        )
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
    "--max-price", type=NONNEGATIVE_FLOAT, default=None, help="Max price filter"
)
@click.option(
    "--min-rating", type=RATING_FLOAT, default=0.0, help="Minimum rating (e.g. 4.0)"
)
@click.option(
    "--min-ratings", type=NONNEGATIVE_INT, default=0, help="Minimum number of ratings"
)
@click.option(
    "--min-hours", type=NONNEGATIVE_FLOAT, default=0.0, help="Minimum length in hours"
)
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
    validate_export_path(output)
    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)

    explicit_options = {
        name
        for name in (
            "max_price",
            "min_rating",
            "min_ratings",
            "min_hours",
            "on_sale",
            "limit",
            "sort",
            "pages",
        )
        if ctx.get_parameter_source(name) == _CL
    }
    try:
        s = resolve_settings(
            SettingsResolutionRequest(
                config=ctx.obj.get("config", {}),
                profile=None,
                explicit_options=explicit_options,
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
            ),
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    max_price, min_rating, min_ratings = s.max_price, s.min_rating, s.min_ratings
    min_hours, on_sale, limit = s.min_hours, s.on_sale, s.limit
    sort, pages = s.sort, s.pages

    dc = _get_client(ctx.obj["locale"])
    cur = _currency(ctx)
    dismissed_asins = load_dismissed_asins()

    with dc:
        # 1. Fetch library
        if not quiet and not json_flag:
            console.print("[dim]Fetching library...[/dim]")
        lib_products = dc.get_library()
        owned_asins = {p.asin for p in lib_products}

        # 2. Identify invested series (user owns min_books+ books)
        invested = _invested_series(
            lib_products, min_books=min_books, series_filter=series_filter
        )

        if not invested:
            if series_filter:
                console.print(
                    f"[dim]No invested series matching '{safe_markup(series_filter)}' "
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
        all_candidates, candidate_series, failures, incomplete = (
            _fetch_series_candidates(
                dc,
                invested_sorted,
                owned_asins,
                pages=pages,
                show_progress=not json_flag,
            )
        )

    _report_partial_series_outcomes(
        failures,
        incomplete,
        len(invested_sorted),
        json_flag=json_flag,
    )

    series_title = f"Series Continuation Books ({len(invested_sorted)} series)"
    eligible_candidates = [
        product
        for product in all_candidates
        if product.title != _PLACEHOLDER_TITLE
        and (gaps_mode or product.price is not None)
        and (
            not gaps_mode
            or max_price is None
            or product.price is None
            or product.price <= max_price
        )
    ]
    result = process_discovery(
        DiscoveryProcessingRequest(
            products=tuple(eligible_candidates),
            context=FilterContext(
                max_price=None if gaps_mode else max_price,
                min_rating=min_rating,
                min_ratings=min_ratings,
                min_hours=min_hours,
                on_sale=on_sale,
                skip_asins=dismissed_asins,
                sort=sort,
                drop_zero_length=False,
            ),
            dedupe_series_editions=False,
        ),
    )

    if gaps_mode:
        # Keep history without caching or applying the flat-view limit.
        record_prices_safely(list(result.products))
        _series_gaps_report(
            list(result.products),
            invested_sorted,
            candidate_series,
            json_flag=json_flag,
            quiet=quiet,
            currency=cur,
        )
        return

    publish_discovery(
        ResultPublicationRequest(
            result=result,
            title=series_title,
            limit=limit,
            output=output,
            json_flag=json_flag,
            quiet=quiet,
            max_price=max_price,
            currency=cur,
            interactive=interactive,
            credit_price=_credit_price(ctx),
            candidates=tuple(eligible_candidates),
            session_spec=ResultSessionSpec(
                producer="series",
                locale=ctx.obj["locale"],
                recipe=result_recipe(
                    max_price=max_price,
                    min_rating=min_rating,
                    min_ratings=min_ratings,
                    min_hours=min_hours,
                    on_sale=on_sale,
                    sort=sort,
                    limit=limit,
                ),
                source={
                    "command": shlex.join(
                        [
                            "deals",
                            "series",
                            "--min-books",
                            str(min_books),
                            "--max-series",
                            str(max_series),
                            "--pages",
                            str(pages),
                            *(["--series", series_filter] if series_filter else []),
                        ]
                    ),
                    "series_filter": series_filter,
                    "min_books": min_books,
                    "max_series": max_series,
                    "pages": pages,
                },
                constraints={
                    "drop_zero_length": False,
                    "always_skip_asins": sorted(dismissed_asins),
                },
            ),
            json_writer=click.echo,
        )
    )
