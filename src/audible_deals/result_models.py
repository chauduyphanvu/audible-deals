"""Typed models for result filtering, refinement, and discovery processing."""

from __future__ import annotations

import dataclasses
import datetime
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from audible_deals import constants
from audible_deals.product import Product


SESSION_VERSION = 2


@dataclasses.dataclass(frozen=True)
class CatalogScanPlan:
    """Immutable description of every catalog request in a scan."""

    queries: tuple[str, ...]
    category_ids: tuple[str, ...] | None
    sort_orders: tuple[str, ...]
    exact_probe_queries: tuple[str, ...]
    pages: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "queries", tuple(self.queries))
        object.__setattr__(
            self,
            "category_ids",
            None if self.category_ids is None else tuple(self.category_ids),
        )
        object.__setattr__(self, "sort_orders", tuple(self.sort_orders))
        object.__setattr__(self, "exact_probe_queries", tuple(self.exact_probe_queries))
        if (
            not isinstance(self.pages, int)
            or isinstance(self.pages, bool)
            or self.pages < 1
        ):
            raise ValueError("pages must be a positive integer")
        if not self.queries:
            raise ValueError("queries must not be empty")
        if not self.sort_orders or any(not sort for sort in self.sort_orders):
            raise ValueError("sort_orders must contain at least one non-empty value")
        if self.category_ids is not None and not self.category_ids:
            raise ValueError("category_ids must not be empty")
        if any(
            not query
            or self.exact_probe_queries.count(query) > self.queries.count(query)
            for query in self.exact_probe_queries
        ):
            raise ValueError("exact_probe_queries must match non-empty queries")

    @classmethod
    def create(
        cls,
        *,
        queries: Sequence[str],
        category_ids: Sequence[str] | None,
        sort_orders: Sequence[str],
        exact_probe_queries: Sequence[str] = (),
        pages: int,
    ) -> CatalogScanPlan:
        return cls(
            queries=tuple(queries),
            category_ids=None if category_ids is None else tuple(category_ids),
            sort_orders=tuple(sort_orders),
            exact_probe_queries=tuple(exact_probe_queries),
            pages=pages,
        )

    @property
    def category_multiplier(self) -> int | None:
        return None if self.category_ids is None else len(self.category_ids)

    @property
    def broad_calls(self) -> int | None:
        if self.category_multiplier is None:
            return None
        return (
            len(self.queries)
            * self.category_multiplier
            * len(self.sort_orders)
            * self.pages
        )

    @property
    def probe_calls(self) -> int | None:
        if self.category_multiplier is None:
            return None
        return len(self.exact_probe_queries) * self.category_multiplier

    @property
    def total_calls(self) -> int | None:
        if self.broad_calls is None or self.probe_calls is None:
            return None
        return self.broad_calls + self.probe_calls

    @property
    def max_items(self) -> int | None:
        if self.total_calls is None:
            return None
        return self.total_calls * constants.MAX_PAGE_SIZE


@dataclasses.dataclass(frozen=True)
class CatalogScanProgress:
    description: str
    total: int
    completed: int
    items: int


_RECIPE_TUPLE_FIELDS = {
    "exclude_authors",
    "exclude_narrators",
    "exclude_keywords",
    "exclude_genres",
}
_RECIPE_BOOL_FIELDS = {
    "on_sale",
    "first_in_series",
    "skip_plus",
    "only_plus",
    "require_history",
    "skip_owned",
    "exclude_seen",
}
_RECIPE_TEXT_FIELDS = {
    "narrator",
    "author",
    "series",
    "publisher",
    "language",
    "sort",
    "released_after",
    "released_before",
}
_RECIPE_NUMBER_FIELDS = {
    "max_price",
    "max_pph",
    "max_effective_price",
    "min_rating",
    "min_ratings",
    "min_hours",
    "min_discount",
    "limit",
    "hist_below",
    "min_price_drop",
}


class ResultSessionValidationError(ValueError):
    pass


def _require(value: Any, expected: type, field: str) -> Any:
    if not isinstance(value, expected):
        raise ResultSessionValidationError(
            f"Last results cache is corrupt: {field} must be {expected.__name__}."
        )
    return value


