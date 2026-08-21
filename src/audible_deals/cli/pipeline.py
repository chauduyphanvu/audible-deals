"""Shared scan pipeline: filter, record, cache, and emit results."""

from __future__ import annotations

import dataclasses
import datetime
import copy
import json as json_mod
import logging
import math
from pathlib import Path

import click
from rich.progress import Progress, TaskID

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
    history_key,
    hist_percentiles,
    load_price_history,
    price_drop_pcts,
    price_history_context,
)
from audible_deals.results_cache import (
    ResultSession,
    save_last_results,
    save_result_session,
    save_seen_asins,
)
from audible_deals.serialization import (
    deserialize_product,
    export_products,
    serialize_product,
)
from audible_deals.settings import Settings

logger = logging.getLogger(__name__)


RECIPE_DEFAULTS: dict[str, object] = {
    "max_price": None,
    "max_pph": None,
    "max_effective_price": None,
    "min_rating": 0.0,
    "min_ratings": 0,
    "min_hours": 0.0,
    "narrator": "",
    "author": "",
    "series": "",
    "publisher": "",
    "exclude_authors": [],
    "exclude_narrators": [],
    "language": "",
    "on_sale": False,
    "min_discount": 0,
    "first_in_series": False,
    "sort": "",
    "limit": 0,
    "skip_plus": False,
    "only_plus": False,
    "exclude_keywords": [],
    "hist_below": None,
    "min_price_drop": 0.0,
    "require_history": False,
    "released_after": "",
    "released_before": "",
    "skip_owned": False,
    "exclude_seen": False,
    "exclude_genres": [],
}


def result_recipe(**values) -> dict[str, object]:
    """Return a complete, JSON-safe refinement recipe."""
    recipe = copy.deepcopy(RECIPE_DEFAULTS)
    for key, value in values.items():
        if key not in recipe:
            raise ValueError(f"Unknown result recipe field: {key}")
        recipe[key] = list(value) if isinstance(value, tuple) else value
    return recipe


def settings_result_recipe(settings: Settings, **values) -> dict[str, object]:
    """Build a refinement recipe from resolved discovery settings."""
    recipe_values = {
        key: getattr(settings, key) for key in RECIPE_DEFAULTS if hasattr(settings, key)
    }
    recipe_values.update(values)
    return result_recipe(**recipe_values)


def apply_result_recipe(
    session: ResultSession,
    recipe: dict[str, object],
    *,
    credit_price: float | None,
) -> tuple[
    list[Product],
    dict[str, int],
    int,
    int,
    dict[str, list[dict]] | None,
    dict[str, str] | None,
]:
    """Apply one session recipe to its complete cached candidate pool."""
    products = [
        product
        for item in session.candidates
        if (product := deserialize_product(item)) is not None
    ]
    constraints = session.constraints
    history_percentiles = constraints.get("history_percentiles")
    price_drops = constraints.get("price_drop_pcts")
    skip_asins: set[str] = set(constraints.get("always_skip_asins", []))
    if recipe.get("skip_owned"):
        skip_asins.update(constraints.get("owned_asins", []))
    if recipe.get("exclude_seen"):
        skip_asins.update(constraints.get("seen_asins", []))
    exclude_category_ids = (
        set(constraints.get("excluded_category_ids", []))
        if recipe.get("exclude_genres")
        else set()
    )
    filtered, breakdown, editions_removed, series_collapsed, histories = _apply_filters(
        products,
        max_price=recipe.get("max_price"),
        max_effective_price=recipe.get("max_effective_price"),
        credit_price=credit_price,
        min_rating=float(recipe.get("min_rating") or 0),
        min_ratings=int(recipe.get("min_ratings") or 0),
        min_hours=float(recipe.get("min_hours") or 0),
        narrator=str(recipe.get("narrator") or ""),
        author=str(recipe.get("author") or ""),
        exclude_authors=tuple(recipe.get("exclude_authors") or ()),
        exclude_narrators=tuple(recipe.get("exclude_narrators") or ()),
        language=str(recipe.get("language") or ""),
        on_sale=bool(recipe.get("on_sale")),
        skip_asins=skip_asins or None,
        exclude_category_ids=exclude_category_ids,
        first_in_series_only=bool(recipe.get("first_in_series")),
        sort=str(recipe.get("sort") or ""),
        max_pph=recipe.get("max_pph"),
        min_discount=int(recipe.get("min_discount") or 0),
        series=str(recipe.get("series") or ""),
        publisher=str(recipe.get("publisher") or ""),
        skip_plus=bool(recipe.get("skip_plus")),
        only_plus=bool(recipe.get("only_plus")),
        exclude_keywords=tuple(recipe.get("exclude_keywords") or ()),
        drop_zero_length=bool(constraints.get("drop_zero_length", True)),
        hist_below=recipe.get("hist_below"),
        min_price_drop=float(recipe.get("min_price_drop") or 0),
        require_history=bool(recipe.get("require_history")),
        released_after=str(recipe.get("released_after") or ""),
        released_before=str(recipe.get("released_before") or ""),
        hist_percentile=(
            history_percentiles if isinstance(history_percentiles, dict) else None
        ),
        price_drops=price_drops if isinstance(price_drops, dict) else None,
    )
    allowed = session.ranking_context.get("allowed_asins")
    if isinstance(allowed, list):
        allowed_set = set(allowed)
        filtered = [product for product in filtered if product.asin in allowed_set]
    match_reasons = session.ranking_context.get("match_reasons")
    match_context = (
        {product.asin: str(match_reasons.get(product.asin, "")) for product in filtered}
        if isinstance(match_reasons, dict)
        else None
    )
    return (
        filtered,
        breakdown,
        editions_removed,
        series_collapsed,
        histories,
        match_context,
    )


