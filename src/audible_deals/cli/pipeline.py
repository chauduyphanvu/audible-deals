"""Shared scan pipeline: filter, record, cache, and emit results."""

from __future__ import annotations

import dataclasses
import json as json_mod
import logging
import math
from pathlib import Path

import click

from audible_deals.cli.helpers import _load_profile, _safe_record_prices
from audible_deals.cli.interactive import _interactive_browse
from audible_deals.client import DealsClient
from audible_deals.product import Product
from audible_deals.constants import LOCALE_LANGUAGES, MAX_PAGE_SIZE
from audible_deals.display import (
    console,
    create_scan_progress,
    display_products,
    display_summary,
)
from audible_deals.filtering import (
    dedupe_editions,
    filter_products,
    first_in_series,
    sort_local,
)
from audible_deals.price_history import (
    hist_percentiles,
    load_price_history,
    price_drop_pcts,
    price_history_context,
)
from audible_deals.results_cache import save_last_results, save_seen_asins
from audible_deals.serialization import export_products, serialize_product
from audible_deals.settings import Settings

logger = logging.getLogger(__name__)


def _apply_filters(
    all_products: list[Product],
    *,
    max_price: float | None,
    min_rating: float,
    min_ratings: int = 0,
    min_hours: float,
    narrator: str = "",
    language: str,
    author: str = "",
    exclude_authors: tuple[str, ...] = (),
    exclude_narrators: tuple[str, ...] = (),
    on_sale: bool,
    skip_asins: set[str] | None,
    exclude_category_ids: set[str],
    first_in_series_only: bool,
    sort: str,
    max_pph: float | None = None,
    min_discount: int = 0,
    series: str = "",
    publisher: str = "",
    skip_plus: bool = False,
    only_plus: bool = False,
    exclude_keywords: tuple[str, ...] = (),
    drop_zero_length: bool = True,
    hist_below: int | None = None,
    min_price_drop: float = 0.0,
    require_history: bool = False,
    released_after: str = "",
    released_before: str = "",
) -> tuple[list[Product], dict[str, int], int, int, dict[str, list[dict]] | None]:
    """Filter, deduplicate, and sort products. Returns (filtered, breakdown, editions_removed, series_collapsed, histories)."""
    hist_percentile = None
    price_drops = None
    histories: dict[str, list[dict]] | None = None
    if hist_below is not None or min_price_drop > 0:
        histories = {
            p.asin: load_price_history(p.asin)
            for p in all_products
            if p.price is not None
        }
        if hist_below is not None:
            hist_percentile = hist_percentiles(all_products, histories)
        if min_price_drop > 0:
            price_drops = price_drop_pcts(all_products, histories)
    filtered, filter_breakdown = filter_products(
        all_products,
        drop_zero_length=drop_zero_length,
        max_price=max_price,
        min_rating=min_rating,
        min_ratings=min_ratings,
        min_hours=min_hours,
        narrator=narrator,
        language=language,
        author=author,
        exclude_authors=exclude_authors,
        exclude_narrators=exclude_narrators,
        on_sale=on_sale,
        skip_asins=skip_asins,
        exclude_category_ids=exclude_category_ids,
        max_pph=max_pph,
        min_discount=min_discount,
        series=series,
        publisher=publisher,
        skip_plus=skip_plus,
        only_plus=only_plus,
        exclude_keywords=exclude_keywords,
        max_hist_percentile=hist_below,
        hist_percentile=hist_percentile,
        min_price_drop=min_price_drop,
        price_drops=price_drops,
        require_history=require_history,
        released_after=released_after,
        released_before=released_before,
    )
    filtered, editions_removed = dedupe_editions(filtered)
    series_collapsed = 0
    if first_in_series_only:
        filtered, series_collapsed = first_in_series(filtered)
    filtered = sort_local(filtered, sort)
    return filtered, filter_breakdown, editions_removed, series_collapsed, histories


