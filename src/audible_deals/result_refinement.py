"""API-free refinement of cached result sessions."""

from __future__ import annotations

import copy
import dataclasses
import datetime
from dataclasses import dataclass, field

from audible_deals.result_models import (
    DiscoveryResult,
    RecipePatch,
    ResultSession,
    UNSET,
)
from audible_deals.result_processing import (
    RECIPE_DEFAULTS,
    process_session_recipe,
)
from audible_deals.settings import resolve_plus_flags


class CachedRefinementError(ValueError):
    pass


class FetchBoundRefinementError(CachedRefinementError):
    pass


class CachedRefinementValidationError(CachedRefinementError):
    pass


@dataclass(frozen=True)
class FetchBoundPatch:
    query: object = UNSET
    category: object = UNSET
    genre: object = UNSET
    pages: object = UNSET
    deep: object = UNSET
    subcategories: object = UNSET
    refresh: object = UNSET
    max_series: object = UNSET
    min_books: object = UNSET
    series: object = UNSET


@dataclass(frozen=True)
class CachedRefinementRequest:
    recipe_patch: RecipePatch = field(default_factory=RecipePatch)
    fetch_bound_patch: FetchBoundPatch = field(default_factory=FetchBoundPatch)
    clear_fields: tuple[str, ...] = ()
    reset: bool = False
    count_only: bool = False
    output_requested: bool = False
    credit_price: float | None | object = UNSET

    def __post_init__(self) -> None:
        object.__setattr__(self, "clear_fields", tuple(self.clear_fields))


@dataclass(frozen=True)
class CachedRefinementOutcome:
    result: DiscoveryResult | None
    visible_result: DiscoveryResult | None
    session: ResultSession
    total_count: int
    persist: bool
    legacy_raw_count: bool = False
    credit_price: float | None = None


def _is_patch_empty(patch: object) -> bool:
    return all(getattr(patch, item.name) is UNSET for item in dataclasses.fields(patch))


def _rerun_command(session: ResultSession) -> str:
    return session.source.get("command", f"deals {session.producer}")


def _validate_fetch_bound(session: ResultSession, patch: FetchBoundPatch) -> None:
    for item in dataclasses.fields(patch):
        value = getattr(patch, item.name)
        if value is UNSET:
            continue
        if item.name == "series" and session.producer == "series":
            raise FetchBoundRefinementError(
                "--series selects which series must be fetched for this session. "
                f"Rerun: {_rerun_command(session)}"
            )
        if value == session.source.get(item.name):
            continue
        raise FetchBoundRefinementError(
            f"--{item.name.replace('_', '-')} changes what must be fetched and cannot "
            f"refine cached results. Rerun: {_rerun_command(session)}"
        )


def _normalize_date(recipe, option: str) -> str:
    value = str(getattr(recipe, option) or "")
    if not value:
        return value
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError:
        raise CachedRefinementValidationError(
            f"--{option.replace('_', '-')}: invalid date {value!r} "
            "(expected YYYY-MM-DD)"
        ) from None


def _fresh_session(
    session: ResultSession,
    *,
    recipe,
    visible_asins: list[str],
    credit_price: float | None,
) -> ResultSession:
    constraints = copy.deepcopy(session.constraints)
    constraints.setdefault("credit_price", credit_price)
    return ResultSession(
        producer=session.producer,
        locale=session.locale,
        title=session.title,
        source=copy.deepcopy(session.source),
        candidates=copy.deepcopy(session.candidates),
        baseline_recipe=session.baseline_recipe,
        current_recipe=recipe,
        visible_asins=list(visible_asins),
        constraints=constraints,
        ranking_context=copy.deepcopy(session.ranking_context),
        timestamp=session.timestamp,
        version=session.version,
        legacy=session.legacy,
    )