@dataclasses.dataclass
class _FetchProgressState:
    total: int
    completed: int = 0
    item_asins: set[str] = dataclasses.field(default_factory=set)


def _apply_filters(
    all_products: list[Product],
    *,
    max_price: float | None,
    max_effective_price: float | None = None,
    credit_price: float | None = None,
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
    hist_percentile: dict[str, int] | None = None,
    price_drops: dict[str, float] | None = None,
) -> tuple[list[Product], dict[str, int], int, int, dict[str, list[dict]] | None]:
    """Filter, deduplicate, and sort products. Returns (filtered, breakdown, editions_removed, series_collapsed, histories)."""
    histories: dict[str, list[dict]] | None = None
    if (hist_below is not None and hist_percentile is None) or (
        min_price_drop > 0 and price_drops is None
    ):
        histories = {
            history_key(p.asin, p.locale): load_price_history(p.asin, p.locale)
            for p in all_products
            if p.price is not None
        }
        if hist_below is not None and hist_percentile is None:
            hist_percentile = hist_percentiles(all_products, histories)
        if min_price_drop > 0 and price_drops is None:
            price_drops = price_drop_pcts(all_products, histories)
    filtered, filter_breakdown = filter_products(
        all_products,
        drop_zero_length=drop_zero_length,
        max_price=max_price,
        max_effective_price=max_effective_price,
        credit_price=credit_price,
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
    record_prices: bool = True,
    limit: int | None,
    candidates: list[Product] | None = None,
    producer: str | None = None,
    locale: str = "us",
    recipe: dict[str, object] | None = None,
    source: dict | None = None,
    constraints: dict | None = None,
    ranking_context: dict | None = None,
    histories: dict[str, list[dict]] | None = None,
    credit_price: float | None = None,
) -> tuple[list[Product], list[dict], int]:
    """Record prices, persist cache, apply limit. Returns (filtered_limited, serialized, total_before_limit)."""
    session_constraints = copy.deepcopy(constraints or {})
    if candidates is not None and producer is not None and recipe is not None:
        snapshot_histories = histories
        if snapshot_histories is None:
            snapshot_histories = {
                history_key(product.asin, product.locale): load_price_history(
                    product.asin, product.locale
                )
                for product in candidates
                if product.price is not None
            }
        session_constraints["history_percentiles"] = hist_percentiles(
            candidates, snapshot_histories
        )
        session_constraints["price_drop_pcts"] = price_drop_pcts(
            candidates, snapshot_histories
        )
        session_constraints["credit_price"] = credit_price
    if record_prices:
        _safe_record_prices(filtered)
    serialized_all = [serialize_product(p) for p in filtered]
    total_before_limit = len(filtered)
    if limit is not None and limit > 0:
        filtered = filtered[:limit]
        serialized = serialized_all[:limit]
    else:
        serialized = serialized_all
    if write_cache:
        try:
            if candidates is not None and producer is not None and recipe is not None:
                session = ResultSession(
                    producer=producer,
                    locale=locale,
                    title=title,
                    source=source or {"command": f"deals {producer}"},
                    candidates=[serialize_product(product) for product in candidates],
                    baseline_recipe=copy.deepcopy(recipe),
                    current_recipe=copy.deepcopy(recipe),
                    visible_asins=[product.asin for product in filtered],
                    constraints=session_constraints,
                    ranking_context=ranking_context or {},
                )
                save_result_session(session)
            else:
                save_last_results(title, serialized_all)
        except Exception:
            logger.warning("Could not save last result session", exc_info=True)
        save_seen_asins({p.asin for p in filtered})
    return filtered, serialized, total_before_limit