def _record_and_cache(
    filtered: list[Product],
    *,
    title: str,
    write_cache: bool = True,
    limit: int | None,
) -> tuple[list[Product], list[dict], int]:
    """Record prices, persist cache, apply limit. Returns (filtered_limited, serialized, total_before_limit)."""
    _safe_record_prices(filtered)
    serialized_all = [serialize_product(p) for p in filtered]
    if write_cache:
        try:
            save_last_results(title, serialized_all)
        except Exception:
            pass
    total_before_limit = len(filtered)
    if limit is not None and limit > 0:
        filtered = filtered[:limit]
        serialized = serialized_all[:limit]
    else:
        serialized = serialized_all
    if write_cache:
        save_seen_asins({p.asin for p in filtered})
    return filtered, serialized, total_before_limit


def _emit_output(
    filtered: list[Product],
    serialized: list[dict],
    *,
    title: str,
    output: Path | None,
    json_flag: bool,
    quiet: bool,
    max_price: float | None,
    filter_breakdown: dict[str, int],
    editions_removed: int,
    series_collapsed: int,
    total_before_limit: int,
    currency: str = "$",
    interactive: bool = False,
    show_url: bool = False,
    histories: dict[str, list[dict]] | None = None,
) -> None:
    """Write results to file, JSON stdout, or the terminal table."""
    if output:
        export_products(filtered, output)
        console.print(f"[green]Exported {len(filtered)} items to {output}[/green]")
    if json_flag:
        click.echo(json_mod.dumps(serialized, indent=2, ensure_ascii=False))
    if not json_flag and not quiet:
        atl_asins, hist_context = price_history_context(filtered, histories=histories)
        console.print()
        display_products(
            filtered,
            max_price=max_price,
            title=title,
            currency=currency,
            show_url=show_url,
            atl_asins=atl_asins,
            hist_context=hist_context,
        )
        display_summary(
            len(filtered),
            filter_breakdown,
            max_price=max_price,
            editions_removed=editions_removed,
            series_collapsed=series_collapsed,
            currency=currency,
            total_before_limit=total_before_limit,
        )
    if interactive and filtered and not json_flag:
        _interactive_browse(filtered, currency=currency)


def _record_and_emit(
    filtered: list[Product],
    filter_breakdown: dict[str, int],
    editions_removed: int,
    series_collapsed: int,
    *,
    title: str,
    limit: int | None,
    output: Path | None,
    json_flag: bool,
    quiet: bool,
    max_price: float | None,
    currency: str,
    interactive: bool = False,
    show_url: bool = False,
    write_cache: bool = True,
    histories: dict[str, list[dict]] | None = None,
) -> None:
    """Run the shared pipeline tail: record/cache/limit, then emit."""
    filtered, serialized, total_before_limit = _record_and_cache(
        filtered,
        title=title,
        write_cache=write_cache,
        limit=limit,
    )
    _emit_output(
        filtered,
        serialized,
        title=title,
        output=output,
        json_flag=json_flag,
        quiet=quiet,
        max_price=max_price,
        filter_breakdown=filter_breakdown,
        editions_removed=editions_removed,
        series_collapsed=series_collapsed,
        total_before_limit=total_before_limit,
        currency=currency,
        interactive=interactive,
        show_url=show_url,
        histories=histories,
    )


def _build_scan_settings(
    ctx: click.Context,
    profile_name: str | None,
    **kwargs,
) -> Settings:
    """Resolve command kwargs + config/profile defaults into a Settings."""
    s = Settings.resolve(
        ctx,
        config=ctx.obj.get("config", {}),
        profile=_load_profile(profile_name),
        cli_flags=dict(kwargs),
    )
    if not s.language and not s.all_languages:
        s = dataclasses.replace(s, language=LOCALE_LANGUAGES.get(ctx.obj["locale"], ""))
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
        snapshot = {k: getattr(s, k) for k in debug_keys}
        logger.debug("resolved scan settings: %s", snapshot)
    return s


