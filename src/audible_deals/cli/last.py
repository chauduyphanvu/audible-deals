"""Cumulative, API-free cached result refinement."""

from __future__ import annotations

import datetime
import math

import click

from audible_deals.cli.helpers import _credit_price, _currency, _resolve_output_quiet
from audible_deals.cli.options import filter_options, output_options
from audible_deals.constants import LOCALE_CURRENCY
from audible_deals.exit_codes import ExitCode
from audible_deals.filter_specs import Refine, Scope, recipe_specs, specs_for
from audible_deals.notices import note
from audible_deals.presentation.result_output import (
    ResultPresentationRequest,
    emit_results,
)
from audible_deals.presentation.terminal import console, safe_markup
from audible_deals.result_models import UNSET, RecipePatch
from audible_deals.result_publication import (
    mark_refresh_eligible_safely,
    record_prices_safely,
)
from audible_deals.result_refinement import (
    CachedRefinementError,
    CachedRefinementRequest,
    FetchBoundPatch,
    refine_cached_results,
)
from audible_deals.results_cache import (
    clear_dismissed_asins,
    clear_last_results,
    clear_seen_asins,
    load_dismissed_asins,
    load_result_session,
    save_result_session,
)
from audible_deals.serialization import (
    export_products,
    serialize_product,
    validate_export_path,
)


def _clearable_filters() -> dict[str, str]:
    """``--clear-filter`` names -> recipe fields: every refinable filter except
    sort and limit, spelled by recipe field name and by its CLI flag."""
    names: dict[str, str] = {}
    for spec in recipe_specs():
        if spec.key in {"sort", "limit"}:
            continue
        names[spec.recipe_name.replace("_", "-")] = spec.recipe_name
        names[spec.flag_name] = spec.recipe_name
    return names


_CLEARABLE = _clearable_filters()


def _option_given(ctx: click.Context, name: str) -> bool:
    return ctx.get_parameter_source(name) == click.core.ParameterSource.COMMANDLINE


@click.command("last")
@filter_options(Scope.LAST)
@click.option(
    "--clear-filter",
    "clear_filters",
    type=click.Choice(sorted(_CLEARABLE)),
    multiple=True,
    help="Clear one inherited filter (repeatable)",
)
@click.option("--reset", is_flag=True, help="Restore the producer's original recipe")
@output_options("output", "json", "quiet", "show_url", "interactive", "exit_code")
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
@click.option("--subcategories/--no-subcategories", default=None, hidden=True)
@click.option("--refresh/--no-refresh", default=None, hidden=True)
@click.option("--max-series", type=click.IntRange(min=1), default=None, hidden=True)
@click.option("--min-books", type=click.IntRange(min=1), default=None, hidden=True)
@click.pass_context
def last_cmd(
    ctx,
    clear_filters,
    reset,
    output,
    json_flag,
    quiet,
    show_url,
    interactive,
    exit_code,
    clear,
    clear_seen,
    clear_dismissed,
    count_only,
    query,
    category,
    subcategories,
    refresh,
    max_series,
    min_books,
    **flags,
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
    recipe_updates = {}
    for spec in recipe_specs():
        param = spec.param_name(Scope.LAST)
        if _option_given(ctx, param):
            recipe_updates[spec.recipe_name] = flags[param]
    if session.producer == "series":
        recipe_updates.pop("series", None)
    fetch_values = {
        "query": query,
        "category": category,
        "subcategories": subcategories,
        "refresh": refresh,
        "max_series": max_series,
        "min_books": min_books,
        "series": flags["series"] if session.producer == "series" else UNSET,
    }
    for spec in specs_for(Scope.LAST):
        if spec.refine is Refine.REFETCH:
            fetch_values[spec.key] = flags[spec.param_name(Scope.LAST)]
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
        if exit_code and not outcome.total_count:
            ctx.exit(ExitCode.NO_MATCH)
        return
    if session.legacy and not json_flag and not quiet and not count_only:
        note(
            "Legacy limited cache: display, narrowing, and selectors work; "
            "run one new discovery scan to enable true widening."
        )
    assert outcome.visible_result is not None
    visible = outcome.visible_result.products

    if output:
        export_products(visible, output)
        console.print(
            f"[green]Exported {len(visible)} items to {safe_markup(output)}[/green]"
        )

    if count_only:
        if outcome.persist:
            save_result_session(outcome.session)
        click.echo(outcome.total_count)
        if exit_code and not outcome.total_count:
            ctx.exit(ExitCode.NO_MATCH)
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
    if exit_code and not visible:
        ctx.exit(ExitCode.NO_MATCH)
