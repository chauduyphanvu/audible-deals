"""Pure result recipe construction and discovery processing."""

from __future__ import annotations

import dataclasses
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Any

from audible_deals.filter_specs import FILTERS, recipe_specs
from audible_deals.filtering import (
    dedupe_editions,
    filter_products,
    first_in_series,
    sort_local,
)
from audible_deals.price_history import (
    hist_percentiles,
    load_product_histories,
    price_drop_pcts,
)
from audible_deals.product import Product
from audible_deals.result_models import (
    DiscoveryResult,
    FilterContext,
    RecipePatch,
    ResultRecipe,
    ResultSession,
)
from audible_deals.serialization import deserialize_product
from audible_deals.settings import Settings

RECIPE_DEFAULTS = ResultRecipe()
RECIPE_FIELDS = tuple(field.name for field in dataclasses.fields(ResultRecipe))


def _filter_context(source: object, *, recipe: bool, **extra: Any) -> FilterContext:
    """Build a FilterContext from a Settings or ResultRecipe via the registry.

    Every registry filter that names a ``context_field`` is copied across, so
    adding a filter does not mean editing two parallel constructions.
    """
    values: dict[str, Any] = {}
    for spec in FILTERS:
        if spec.context_field is None:
            continue
        value = getattr(source, spec.recipe_name if recipe else spec.key)
        if spec.is_numeric and not spec.nullable:
            # A persisted recipe may carry None for "no minimum".
            value = (int if spec.integer else float)(value or 0)
        values[spec.context_field] = value
    return FilterContext(**values, **extra)


@dataclass(frozen=True)
class DiscoveryProcessingRequest:
    products: tuple[Product, ...]
    context: FilterContext
    dedupe_series_editions: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "products", tuple(self.products))


@dataclass(frozen=True)
class SettingsFilterRequest:
    products: tuple[Product, ...]
    settings: Settings
    # Any sets; they are frozen when the filter context is built.
    skip_asins: AbstractSet[str] | None = None
    exclude_category_ids: AbstractSet[str] = frozenset()
    credit_price: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "products", tuple(self.products))
        if self.skip_asins is not None:
            object.__setattr__(self, "skip_asins", frozenset(self.skip_asins))
        object.__setattr__(
            self, "exclude_category_ids", frozenset(self.exclude_category_ids)
        )


def result_recipe(**values: object) -> ResultRecipe:
    """Return a complete recipe, rejecting unknown fields."""
    return RecipePatch.from_mapping(values).merge(RECIPE_DEFAULTS)


def recipe_from_settings(settings: Settings, **values: object) -> ResultRecipe:
    recipe_values = {
        spec.recipe_name: getattr(settings, spec.key) for spec in recipe_specs()
    }
    recipe_values.update(values)
    return result_recipe(**recipe_values)


def process_discovery(request: DiscoveryProcessingRequest) -> DiscoveryResult:
    context = request.context
    histories: dict[str, list[dict]] | None = None
    hist_percentile = context.hist_percentile
    price_drops = context.price_drops
    if (context.max_hist_percentile is not None and hist_percentile is None) or (
        context.min_price_drop > 0 and price_drops is None
    ):
        histories = load_product_histories(request.products)
        if context.max_hist_percentile is not None and hist_percentile is None:
            hist_percentile = hist_percentiles(list(request.products), histories)
        if context.min_price_drop > 0 and price_drops is None:
            price_drops = price_drop_pcts(list(request.products), histories)
    resolved_context = dataclasses.replace(
        context,
        hist_percentile=hist_percentile,
        price_drops=price_drops,
    )
    outcome = filter_products(list(request.products), resolved_context)
    if request.dedupe_series_editions:
        outcome = dedupe_editions(outcome)
    if context.first_in_series_only:
        outcome = first_in_series(outcome)
    outcome = dataclasses.replace(
        outcome, products=tuple(sort_local(list(outcome.products), context.sort))
    )
    return DiscoveryResult.from_outcome(outcome, histories=histories)


def known_candidate_context(
    request: SettingsFilterRequest,
) -> FilterContext:
    """The filters that can be judged on a title's last-known data.

    Everything the user asked for except what depends on today's price: the
    price caps, the discount filters, price per hour and the history-based
    price filters. Those apply after the title is re-priced.
    """
    context = _filter_context(
        request.settings,
        recipe=False,
        credit_price=request.credit_price,
        skip_asins=request.skip_asins,
        exclude_category_ids=request.exclude_category_ids,
        drop_zero_length=True,
    )
    return dataclasses.replace(
        context,
        max_price=None,
        max_effective_price=None,
        on_sale=False,
        min_discount=0,
        max_pph=None,
        max_hist_percentile=None,
        hist_percentile=None,
        min_price_drop=0.0,
        price_drops=None,
        require_history=False,
    )


def process_settings_discovery(request: SettingsFilterRequest) -> DiscoveryResult:
    return process_discovery(
        DiscoveryProcessingRequest(
            request.products,
            _filter_context(
                request.settings,
                recipe=False,
                credit_price=request.credit_price,
                skip_asins=request.skip_asins,
                exclude_category_ids=request.exclude_category_ids,
                drop_zero_length=True,
            ),
        )
    )


def process_session_recipe(
    session: ResultSession,
    recipe: ResultRecipe,
    *,
    credit_price: float | None,
) -> DiscoveryResult:
    products = tuple(
        product
        for item in session.candidates
        if (product := deserialize_product(item)) is not None
    )
    constraints = session.constraints
    history_percentiles = constraints.get("history_percentiles")
    price_drops = constraints.get("price_drop_pcts")
    skip_asins: set[str] = set(constraints.get("always_skip_asins", []))
    if recipe.skip_owned:
        skip_asins.update(constraints.get("owned_asins", []))
    if recipe.exclude_seen:
        skip_asins.update(constraints.get("seen_asins", []))
    exclude_category_ids = (
        set(constraints.get("excluded_category_ids", []))
        if recipe.exclude_genres
        else set()
    )
    result = process_discovery(
        DiscoveryProcessingRequest(
            products,
            _filter_context(
                recipe,
                recipe=True,
                credit_price=credit_price,
                skip_asins=skip_asins or None,
                exclude_category_ids=exclude_category_ids,
                drop_zero_length=bool(constraints.get("drop_zero_length", True)),
                hist_percentile=(
                    history_percentiles
                    if isinstance(history_percentiles, dict)
                    else None
                ),
                price_drops=price_drops if isinstance(price_drops, dict) else None,
            ),
        )
    )
    allowed = session.ranking_context.get("allowed_asins")
    if isinstance(allowed, list):
        allowed_set = set(allowed)
        filtered_products = tuple(
            product for product in result.products if product.asin in allowed_set
        )
        if not recipe.sort:
            by_asin = {product.asin: product for product in filtered_products}
            filtered_products = tuple(
                by_asin[asin] for asin in allowed if asin in by_asin
            )
        result = dataclasses.replace(
            result,
            products=filtered_products,
        )
    match_reasons = session.ranking_context.get("match_reasons")
    match_context = (
        {
            product.asin: str(match_reasons.get(product.asin, ""))
            for product in result.products
        }
        if isinstance(match_reasons, dict)
        else None
    )
    return dataclasses.replace(result, match_reasons=match_context)
