"""Library command and its presentation helpers."""

from __future__ import annotations

import collections
import json as json_mod
import logging
from pathlib import Path

import click

from audible_deals.cli.helpers import _currency, _get_client, _resolve_output_quiet
from audible_deals.validation import NONNEGATIVE_FLOAT, NONNEGATIVE_INT, RATING_FLOAT
from audible_deals.client import DealsClient
from audible_deals.filtering import filter_products, sort_local
from audible_deals.presentation.products import display_products
from audible_deals.presentation.reports import display_library_stats, display_summary
from audible_deals.presentation.terminal import (
    console,
    create_scan_progress,
    safe_markup,
    safe_text,
)
from audible_deals.product import Product
from audible_deals.result_models import FilterContext
from audible_deals.serialization import (
    export_products,
    serialize_product,
    validate_export_path,
)

logger = logging.getLogger(__name__)


def fetch_library_with_progress(
    dc: DealsClient, *, show_progress: bool = True
) -> list[Product]:
    """Fetch the full library with a progress bar."""
    all_products: list[Product] = []
    with create_scan_progress(disable=not show_progress) as progress:
        task = progress.add_task("Fetching library", total=None, items=0)
        page_count = 0
        for page_products, page_num in dc.get_library_pages():
            all_products.extend(page_products)
            page_count = page_num
            progress.update(task, completed=page_num, items=len(all_products))
        progress.update(task, total=page_count, completed=page_count)
    return all_products


def _library_stats_json(products: list[Product]) -> dict:
    """JSON-serializable form of the aggregates shown by display_library_stats."""
    total = len(products)
    total_hours = sum(p.hours for p in products)
    rated = [p.rating for p in products if p.rating > 0]

    def _top(counter: collections.Counter[str]) -> list[dict]:
        return [{"name": name, "count": c} for name, c in counter.most_common(5)]

    genre_counts: collections.Counter[str] = collections.Counter()
    author_counts: collections.Counter[str] = collections.Counter()
    narrator_counts: collections.Counter[str] = collections.Counter()
    for p in products:
        genre_counts.update(p.categories)
        author_counts.update(p.authors)
        narrator_counts.update(p.narrators)

    return {
        "total_books": total,
        "total_hours": round(total_hours, 1),
        "avg_hours": round(total_hours / total, 1) if total else 0.0,
        "avg_rating": round(sum(rated) / len(rated), 2) if rated else 0.0,
        "top_genres": _top(genre_counts),
        "top_authors": _top(author_counts),
        "top_narrators": _top(narrator_counts),
    }


def _emit_library_output(
    filtered: list[Product],
    filter_breakdown: dict[str, int],
    *,
    stats: bool,
    stats_products: list[Product],
    total_before_limit: int,
    output: Path | None,
    json_flag: bool,
    quiet: bool,
    currency: str,
) -> None:
    """Write library results to file, JSON stdout, or the terminal table."""
    if output:
        export_products(filtered, output)
        export_message = f"Exported {len(filtered)} items to {output}"
        if json_flag:
            click.echo(safe_text(export_message), err=True)
        else:
            console.print(f"[green]{safe_markup(export_message)}[/green]")
    if json_flag:
        payload: object = (
            _library_stats_json(stats_products)
            if stats
            else [serialize_product(p) for p in filtered]
        )
        click.echo(
            json_mod.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
        )
    if not json_flag and not quiet:
        console.print()
        if stats:
            display_library_stats(stats_products, currency)
        else:
            display_products(filtered, title="Your Library", currency=currency)
            if filter_breakdown:
                display_summary(
                    len(filtered),
                    filter_breakdown,
                    currency=currency,
                    total_before_limit=total_before_limit,
                    noun="books",
                )
            elif total_before_limit > len(filtered):
                console.print(
                    f"  [bold]{len(filtered)}[/bold] of {total_before_limit} books shown"
                )
            else:
                console.print(f"  [bold]{len(filtered)}[/bold] books in library")


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
@click.option("--min-rating", type=RATING_FLOAT, default=0.0, help="Minimum rating")
@click.option(
    "--min-ratings", type=NONNEGATIVE_INT, default=0, help="Minimum number of ratings"
)
@click.option(
    "--min-hours", type=NONNEGATIVE_FLOAT, default=0.0, help="Minimum length in hours"
)
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
    validate_export_path(output)
    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)
    dc = _get_client(ctx.obj["locale"])
    with dc:
        all_products = fetch_library_with_progress(dc, show_progress=not json_flag)
    outcome = filter_products(
        all_products,
        FilterContext(
            author=author,
            narrator=narrator,
            min_rating=min_rating,
            min_ratings=min_ratings,
            min_hours=min_hours,
            genre=genre,
        ),
    )
    filtered = sort_local(list(outcome.products), sort)
    stats_products = filtered
    total_before_limit = len(filtered)
    if limit is not None and limit > 0:
        filtered = filtered[:limit]
    _emit_library_output(
        filtered,
        outcome.breakdown,
        stats=stats,
        stats_products=stats_products,
        total_before_limit=total_before_limit,
        output=output,
        json_flag=json_flag,
        quiet=quiet,
        currency=_currency(ctx),
    )