def _validate_recipe(recipe: dict[str, Any], field: str) -> None:
    for key in _RECIPE_TUPLE_FIELDS:
        value = recipe.get(key, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise ResultSessionValidationError(
                f"Last results cache is corrupt: {field}.{key} must be a string list."
            )
    for key in _RECIPE_BOOL_FIELDS:
        if key in recipe and not isinstance(recipe[key], bool):
            raise ResultSessionValidationError(
                f"Last results cache is corrupt: {field}.{key} must be boolean."
            )
    for key in _RECIPE_TEXT_FIELDS:
        if key in recipe and not isinstance(recipe[key], str):
            raise ResultSessionValidationError(
                f"Last results cache is corrupt: {field}.{key} must be text."
            )
    for key in _RECIPE_NUMBER_FIELDS:
        value = recipe.get(key)
        if value is not None and (
            not isinstance(value, (int, float)) or isinstance(value, bool)
        ):
            raise ResultSessionValidationError(
                f"Last results cache is corrupt: {field}.{key} must be numeric."
            )


@dataclasses.dataclass(frozen=True)
class ResultRecipe:
    max_price: float | None = None
    max_pph: float | None = None
    max_effective_price: float | None = None
    min_rating: float = 0.0
    min_ratings: int = 0
    min_hours: float = 0.0
    narrator: str = ""
    author: str = ""
    series: str = ""
    publisher: str = ""
    exclude_authors: tuple[str, ...] = ()
    exclude_narrators: tuple[str, ...] = ()
    language: str = ""
    on_sale: bool = False
    min_discount: int = 0
    first_in_series: bool = False
    sort: str = ""
    limit: int | None = 0
    skip_plus: bool = False
    only_plus: bool = False
    exclude_keywords: tuple[str, ...] = ()
    hist_below: int | None = None
    min_price_drop: float = 0.0
    require_history: bool = False
    released_after: str = ""
    released_before: str = ""
    skip_owned: bool = False
    exclude_seen: bool = False
    exclude_genres: tuple[str, ...] = ()
    _extra_fields: dataclasses.InitVar[Mapping[str, Any] | None] = None

    def __post_init__(self, _extra_fields: Mapping[str, Any] | None) -> None:
        for name in _RECIPE_TUPLE_FIELDS:
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(
            self,
            "_persisted_extra_fields",
            MappingProxyType(
                {
                    key: _freeze_json(value)
                    for key, value in (_extra_fields or {}).items()
                }
            ),
        )

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> ResultRecipe:
        known = _recipe_field_names(cls)
        unknown = set(values) - known
        if unknown:
            raise ValueError(f"Unknown result recipe field: {sorted(unknown)[0]}")
        return cls(**values)

    @classmethod
    def from_persisted_mapping(cls, values: Mapping[str, Any]) -> ResultRecipe:
        known = _recipe_field_names(cls)
        return cls(
            **{key: value for key, value in values.items() if key in known},
            _extra_fields={
                key: value for key, value in values.items() if key not in known
            },
        )

    def to_dict(self) -> dict[str, object]:
        values = {
            field.name: (
                list(getattr(self, field.name))
                if field.name in _RECIPE_TUPLE_FIELDS
                else getattr(self, field.name)
            )
            for field in dataclasses.fields(self)
            if not field.name.startswith("_")
        }
        values.update(
            {
                key: _thaw_json(value)
                for key, value in self._persisted_extra_fields.items()
            }
        )
        return values


def _recipe_field_names(recipe_type: type[ResultRecipe]) -> set[str]:
    return {
        field.name
        for field in dataclasses.fields(recipe_type)
        if not field.name.startswith("_")
    }


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclasses.dataclass
class ResultSession:
    """One persistent, locally refinable result set."""

    producer: str
    locale: str
    title: str
    source: dict[str, Any]
    candidates: list[dict]
    baseline_recipe: ResultRecipe
    current_recipe: ResultRecipe
    visible_asins: list[str]
    constraints: dict[str, Any] = dataclasses.field(default_factory=dict)
    ranking_context: dict[str, Any] = dataclasses.field(default_factory=dict)
    timestamp: str = dataclasses.field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    version: int = SESSION_VERSION
    legacy: bool = False

    @property
    def visible_results(self) -> list[dict]:
        by_asin = {
            item.get("asin"): item
            for item in self.candidates
            if isinstance(item, dict) and item.get("asin")
        }
        return [by_asin[asin] for asin in self.visible_asins if asin in by_asin]

    @classmethod
    def from_dict(cls, data: Any) -> ResultSession:
        if isinstance(data, list):
            return cls._from_legacy(data)
        if not isinstance(data, dict):
            raise ResultSessionValidationError("Last results cache is corrupt.")
        if data.get("version") != SESSION_VERSION:
            results = data.get("results")
            if isinstance(results, list):
                return cls._from_legacy(
                    results, str(data.get("title") or "Last results")
                )
            raise ResultSessionValidationError("Last results cache is corrupt.")

        producer = _require(data.get("producer"), str, "producer")
        locale = _require(data.get("locale"), str, "locale")
        title = _require(data.get("title"), str, "title")
        source = _require(data.get("source"), dict, "source")
        candidates = _require(data.get("candidates"), list, "candidates")
        baseline = _require(data.get("baseline_recipe"), dict, "baseline_recipe")
        current = _require(data.get("current_recipe"), dict, "current_recipe")
        visible = _require(data.get("visible_asins"), list, "visible_asins")
        constraints = _require(data.get("constraints", {}), dict, "constraints")
        ranking = _require(data.get("ranking_context", {}), dict, "ranking_context")
        legacy = _require(data.get("legacy", False), bool, "legacy")
        timestamp = _require(data.get("timestamp", ""), str, "timestamp")
        if locale not in constants.LOCALE_DOMAIN:
            raise ResultSessionValidationError(
                "Last results cache is corrupt: invalid locale."
            )
        if not isinstance(source.get("command"), str):
            raise ResultSessionValidationError(
                "Last results cache is corrupt: source.command must be text."
            )
        try:
            datetime.datetime.fromisoformat(timestamp)
        except ValueError:
            raise ResultSessionValidationError(
                "Last results cache is corrupt: invalid timestamp."
            ) from None
        _validate_recipe(baseline, "baseline_recipe")
        _validate_recipe(current, "current_recipe")
        if not all(isinstance(item, dict) for item in candidates):
            raise ResultSessionValidationError(
                "Last results cache is corrupt: candidates must be objects."
            )
        if any(
            not isinstance(item.get("asin"), str)
            or not item.get("asin")
            or not isinstance(item.get("title"), str)
            for item in candidates
        ):
            raise ResultSessionValidationError(
                "Last results cache is corrupt: every candidate needs an ASIN and title."
            )
        if any(
            "locale" in item
            and (
                not isinstance(item["locale"], str)
                or item["locale"] not in constants.LOCALE_DOMAIN
            )
            for item in candidates
        ):
            raise ResultSessionValidationError(
                "Last results cache is corrupt: candidate locale is invalid."
            )
        if not all(isinstance(asin, str) for asin in visible):
            raise ResultSessionValidationError(
                "Last results cache is corrupt: visible_asins must be strings."
            )
        candidate_asins = {
            item.get("asin") for item in candidates if isinstance(item.get("asin"), str)
        }
        if any(asin not in candidate_asins for asin in visible):
            raise ResultSessionValidationError(
                "Last results cache is corrupt: visible result is not in candidates."
            )
        credit_price = constraints.get("credit_price")
        if credit_price is not None and (
            not isinstance(credit_price, (int, float))
            or isinstance(credit_price, bool)
            or not math.isfinite(credit_price)
            or credit_price < 0
        ):
            raise ResultSessionValidationError(
                "Last results cache is corrupt: constraints.credit_price must be non-negative."
            )
        for field, value_type in (
            ("history_percentiles", int),
            ("price_drop_pcts", (int, float)),
        ):
            values = constraints.get(field)
            if values is not None and (
                not isinstance(values, dict)
                or any(
                    not isinstance(key, str)
                    or not isinstance(value, value_type)
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                    for key, value in values.items()
                )
            ):
                raise ResultSessionValidationError(
                    f"Last results cache is corrupt: constraints.{field} is invalid."
                )
        try:
            baseline_recipe = ResultRecipe.from_persisted_mapping(baseline)
            current_recipe = ResultRecipe.from_persisted_mapping(current)
        except (TypeError, ValueError):
            raise ResultSessionValidationError(
                "Last results cache is corrupt."
            ) from None
        return cls(
            producer=producer,
            locale=locale,
            title=title,
            source=source,
            candidates=candidates,
            baseline_recipe=baseline_recipe,
            current_recipe=current_recipe,
            visible_asins=visible,
            constraints=constraints,
            ranking_context=ranking,
            timestamp=timestamp,
            legacy=legacy,
        )

    @classmethod
    def _from_legacy(
        cls, data: list[dict], title: str = "Last results"
    ) -> ResultSession:
        if not all(isinstance(item, dict) for item in data):
            raise ResultSessionValidationError(
                "Last results cache is corrupt: results must be objects."
            )
        if any(
            not isinstance(item.get("asin"), str) or not item.get("asin")
            for item in data
        ):
            raise ResultSessionValidationError(
                "Last results cache contains an entry with no ASIN."
            )
        asins = [item["asin"] for item in data]
        locale = next(
            (
                item["locale"]
                for item in data
                if item.get("locale") in constants.LOCALE_DOMAIN
            ),
            "us",
        )
        return cls(
            producer="legacy",
            locale=locale,
            title=title,
            source={"command": "Run a new discovery command for true widening."},
            candidates=data,
            baseline_recipe=ResultRecipe(),
            current_recipe=ResultRecipe(),
            visible_asins=asins,
            legacy=True,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": SESSION_VERSION,
            "producer": self.producer,
            "locale": self.locale,
            "timestamp": self.timestamp,
            "title": self.title,
            "source": self.source,
            "candidates": self.candidates,
            "baseline_recipe": self.baseline_recipe.to_dict(),
            "current_recipe": self.current_recipe.to_dict(),
            "visible_asins": self.visible_asins,
            "constraints": self.constraints,
            "ranking_context": self.ranking_context,
            "legacy": self.legacy,
            "results": self.visible_results,
        }


class _Unset:
    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()


@dataclasses.dataclass(frozen=True)
class RecipePatch:
    max_price: float | None | _Unset = UNSET
    max_pph: float | None | _Unset = UNSET
    max_effective_price: float | None | _Unset = UNSET
    min_rating: float | _Unset = UNSET
    min_ratings: int | _Unset = UNSET
    min_hours: float | _Unset = UNSET
    narrator: str | _Unset = UNSET
    author: str | _Unset = UNSET
    series: str | _Unset = UNSET
    publisher: str | _Unset = UNSET
    exclude_authors: tuple[str, ...] | _Unset = UNSET
    exclude_narrators: tuple[str, ...] | _Unset = UNSET
    language: str | _Unset = UNSET
    on_sale: bool | _Unset = UNSET
    min_discount: int | _Unset = UNSET
    first_in_series: bool | _Unset = UNSET
    sort: str | _Unset = UNSET
    limit: int | None | _Unset = UNSET
    skip_plus: bool | _Unset = UNSET
    only_plus: bool | _Unset = UNSET
    exclude_keywords: tuple[str, ...] | _Unset = UNSET
    hist_below: int | None | _Unset = UNSET
    min_price_drop: float | _Unset = UNSET
    require_history: bool | _Unset = UNSET
    released_after: str | _Unset = UNSET
    released_before: str | _Unset = UNSET
    skip_owned: bool | _Unset = UNSET
    exclude_seen: bool | _Unset = UNSET
    exclude_genres: tuple[str, ...] | _Unset = UNSET

    def __post_init__(self) -> None:
        for name in _RECIPE_TUPLE_FIELDS:
            value = getattr(self, name)
            if value is not UNSET:
                object.__setattr__(self, name, tuple(value))

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> RecipePatch:
        known = {field.name for field in dataclasses.fields(cls)}
        unknown = set(values) - known
        if unknown:
            raise ValueError(f"Unknown result recipe field: {sorted(unknown)[0]}")
        return cls(**values)

    def merge(self, recipe: ResultRecipe) -> ResultRecipe:
        values = recipe.to_dict()
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if value is not UNSET:
                values[field.name] = value
        return ResultRecipe.from_persisted_mapping(values)


@dataclasses.dataclass(frozen=True)
class FilterContext:
    max_price: float | None = None
    max_effective_price: float | None = None
    credit_price: float | None = None
    min_rating: float = 0.0
    min_ratings: int = 0
    min_hours: float = 0.0
    language: str = ""
    narrator: str = ""
    author: str = ""
    exclude_authors: tuple[str, ...] = ()
    exclude_narrators: tuple[str, ...] = ()
    on_sale: bool = False
    skip_asins: frozenset[str] | None = None
    exclude_category_ids: frozenset[str] = frozenset()
    genre: str = ""
    max_pph: float | None = None
    min_discount: int = 0
    series: str = ""
    publisher: str = ""
    skip_plus: bool = False
    only_plus: bool = False
    exclude_keywords: tuple[str, ...] = ()
    drop_zero_length: bool = False
    max_hist_percentile: int | None = None
    hist_percentile: Mapping[str, int] | None = None
    min_price_drop: float = 0.0
    price_drops: Mapping[str, float] | None = None
    require_history: bool = False
    released_after: str = ""
    released_before: str = ""
    first_in_series_only: bool = False
    sort: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "exclude_authors", tuple(self.exclude_authors))
        object.__setattr__(self, "exclude_narrators", tuple(self.exclude_narrators))
        object.__setattr__(self, "exclude_keywords", tuple(self.exclude_keywords))
        if self.skip_asins is not None:
            object.__setattr__(self, "skip_asins", frozenset(self.skip_asins))
        object.__setattr__(
            self, "exclude_category_ids", frozenset(self.exclude_category_ids)
        )
        if self.hist_percentile is not None:
            object.__setattr__(
                self, "hist_percentile", MappingProxyType(dict(self.hist_percentile))
            )
        if self.price_drops is not None:
            object.__setattr__(
                self, "price_drops", MappingProxyType(dict(self.price_drops))
            )


