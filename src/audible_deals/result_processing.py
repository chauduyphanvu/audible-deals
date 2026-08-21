"""Pure result recipe construction and discovery processing."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

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


@dataclass(frozen=True)
class DiscoveryProcessingRequest:
    products: tuple[Product, ...]
    context: FilterContext

    def __post_init__(self) -> None:
        object.__setattr__(self, "products", tuple(self.products))


@dataclass(frozen=True)
class SettingsFilterRequest:
    products: tuple[Product, ...]
    settings: Settings
    skip_asins: frozenset[str] | None = None
    exclude_category_ids: frozenset[str] = frozenset()
    hist_below: int | None = None
    min_price_drop: float = 0.0
    require_history: bool = False
    released_after: str = ""
    released_before: str = ""
    max_effective_price: float | None = None
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
        key: getattr(settings, key) for key in RECIPE_FIELDS if hasattr(settings, key)
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
        histories = {
            history_key(product.asin, product.locale): load_price_history(
                product.asin, product.locale
            )
            for product in request.products
            if product.price is not None
        }
        if context.max_hist_percentile is not None and hist_percentile is None:
            hist_percentile = hist_percentiles(list(request.products), histories)
        if context.min_price_drop > 0 and price_drops is None:
            price_drops = price_drop_pcts(list(request.products), histories)
    resolved_context = dataclasses.replace(
        context,
        hist_percentile=hist_percentile,
        price_drops=price_drops,
    )
    outcome = dedupe_editions(filter_products(list(request.products), resolved_context))
    if context.first_in_series_only:
        outcome = first_in_series(outcome)
    outcome = dataclasses.replace(
        outcome, products=tuple(sort_local(list(outcome.products), context.sort))
    )
    return DiscoveryResult.from_outcome(outcome, histories=histories)


def process_settings_discovery(request: SettingsFilterRequest) -> DiscoveryResult:
    settings = request.settings
    return process_discovery(
        DiscoveryProcessingRequest(
            request.products,
            FilterContext(
                max_price=settings.max_price,
                max_effective_price=request.max_effective_price,
                credit_price=request.credit_price,
                min_rating=settings.min_rating,
                min_ratings=settings.min_ratings,
                min_hours=settings.min_hours,
                narrator=settings.narrator,
                author=settings.author,
                exclude_authors=settings.exclude_authors,
                exclude_narrators=settings.exclude_narrators,
                language=settings.language,
                on_sale=settings.on_sale,
                skip_asins=request.skip_asins,
                exclude_category_ids=request.exclude_category_ids,
                first_in_series_only=settings.first_in_series,
                sort=settings.sort,
                max_pph=settings.max_pph,
                min_discount=settings.min_discount,
                series=settings.series,
                publisher=settings.publisher,
                skip_plus=settings.skip_plus,
                only_plus=settings.only_plus,
                exclude_keywords=settings.exclude_keywords,
                drop_zero_length=True,
                max_hist_percentile=request.hist_below,
                min_price_drop=request.min_price_drop,
                require_history=request.require_history,
                released_after=request.released_after,
                released_before=request.released_before,
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
            FilterContext(
                max_price=recipe.max_price,
                max_effective_price=recipe.max_effective_price,
                credit_price=credit_price,
                min_rating=float(recipe.min_rating or 0),
                min_ratings=int(recipe.min_ratings or 0),
                min_hours=float(recipe.min_hours or 0),
                narrator=recipe.narrator,
                author=recipe.author,
                exclude_authors=recipe.exclude_authors,
                exclude_narrators=recipe.exclude_narrators,
                language=recipe.language,
                on_sale=recipe.on_sale,
                skip_asins=skip_asins or None,
                exclude_category_ids=exclude_category_ids,
                first_in_series_only=recipe.first_in_series,
                sort=recipe.sort,
                max_pph=recipe.max_pph,
                min_discount=int(recipe.min_discount or 0),
                series=recipe.series,
                publisher=recipe.publisher,
                skip_plus=recipe.skip_plus,
                only_plus=recipe.only_plus,
                exclude_keywords=recipe.exclude_keywords,
                drop_zero_length=bool(constraints.get("drop_zero_length", True)),
                max_hist_percentile=recipe.hist_below,
                min_price_drop=float(recipe.min_price_drop or 0),
                require_history=recipe.require_history,
                released_after=recipe.released_after,
                released_before=recipe.released_before,
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
        result = dataclasses.replace(
            result,
            products=tuple(
                product for product in result.products if product.asin in allowed_set
            ),
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