def _apply_settings_filters(
    products: list[Product],
    s: Settings,
    *,
    skip_asins: set[str] | None,
    exclude_category_ids: set[str],
    hist_below: int | None = None,
    min_price_drop: float = 0.0,
    require_history: bool = False,
    released_after: str = "",
    released_before: str = "",
) -> tuple[list[Product], dict[str, int], int, int, dict[str, list[dict]] | None]:
    """Run _apply_filters with all filter options taken from a resolved Settings."""
    return _apply_filters(
        products,
        max_price=s.max_price,
        min_rating=s.min_rating,
        min_ratings=s.min_ratings,
        min_hours=s.min_hours,
        narrator=s.narrator,
        author=s.author,
        exclude_authors=s.exclude_authors,
        exclude_narrators=s.exclude_narrators,
        language=s.language,
        on_sale=s.on_sale,
        skip_asins=skip_asins,
        exclude_category_ids=exclude_category_ids,
        first_in_series_only=s.first_in_series,
        sort=s.sort,
        max_pph=s.max_pph,
        min_discount=s.min_discount,
        series=s.series,
        publisher=s.publisher,
        skip_plus=s.skip_plus,
        only_plus=s.only_plus,
        exclude_keywords=s.exclude_keywords,
        hist_below=hist_below,
        min_price_drop=min_price_drop,
        require_history=require_history,
        released_after=released_after,
        released_before=released_before,
    )


def _print_dry_run_summary(
    *,
    category_name: str,
    query: str,
    sort_orders: list[str],
    pages: int,
    subcategory_count: int | None = None,
) -> None:
    """Print a dry-run scan summary."""
    sort_label = ", ".join(sort_orders)
    multiplier = subcategory_count if subcategory_count is not None else 1
    console.print("\n[bold]Dry run[/bold] — would scan:")
    if category_name:
        console.print(f"  Category: {category_name}")
    if subcategory_count is not None:
        console.print(f"  Subcategories: {subcategory_count}")
    if query:
        console.print(f"  Query: {query}")
    console.print(f"  Sort orders: {sort_label}")
    console.print(f"  Pages per sort: {pages}")
    console.print(
        f"  Max items: ~{pages * len(sort_orders) * MAX_PAGE_SIZE * multiplier}"
    )
    console.print(f"  API calls: {pages * len(sort_orders) * multiplier}")


def _fetch_with_progress(
    dc: DealsClient,
    *,
    keywords: str,
    category_ids: list[str],
    sort_orders: list[str],
    pages: int,
    description: str,
) -> list[Product]:
    """Fetch products across one or more category ids and sort orders with a progress bar.

    Deduplicates by ASIN across all segments. Returns a flat list.
    """
    all_products: list[Product] = []
    seen_asins: set[str] = set()
    total_segments = len(category_ids) * len(sort_orders)
    total_pages = pages * total_segments

    with create_scan_progress() as progress:
        task = progress.add_task(description, total=total_pages, items=0)
        pages_done = 0
        segments_done = 0

        for category_id in category_ids:
            for sort_idx, sort_order in enumerate(sort_orders):
                for products, page_num, total in dc.search_pages(
                    keywords=keywords,
                    category_id=category_id,
                    sort_by=sort_order,
                    max_pages=pages,
                ):
                    new_products = [p for p in products if p.asin not in seen_asins]
                    seen_asins.update(p.asin for p in new_products)
                    all_products.extend(new_products)
                    pages_done += 1

                    if page_num == 1:
                        actual = min(pages, math.ceil(total / 50)) if total else 1
                        segments_remaining = total_segments - segments_done - 1
                        total_pages = (
                            (pages_done - 1) + actual + segments_remaining * pages
                        )
                        progress.update(task, total=total_pages)

                    progress.update(task, completed=pages_done, items=len(all_products))

                segments_done += 1

    return all_products