@dataclasses.dataclass(frozen=True)
class FilterOutcome:
    products: tuple[Product, ...]
    breakdown: Mapping[str, int] = dataclasses.field(default_factory=dict)
    editions_removed: int = 0
    series_collapsed: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "products", tuple(self.products))
        object.__setattr__(self, "breakdown", MappingProxyType(dict(self.breakdown)))


HistoryEntries = Mapping[str, tuple[Mapping[str, Any], ...]]


def _freeze_histories(
    histories: Mapping[str, list[dict] | tuple[Mapping[str, Any], ...]] | None,
) -> HistoryEntries | None:
    if histories is None:
        return None
    return MappingProxyType(
        {
            key: tuple(MappingProxyType(dict(entry)) for entry in entries)
            for key, entries in histories.items()
        }
    )


@dataclasses.dataclass(frozen=True)
class DiscoveryResult:
    products: tuple[Product, ...]
    breakdown: Mapping[str, int] = dataclasses.field(default_factory=dict)
    editions_removed: int = 0
    series_collapsed: int = 0
    histories: HistoryEntries | None = None
    match_reasons: Mapping[str, str] | None = None
    atl_asins: frozenset[str] | None = None
    hist_context: Mapping[str, int] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "products", tuple(self.products))
        object.__setattr__(self, "breakdown", MappingProxyType(dict(self.breakdown)))
        object.__setattr__(self, "histories", _freeze_histories(self.histories))
        if self.match_reasons is not None:
            object.__setattr__(
                self, "match_reasons", MappingProxyType(dict(self.match_reasons))
            )
        if self.atl_asins is not None:
            object.__setattr__(self, "atl_asins", frozenset(self.atl_asins))
        if self.hist_context is not None:
            object.__setattr__(
                self, "hist_context", MappingProxyType(dict(self.hist_context))
            )

    @classmethod
    def from_outcome(
        cls,
        outcome: FilterOutcome,
        *,
        histories: Mapping[str, list[dict]] | None = None,
        match_reasons: Mapping[str, str] | None = None,
        atl_asins: set[str] | None = None,
        hist_context: Mapping[str, int] | None = None,
    ) -> DiscoveryResult:
        return cls(
            products=outcome.products,
            breakdown=outcome.breakdown,
            editions_removed=outcome.editions_removed,
            series_collapsed=outcome.series_collapsed,
            histories=histories,
            match_reasons=match_reasons,
            atl_asins=atl_asins,
            hist_context=hist_context,
        )
