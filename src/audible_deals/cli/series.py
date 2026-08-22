"""Series continuation-deal command."""

from __future__ import annotations

import json as json_mod
import logging
import shlex
import time
from pathlib import Path

import click

from audible_deals.cli.helpers import (
    _CL,
    _credit_price,
    _currency,
    _get_client,
    _resolve_output_quiet,
)
from audible_deals.client import DealsClient
from audible_deals.parsing import parse_series_position
from audible_deals.price_history import price_history_context
from audible_deals.presentation.reports import display_series_gaps
from audible_deals.presentation.terminal import console, create_scan_progress
from audible_deals.product import Product
from audible_deals.result_models import FilterContext
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
from audible_deals.settings import (
    SettingsResolutionRequest,
    resolve_settings,
)
from audible_deals.validation import NONNEGATIVE_FLOAT, NONNEGATIVE_INT, RATING_FLOAT

logger = logging.getLogger(__name__)


def _invested_series(
    lib_products: list[Product], *, min_books: int, series_filter: str
) -> dict[str, list[Product]]:
    """Group library books by series, keeping those with min_books+ owned."""
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
    return invested


def _fetch_series_candidates(
    dc: DealsClient,
    invested_sorted: list[tuple[str, list[Product]]],
    owned_asins: set[str],
    *,
    pages: int,
) -> tuple[list[Product], dict[str, str]]:
    """Fetch catalog entries for each invested series, skipping owned books.

    Returns (candidates, asin -> series_name map).
    """
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

            progress.update(task, completed=series_idx + 1, items=len(all_candidates))

            # Rate limit between series lookups
            if series_idx < len(invested_sorted) - 1:
                time.sleep(0.3)

    return all_candidates, candidate_series


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
        all_candidates, candidate_series = _fetch_series_candidates(
            dc, invested_sorted, owned_asins, pages=pages
        )

    series_title = f"Series Continuation Books ({len(invested_sorted)} series)"
    result = process_discovery(
        DiscoveryProcessingRequest(
            products=tuple(all_candidates),
            context=FilterContext(
                max_price=max_price,
                min_rating=min_rating,
                min_ratings=min_ratings,
                min_hours=min_hours,
                on_sale=on_sale,
                sort=sort,
                drop_zero_length=False,
            ),
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
            candidates=tuple(all_candidates),
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
                constraints={"drop_zero_length": False},
            ),
            json_writer=click.echo,
        )
    )