def refine_cached_results(
    session: ResultSession, request: CachedRefinementRequest
) -> CachedRefinementOutcome:
    any_refinement = (
        request.reset
        or bool(request.clear_fields)
        or not _is_patch_empty(request.recipe_patch)
    )
    if (
        request.count_only
        and session.legacy
        and not any_refinement
        and not session.constraints.get("always_skip_asins")
    ):
        return CachedRefinementOutcome(
            result=None,
            visible_result=None,
            session=session,
            total_count=len(session.visible_asins),
            persist=False,
            legacy_raw_count=True,
        )

    _validate_fetch_bound(session, request.fetch_bound_patch)
    recipe = session.baseline_recipe if request.reset else session.current_recipe
    clear_values: dict[str, object] = {}
    for name in request.clear_fields:
        if not hasattr(RECIPE_DEFAULTS, name):
            raise CachedRefinementValidationError(f"Unknown result filter: {name}")
        clear_values[name] = getattr(RECIPE_DEFAULTS, name)
    recipe = RecipePatch.from_mapping(clear_values).merge(recipe)
    recipe = request.recipe_patch.merge(recipe)
    try:
        skip_plus, only_plus = resolve_plus_flags(
            recipe.skip_plus,
            recipe.only_plus,
            skip_rank=int(request.recipe_patch.skip_plus is True),
            only_rank=int(request.recipe_patch.only_plus is True),
        )
    except ValueError as exc:
        raise CachedRefinementValidationError(str(exc)) from None
    if (skip_plus, only_plus) != (recipe.skip_plus, recipe.only_plus):
        recipe = RecipePatch(skip_plus=skip_plus, only_plus=only_plus).merge(recipe)

    if (
        request.recipe_patch.exclude_genres is not UNSET
        and tuple(request.recipe_patch.exclude_genres)
        != session.baseline_recipe.exclude_genres
    ):
        raise CachedRefinementValidationError(
            "Changing --exclude-genre requires category resolution. Rerun: "
            + _rerun_command(session)
        )
    if (
        recipe.require_history
        and recipe.hist_below is None
        and not recipe.min_price_drop
    ):
        raise CachedRefinementValidationError(
            "--require-history requires --hist-below or --min-price-drop"
        )
    after = _normalize_date(recipe, "released_after")
    before = _normalize_date(recipe, "released_before")
    recipe = RecipePatch(released_after=after, released_before=before).merge(recipe)
    if after and before and after > before:
        raise CachedRefinementValidationError(
            "--released-after cannot be later than --released-before"
        )
    if recipe.skip_owned and not session.constraints.get(
        "owned_snapshot_available", bool(session.constraints.get("owned_asins"))
    ):
        raise CachedRefinementValidationError(
            "This session has no cached ownership snapshot. Rerun: "
            + _rerun_command(session)
        )
    if recipe.exclude_seen and not session.constraints.get(
        "seen_snapshot_available", "seen_asins" in session.constraints
    ):
        raise CachedRefinementValidationError(
            "This session has no cached seen-ASIN snapshot. Rerun: "
            + _rerun_command(session)
        )
    if recipe.exclude_genres and not session.constraints.get(
        "category_snapshot_available",
        bool(session.constraints.get("excluded_category_ids")),
    ):
        raise CachedRefinementValidationError(
            "This session has no cached category-exclusion snapshot. Rerun: "
            + _rerun_command(session)
        )

    credit_price = (
        session.constraints["credit_price"]
        if "credit_price" in session.constraints
        else (None if request.credit_price is UNSET else request.credit_price)
    )
    result = process_session_recipe(
        session,
        recipe,
        credit_price=credit_price,
    )
    total_count = len(result.products)
    effective_limit = int(recipe.limit or 0)
    visible_products = (
        result.products[:effective_limit] if effective_limit > 0 else result.products
    )
    visible_result = dataclasses.replace(result, products=visible_products)
    visible_asins = (
        [product.asin for product in visible_products]
        if not request.count_only or request.output_requested
        else list(session.visible_asins)
    )
    new_session = _fresh_session(
        session,
        recipe=recipe,
        visible_asins=visible_asins,
        credit_price=credit_price,
    )
    return CachedRefinementOutcome(
        result=result,
        visible_result=visible_result,
        session=new_session,
        total_count=total_count,
        persist=True,
        credit_price=credit_price,
    )
