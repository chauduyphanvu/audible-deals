"""Cumulative, API-free cached result refinement."""

from __future__ import annotations

import copy
import datetime
from pathlib import Path

import click

from audible_deals.cli.helpers import _credit_price, _currency, _resolve_output_quiet
from audible_deals.cli.options import _check_plus_flags
from audible_deals.cli.pipeline import (
    RECIPE_DEFAULTS,
    _record_and_emit,
    apply_result_recipe,
)
from audible_deals.display import console
from audible_deals.constants import LOCALE_CURRENCY
from audible_deals.results_cache import (
    clear_last_results,
    clear_seen_asins,
    load_last_results,
    load_result_session,
    save_result_session,
)
from audible_deals.serialization import export_products, validate_export_path

_SORTS = [
    "price",
    "-price",
    "discount",
    "price-per-hour",
    "value",
    "rating",
    "length",
    "date",
    "title",
    "author",
    "asin",
    "bestsellers",
    "relevance",
]

_CLEARABLE = {
    key.replace("_", "-"): key
    for key in RECIPE_DEFAULTS
    if key not in {"sort", "limit"}
}
_CLEARABLE.update(
    {
        "max-price-per-hour": "max_pph",
        "first-in-series": "first_in_series",
        "exclude-author": "exclude_authors",
        "exclude-narrator": "exclude_narrators",
        "exclude-keyword": "exclude_keywords",
        "exclude-genre": "exclude_genres",
    }
)


def _option_given(ctx: click.Context, name: str) -> bool:
    return ctx.get_parameter_source(name) == click.core.ParameterSource.COMMANDLINE


def _validate_immutable_options(ctx: click.Context, session, values: dict) -> None:
    for option, source_key in (
        ("query", "query"),
        ("category", "category"),
        ("genre", "genre"),
        ("pages", "pages"),
        ("deep", "deep"),
        ("subcategories", "subcategories"),
        ("refresh", "refresh"),
        ("max_series", "max_series"),
        ("min_books", "min_books"),
    ):
        if not _option_given(ctx, option):
            continue
        if values[option] == session.source.get(source_key):
            continue
        command = session.source.get("command", f"deals {session.producer}")
        raise click.UsageError(
            f"--{option.replace('_', '-')} changes what must be fetched and cannot "
            f"refine cached results. Rerun: {command}"
        )
    if session.producer == "series" and _option_given(ctx, "series"):
        command = session.source.get("command", "deals series")
        raise click.UsageError(
            "--series selects which series must be fetched for this session. "
            f"Rerun: {command}"
        )


def _clear_recipe_value(key: str):
    value = RECIPE_DEFAULTS[key]
    return copy.deepcopy(value)


