"""Cumulative, API-free cached result refinement."""

from __future__ import annotations

from pathlib import Path
import datetime
import math

import click

from audible_deals.cli.helpers import _credit_price, _currency, _resolve_output_quiet
from audible_deals.presentation.result_output import (
    ResultPresentationRequest,
    emit_results,
)
from audible_deals.constants import LOCALE_CURRENCY
from audible_deals.presentation.terminal import console
from audible_deals.result_publication import (
    mark_refresh_eligible_safely,
    record_prices_safely,
)
from audible_deals.results_cache import (
    clear_dismissed_asins,
    clear_last_results,
    clear_seen_asins,
    load_dismissed_asins,
    load_result_session,
    save_result_session,
)
from audible_deals.result_models import RecipePatch, UNSET
from audible_deals.result_processing import RECIPE_FIELDS
from audible_deals.result_refinement import (
    CachedRefinementError,
    CachedRefinementRequest,
    FetchBoundPatch,
    refine_cached_results,
)
from audible_deals.serialization import (
    export_products,
    serialize_product,
    validate_export_path,
)
from audible_deals.validation import NONNEGATIVE_FLOAT, NONNEGATIVE_INT, RATING_FLOAT

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
    key.replace("_", "-"): key for key in RECIPE_FIELDS if key not in {"sort", "limit"}
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


@click.command("last")
@click.option("--sort", type=click.Choice(_SORTS), default=None, help="Re-sort results")
@click.option("--max-price", type=NONNEGATIVE_FLOAT, default=None)
@click.option("--max-price-per-hour", "max_pph", type=NONNEGATIVE_FLOAT, default=None)
@click.option(
    "--max-effective-price",
    "max_effective_price",
    type=NONNEGATIVE_FLOAT,
    default=None,
)
@click.option("--min-rating", type=RATING_FLOAT, default=None)
@click.option("--min-ratings", type=NONNEGATIVE_INT, default=None)
@click.option("--min-hours", type=NONNEGATIVE_FLOAT, default=None)
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
@click.option("--min-price-drop", type=NONNEGATIVE_FLOAT, default=None)
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
@click.option("--clear-dismissed", is_flag=True, default=False)
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
    clear_dismissed,
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
    if clear_dismissed:
        console.print(
            "[green]Dismissed ASINs list cleared.[/green]"
            if clear_dismissed_asins()
            else "[dim]No dismissed ASINs to clear.[/dim]"
        )
        did_clear = True
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
    previously_visible = set(session.visible_asins)
    session.constraints["always_skip_asins"] = sorted(load_dismissed_asins())
    override_values = {
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
    recipe_updates = {
        key: value for key, value in override_values.items() if _option_given(ctx, key)
    }
    if session.producer == "series":
        recipe_updates.pop("series", None)
    for key, value in (
        ("exclude_authors", exclude_authors),
        ("exclude_narrators", exclude_narrators),
        ("exclude_keywords", exclude_keywords),
        ("exclude_genres", exclude_genres),
    ):
        if _option_given(ctx, key):
            recipe_updates[key] = value
    fetch_values = {
        "query": query,
        "category": category,
        "genre": genre,
        "pages": pages,
        "deep": deep,
        "subcategories": subcategories,
        "refresh": refresh,
        "max_series": max_series,
        "min_books": min_books,
        "series": series if session.producer == "series" else UNSET,
    }
    fetch_patch = FetchBoundPatch(
        **{
            key: value if value is not UNSET and _option_given(ctx, key) else UNSET
            for key, value in fetch_values.items()
        }
    )
    request = CachedRefinementRequest(
        recipe_patch=RecipePatch.from_mapping(recipe_updates),
        fetch_bound_patch=fetch_patch,
        clear_fields=tuple(_CLEARABLE[name] for name in clear_filters),
        reset=reset,
        count_only=count_only,
        output_requested=output is not None,
        credit_price=_credit_price(ctx),
    )
    try:
        outcome = refine_cached_results(session, request)
    except CachedRefinementError as exc:
        raise click.UsageError(str(exc)) from None
    if outcome.legacy_raw_count:
        click.echo(outcome.total_count)
        return
    if session.legacy and not json_flag and not quiet and not count_only:
        console.print(
            "[yellow]Legacy limited cache: display, narrowing, and selectors work; "
            "run one new discovery scan to enable true widening.[/yellow]"
        )
    assert outcome.visible_result is not None
    visible = outcome.visible_result.products

    if output:
        export_products(visible, output)
        console.print(f"[green]Exported {len(visible)} items to {output}[/green]")

    if count_only:
        if outcome.persist:
            save_result_session(outcome.session)
        click.echo(outcome.total_count)
        return

    newly_visible = [
        product
        for product in visible
        if product.asin not in previously_visible
        and isinstance(product.price, (int, float))
        and not isinstance(product.price, bool)
        and math.isfinite(product.price)
    ]

    def commit_presentation() -> None:
        if outcome.persist:
            save_result_session(outcome.session)
        if newly_visible:
            if not session.legacy:
                observation_date = datetime.datetime.fromisoformat(
                    session.timestamp
                ).date()
                record_prices_safely(newly_visible, observation_date=observation_date)
            mark_refresh_eligible_safely(newly_visible)

    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)
    serialized = tuple(serialize_product(product) for product in visible)
    emit_results(
        ResultPresentationRequest(
            result=outcome.visible_result,
            serialized=serialized,
            title=session.title,
            json_flag=json_flag,
            quiet=quiet,
            max_price=outcome.session.current_recipe.max_price,
            total_before_limit=outcome.total_count,
            currency=LOCALE_CURRENCY.get(session.locale, _currency(ctx)),
            interactive=interactive,
            show_url=show_url,
            credit_price=outcome.credit_price,
            json_writer=click.echo,
            on_presented=commit_presentation,
        )
    )