def _prior_histories(
    products: list[Product], histories: dict[str, list[dict]] | None
) -> dict[str, list[dict]]:
    """Histories with today's entry stripped, so the 'vs median' badge ignores
    today's just-recorded price — matching the ATL logic and making it
    independent of whether a pre-record snapshot was passed in."""
    today_iso = datetime.date.today().isoformat()
    source = histories
    if source is None:
        source = {
            history_key(p.asin, p.locale): load_price_history(p.asin, p.locale)
            for p in products
            if p.price is not None
        }
    return {
        key: [e for e in entries if e.get("date") != today_iso]
        for key, entries in source.items()
    }


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
    credit_price: float | None = None,
    match_context: dict[str, str] | None = None,
    atl_asins: set[str] | None = None,
    hist_context: dict[str, int] | None = None,
    suppress_action_footer: bool = False,
) -> None:
    """Write results to file, JSON stdout, or the terminal table."""
    if output:
        export_products(filtered, output)
        console.print(f"[green]Exported {len(filtered)} items to {output}[/green]")
    if json_flag:
        click.echo(json_mod.dumps(serialized, indent=2, ensure_ascii=False))
    need_context = (not json_flag and not quiet) or interactive
    if need_context and atl_asins is None and hist_context is None:
        atl_asins, hist_context = price_history_context(
            filtered, histories=_prior_histories(filtered, histories)
        )
    if not json_flag and not quiet:
        console.print()
        display_products(
            filtered,
            max_price=max_price,
            title=title,
            currency=currency,
            show_url=show_url,
            atl_asins=atl_asins,
            hist_context=hist_context,
            credit_price=credit_price,
            match_context=match_context,
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
        if (
            filtered
            and not interactive
            and not suppress_action_footer
            and console.is_terminal
        ):
            actions = ["deals detail @1", "deals wishlist add @1"]
            if len(filtered) >= 2:
                actions.insert(1, "deals compare @1 @2")
            console.print("  [dim]Next: " + " · ".join(actions) + "[/dim]")
    if interactive and filtered and not json_flag:
        _interactive_browse(
            filtered,
            currency=currency,
            credit_price=credit_price,
            title=title,
            max_price=max_price,
            show_url=show_url,
            atl_asins=atl_asins,
            hist_context=hist_context,
            match_context=match_context,
        )


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
    record_prices: bool = True,
    histories: dict[str, list[dict]] | None = None,
    credit_price: float | None = None,
    match_context: dict[str, str] | None = None,
    atl_asins: set[str] | None = None,
    hist_context: dict[str, int] | None = None,
    candidates: list[Product] | None = None,
    producer: str | None = None,
    locale: str = "us",
    recipe: dict[str, object] | None = None,
    source: dict | None = None,
    constraints: dict | None = None,
    ranking_context: dict | None = None,
) -> None:
    """Run the shared pipeline tail: record/cache/limit, then emit."""
    had_output = output is not None
    if output:
        export_products(filtered[:limit] if limit and limit > 0 else filtered, output)
        console.print(
            f"[green]Exported {min(len(filtered), limit) if limit and limit > 0 else len(filtered)} items to {output}[/green]"
        )
    filtered, serialized, total_before_limit = _record_and_cache(
        filtered,
        title=title,
        write_cache=write_cache,
        record_prices=record_prices,
        limit=limit,
        candidates=candidates,
        producer=producer,
        locale=locale,
        recipe=recipe,
        source=source,
        constraints=constraints,
        ranking_context=ranking_context,
        histories=histories,
        credit_price=credit_price,
    )
    _emit_output(
        filtered,
        serialized,
        title=title,
        output=None,
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
        credit_price=credit_price,
        match_context=match_context,
        atl_asins=atl_asins,
        hist_context=hist_context,
        suppress_action_footer=had_output,
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
    max_effective_price: float | None = None,
    credit_price: float | None = None,
) -> tuple[list[Product], dict[str, int], int, int, dict[str, list[dict]] | None]:
    """Run _apply_filters with all filter options taken from a resolved Settings."""
    return _apply_filters(
        products,
        max_price=s.max_price,
        max_effective_price=max_effective_price,
        credit_price=credit_price,
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
    subcategories_unknown: bool = False,
    query_count: int = 1,
    title_probe_count: int = 0,
    result_sort: str,
    limit: int | None,
    profile_name: str | None,
    active_filters: list[str],
) -> None:
    """Print a dry-run scan summary."""
    sort_label = ", ".join(sort_orders)
    multiplier = subcategory_count if subcategory_count is not None else 1
    console.print("\n[bold]Dry run[/bold] — would scan:")
    if category_name:
        console.print(f"  Category: {category_name}")
    if subcategory_count is not None:
        console.print(f"  Subcategories: {subcategory_count}")
    elif subcategories_unknown:
        console.print("  Subcategories: unknown (resolved during scan)")
    if query:
        console.print(f"  Query: {query}")
    console.print(f"  Result sort: {result_sort}")
    console.print(f"  Limit: {limit if limit and limit > 0 else 'unlimited'}")
    console.print(f"  Profile: {profile_name or 'none'}")
    console.print(
        f"  Filters: {'; '.join(active_filters) if active_filters else 'none'}"
    )
    console.print(f"  Sort orders: {sort_label}")
    console.print(f"  Pages per sort: {pages}")
    if subcategories_unknown:
        console.print("  Max items: unknown (depends on subcategory count)")
        console.print("  API calls: unknown (depends on subcategory count)")
    else:
        broad_calls = pages * len(sort_orders) * multiplier * query_count
        title_calls = title_probe_count * multiplier
        total_calls = broad_calls + title_calls
        console.print(f"  Max items: ~{total_calls * MAX_PAGE_SIZE}")
        console.print(f"  API calls: {total_calls}")