@click.command("last")
@click.option("--sort", type=click.Choice(_SORTS), default=None, help="Re-sort results")
@click.option("--max-price", type=click.FloatRange(min=0), default=None)
@click.option(
    "--max-price-per-hour", "max_pph", type=click.FloatRange(min=0), default=None
)
@click.option(
    "--max-effective-price",
    "max_effective_price",
    type=click.FloatRange(min=0),
    default=None,
)
@click.option("--min-rating", type=float, default=None)
@click.option("--min-ratings", type=click.IntRange(min=0), default=None)
@click.option("--min-hours", type=click.FloatRange(min=0), default=None)
@click.option("--narrator", default=None, help="Filter by narrator name (client-side)")
@click.option("--author", default=None)
@click.option("--series", default=None)
@click.option("--publisher", default=None)
@click.option("--exclude-author", "exclude_authors", multiple=True)
@click.option("--exclude-narrator", "exclude_narrators", multiple=True)
@click.option("--language", default=None)
@click.option("--on-sale/--no-on-sale", default=None)
@click.option("--min-discount", type=click.IntRange(min=0, max=100), default=None)
@click.option("--first-in-series/--no-first-in-series", default=None)
@click.option("--skip-plus/--no-skip-plus", default=None)
@click.option("--only-plus/--no-only-plus", default=None)
@click.option("--skip-owned/--no-skip-owned", default=None)
@click.option("--exclude-seen/--no-exclude-seen", default=None)
@click.option("--exclude-keyword", "exclude_keywords", multiple=True)
@click.option("--exclude-genre", "exclude_genres", multiple=True)
@click.option("--hist-below", type=click.IntRange(min=0, max=100), default=None)
@click.option("--min-price-drop", type=click.FloatRange(min=0), default=None)
@click.option("--require-history/--no-require-history", default=None)
@click.option("--released-after", default=None)
@click.option("--released-before", default=None)
@click.option("--limit", "-n", type=click.IntRange(min=0), default=None)
@click.option(
    "--clear-filter",
    "clear_filters",
    type=click.Choice(sorted(_CLEARABLE)),
    multiple=True,
    help="Clear one inherited filter (repeatable)",
)
@click.option("--reset", is_flag=True, help="Restore the producer's original recipe")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None)
@click.option("--json", "json_flag", is_flag=True, default=False)
@click.option("--quiet", "-q", is_flag=True, default=False)
@click.option("--show-url", is_flag=True, default=False)
@click.option("--interactive", "-i", is_flag=True, default=False)
@click.option("--clear", is_flag=True, default=False)
@click.option("--clear-seen", is_flag=True, default=False)
@click.option(
    "--count",
    "count_only",
    is_flag=True,
    default=False,
    help="Show current matches before the display limit",
)
# Fetch-bound options are accepted only to produce a useful rerun error.
@click.option("--query", default=None, hidden=True)
@click.option("--category", default=None, hidden=True)
@click.option("--genre", default=None, hidden=True)
@click.option("--pages", type=click.IntRange(min=1), default=None, hidden=True)
@click.option("--deep/--no-deep", default=None, hidden=True)
@click.option("--subcategories/--no-subcategories", default=None, hidden=True)
@click.option("--refresh/--no-refresh", default=None, hidden=True)
@click.option("--max-series", type=click.IntRange(min=1), default=None, hidden=True)
@click.option("--min-books", type=click.IntRange(min=1), default=None, hidden=True)
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
    skip_plus,
    only_plus,
    skip_owned,
    exclude_seen,
    exclude_keywords,
    exclude_genres,
    hist_below,
    min_price_drop,
    require_history,
    released_after,
    released_before,
    limit,
    clear_filters,
    reset,
    output,
    json_flag,
    quiet,
    show_url,
    interactive,
    clear,
    clear_seen,
    count_only,
    query,
    category,
    genre,
    pages,
    deep,
    subcategories,
    refresh,
    max_series,
    min_books,
):
    """Re-display and cumulatively refine the last result session without API calls.

    Explicit flags replace the current value. Use --clear-filter NAME to remove
    one inherited filter, or --reset to restore the original producer recipe.

    \b
    Examples:
        deals last
        deals last --max-price 8 --sort discount
        deals last --clear-filter language
        deals last --reset --max-price 10
    """
    validate_export_path(output)
    did_clear = False
    if clear_seen:
        console.print(
            "[green]Seen ASINs list cleared.[/green]"
            if clear_seen_asins()
            else "[dim]No seen ASINs to clear.[/dim]"
        )
        did_clear = True
    if clear:
        console.print(
            "[green]Last results cache cleared.[/green]"
            if clear_last_results()
            else "[dim]No cached results to clear.[/dim]"
        )
        did_clear = True
    if did_clear:
        return

    session = load_result_session()
    any_refinement = (
        reset
        or bool(clear_filters)
        or any(
            _option_given(ctx, name)
            for name in (
                "sort",
                "max_price",
                "max_pph",
                "max_effective_price",
                "min_rating",
                "min_ratings",
                "min_hours",
                "narrator",
                "author",
                "series",
                "publisher",
                "exclude_authors",
                "exclude_narrators",
                "language",
                "on_sale",
                "min_discount",
                "first_in_series",
                "skip_plus",
                "only_plus",
                "skip_owned",
                "exclude_seen",
                "exclude_keywords",
                "exclude_genres",
                "hist_below",
                "min_price_drop",
                "require_history",
                "released_after",
                "released_before",
                "limit",
            )
        )
    )
    if count_only and session.legacy and not any_refinement:
        _, legacy_results = load_last_results()
        click.echo(len(legacy_results))
        return
    _validate_immutable_options(
        ctx,
        session,
        {
            "query": query,
            "category": category,
            "genre": genre,
            "pages": pages,
            "deep": deep,
            "subcategories": subcategories,
            "refresh": refresh,
            "max_series": max_series,
            "min_books": min_books,
        },
    )
    if session.legacy and not json_flag and not quiet and not count_only:
        console.print(
            "[yellow]Legacy limited cache: display, narrowing, and selectors work; "
            "run one new discovery scan to enable true widening.[/yellow]"
        )

    recipe = copy.deepcopy(session.baseline_recipe if reset else session.current_recipe)
    for key, default in RECIPE_DEFAULTS.items():
        recipe.setdefault(key, copy.deepcopy(default))
    for name in clear_filters:
        recipe[_CLEARABLE[name]] = _clear_recipe_value(_CLEARABLE[name])

    overrides = {
        "sort": sort,
        "max_price": max_price,
        "max_pph": max_pph,
        "max_effective_price": max_effective_price,
        "min_rating": min_rating,
        "min_ratings": min_ratings,
        "min_hours": min_hours,
        "narrator": narrator,
        "author": author,
        "series": series,
        "publisher": publisher,
        "language": language,
        "on_sale": on_sale,
        "min_discount": min_discount,
        "first_in_series": first_in_series,
        "skip_plus": skip_plus,
        "only_plus": only_plus,
        "skip_owned": skip_owned,
        "exclude_seen": exclude_seen,
        "hist_below": hist_below,
        "min_price_drop": min_price_drop,
        "require_history": require_history,
        "released_after": released_after,
        "released_before": released_before,
        "limit": limit,
    }
    for key, value in overrides.items():
        if _option_given(ctx, key):
            recipe[key] = value
    for key, value in (
        ("exclude_authors", exclude_authors),
        ("exclude_narrators", exclude_narrators),
        ("exclude_keywords", exclude_keywords),
        ("exclude_genres", exclude_genres),
    ):
        if _option_given(ctx, key):
            recipe[key] = list(value)

    if _option_given(ctx, "exclude_genres") and list(exclude_genres) != list(
        session.baseline_recipe.get("exclude_genres", [])
    ):
        raise click.UsageError(
            "Changing --exclude-genre requires category resolution. Rerun: "
            + session.source.get("command", f"deals {session.producer}")
        )

    _check_plus_flags(bool(recipe["skip_plus"]), bool(recipe["only_plus"]))
    if (
        recipe.get("require_history")
        and recipe.get("hist_below") is None
        and not recipe.get("min_price_drop")
    ):
        raise click.UsageError(
            "--require-history requires --hist-below or --min-price-drop"
        )

    def normalized_date(option: str) -> str:
        value = str(recipe.get(option) or "")
        if not value:
            return value
        try:
            return datetime.date.fromisoformat(value).isoformat()
        except ValueError:
            raise click.UsageError(
                f"--{option.replace('_', '-')}: invalid date {value!r} "
                "(expected YYYY-MM-DD)"
            )

    after = normalized_date("released_after")
    before = normalized_date("released_before")
    recipe["released_after"] = after
    recipe["released_before"] = before
    if after and before and after > before:
        raise click.UsageError(
            "--released-after cannot be later than --released-before"
        )
    if recipe.get("skip_owned") and not session.constraints.get(
        "owned_snapshot_available",
        bool(session.constraints.get("owned_asins")),
    ):
        raise click.UsageError(
            "This session has no cached ownership snapshot. Rerun: "
            + session.source.get("command", f"deals {session.producer}")
        )
    if recipe.get("exclude_seen") and not session.constraints.get(
        "seen_snapshot_available",
        "seen_asins" in session.constraints,
    ):
        raise click.UsageError(
            "This session has no cached seen-ASIN snapshot. Rerun: "
            + session.source.get("command", f"deals {session.producer}")
        )
    if recipe.get("exclude_genres") and not session.constraints.get(
        "category_snapshot_available",
        bool(session.constraints.get("excluded_category_ids")),
    ):
        raise click.UsageError(
            "This session has no cached category-exclusion snapshot. Rerun: "
            + session.source.get("command", f"deals {session.producer}")
        )

    credit_price = (
        session.constraints["credit_price"]
        if "credit_price" in session.constraints
        else _credit_price(ctx)
    )
    (
        filtered,
        breakdown,
        editions_removed,
        series_collapsed,
        histories,
        match_context,
    ) = apply_result_recipe(session, recipe, credit_price=credit_price)
    current_count = len(filtered)
    effective_limit = int(recipe.get("limit") or 0)
    visible = filtered[:effective_limit] if effective_limit > 0 else filtered

    if output:
        export_products(visible, output)
        console.print(f"[green]Exported {len(visible)} items to {output}[/green]")

    session.current_recipe = copy.deepcopy(recipe)
    session.constraints.setdefault("credit_price", credit_price)
    if not count_only or output:
        session.visible_asins = [product.asin for product in visible]
    save_result_session(session)

    if count_only:
        click.echo(current_count)
        return

    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)
    _record_and_emit(
        filtered,
        breakdown,
        editions_removed,
        series_collapsed,
        title=session.title,
        limit=effective_limit,
        output=None,
        json_flag=json_flag,
        quiet=quiet,
        max_price=recipe.get("max_price"),
        currency=LOCALE_CURRENCY.get(session.locale, _currency(ctx)),
        interactive=interactive,
        show_url=show_url,
        write_cache=False,
        record_prices=False,
        histories=histories,
        credit_price=credit_price,
        match_context=match_context,
    )
