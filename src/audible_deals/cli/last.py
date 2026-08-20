"""Cached result command."""

from __future__ import annotations

import logging
from pathlib import Path

import click

from audible_deals.cli.helpers import _credit_price, _currency, _resolve_output_quiet
from audible_deals.cli.options import _check_plus_flags
from audible_deals.cli.pipeline import _apply_filters, _record_and_emit
from audible_deals.display import console
from audible_deals.results_cache import (
    clear_last_results,
    clear_seen_asins,
    load_last_results,
)
from audible_deals.serialization import deserialize_product, validate_export_path

logger = logging.getLogger(__name__)


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
            "title",
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
@click.option(
    "--max-effective-price",
    "max_effective_price",
    type=click.FloatRange(min=0),
    default=None,
    help="Max effective price — the cheaper of cash price and one credit",
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
    max_effective_price,
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
    validate_export_path(output)
    _check_plus_flags(skip_plus, only_plus)
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
    credit_price = _credit_price(ctx)
    filtered, filter_breakdown, editions_removed, series_collapsed, _ = _apply_filters(
        products,
        max_price=max_price,
        max_effective_price=max_effective_price,
        credit_price=credit_price,
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
    _record_and_emit(
        filtered,
        filter_breakdown,
        editions_removed,
        series_collapsed,
        title=cached_title,
        limit=limit,
        output=output,
        json_flag=json_flag,
        quiet=quiet,
        max_price=max_price,
        currency=cur,
        interactive=interactive,
        show_url=show_url,
        write_cache=False,
        record_prices=False,
        credit_price=credit_price,
    )