def _fetch_with_progress(
    dc: DealsClient,
    *,
    keywords: str,
    title: str = "",
    category_ids: list[str],
    sort_orders: list[str],
    pages: int,
    description: str,
    progress: Progress | None = None,
    task: TaskID | None = None,
    state: _FetchProgressState | None = None,
) -> list[Product]:
    """Fetch products across one or more category ids and sort orders with a progress bar.

    Deduplicates by ASIN across all segments. Returns a flat list.
    """
    total_segments = len(category_ids) * len(sort_orders)
    if progress is None:
        state = _FetchProgressState(total=pages * total_segments)
        with create_scan_progress() as owned_progress:
            owned_task = owned_progress.add_task(
                description, total=state.total, items=0
            )
            products = _fetch_with_progress(
                dc,
                keywords=keywords,
                title=title,
                category_ids=category_ids,
                sort_orders=sort_orders,
                pages=pages,
                description=description,
                progress=owned_progress,
                task=owned_task,
                state=state,
            )
            owned_progress.update(
                owned_task,
                total=state.completed,
                completed=state.completed,
                items=len(state.item_asins),
            )
            return products

    if task is None or state is None:
        raise ValueError("progress, task, and state must be provided together")

    all_products: list[Product] = []
    seen_asins: set[str] = set()
    for category_id in category_ids:
        for sort_order in sort_orders:
            first_page_seen = False
            query_args = {"title": title} if title else {"keywords": keywords}
            for products, page_num, total in dc.search_pages(
                **query_args,
                category_id=category_id,
                sort_by=sort_order,
                max_pages=pages,
            ):
                new_products = [p for p in products if p.asin not in seen_asins]
                seen_asins.update(p.asin for p in new_products)
                all_products.extend(new_products)
                state.item_asins.update(p.asin for p in products)
                state.completed += 1

                if page_num == 1 and not first_page_seen:
                    actual = (
                        min(pages, math.ceil(total / MAX_PAGE_SIZE)) if total else 1
                    )
                    state.total -= pages - actual
                    first_page_seen = True

                progress.update(
                    task,
                    total=state.total,
                    completed=state.completed,
                    items=len(state.item_asins),
                )

    return all_products
