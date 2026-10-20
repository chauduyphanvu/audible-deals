"""The filter registry: the one place a filter's flag, type, bounds, help, default
and availability are declared.

Everything that used to be copied by hand into the click commands, the config
schema, profile validation, monitor settings, ``deals last`` and the docs is
derived from :data:`FILTERS`:

* :func:`audible_deals.cli.options.filter_options` builds the click options of
  ``find``, ``search``, ``for-me``, ``series``, ``last``, ``profile save`` and
  ``monitor add`` for one :class:`Scope`.
* :data:`CONFIG_FILTER_SCHEMA`, :func:`config_bounds` and the config coercion in
  ``config_store`` come from the specs available in ``Scope.CONFIG``.
* ``result_processing`` copies ``Settings``/recipe values into ``FilterContext``
  through each spec's ``context_field``; ``--dry-run``'s active-filter list uses
  :func:`active_filter_labels`.
* ``settings.profile_validation_error`` and ``Settings`` validation use the
  kind, bounds and choices declared here.
* ``result_models`` derives its recipe field groups from :data:`FILTERS`.

``Settings`` and ``ResultRecipe`` stay explicit typed dataclasses; the drift
tests in ``tests/test_filter_specs.py`` fail when they, the config schema or the
docs stop matching this registry.

This module is stdlib-only (plus the constants leaf) so it is safe to import on
the startup path. Adding a filter: add a :class:`FilterSpec` to :data:`FILTERS`,
add the matching ``Settings``/``ResultRecipe``/``RecipePatch``/``FilterContext``
fields, write the predicate in ``filtering.py``, add the docs line, regenerate
the golden option names, and add tests (see CONTRIBUTING.md).
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from audible_deals.constants import (
    ALL_SORT_OPTIONS,
    CLIENT_SORT_OPTIONS,
    DEFAULT_LIMIT,
    DEFAULT_SORT,
    SORT_OPTIONS,
)


class Kind(enum.Enum):
    FLOAT = "float"  # finite float, optional bounds
    INT = "int"  # integer, optional bounds
    PERCENT = "percent"  # integer 0-100
    RATING = "rating"  # float 0-5
    TEXT = "text"  # one string (optionally restricted to ``choices``)
    MULTI_TEXT = "multi_text"  # repeatable string option, stored as a tuple
    BOOL_PAIR = "bool_pair"  # --x/--no-x
    FLAG = "flag"  # --x only (a per-scope presentation of a boolean)
    DATE = "date"  # YYYY-MM-DD, empty string means unset


class Scope(enum.Enum):
    FIND = "find"
    SEARCH = "search"
    LAST = "last"
    FOR_ME = "for-me"
    SERIES = "series"
    PROFILE = "profile"
    CONFIG = "config"
    MONITOR = "monitor"


class Refine(enum.Enum):
    """How ``deals last`` can honor a filter on a cached result session."""

    LOCAL = "local"  # re-applied to the cached candidates (a recipe field)
    REFETCH = "refetch"  # changes what is fetched; only a matching rerun works
    NONE = "none"  # not applicable to cached results


class _Inherit:
    __slots__ = ()

    def __repr__(self) -> str:
        return "INHERIT"


INHERIT: Any = _Inherit()

# Scopes whose commands resolve a full Settings object from defaults. The
# others (last, profile, monitor) only look at explicitly passed values, so
# their options default to "not given".
RESOLVING_SCOPES = frozenset({Scope.FIND, Scope.SEARCH, Scope.FOR_ME, Scope.SERIES})
CLI_SCOPES = frozenset(Scope) - {Scope.CONFIG}


@dataclass(frozen=True)
class ScopeVariant:
    """How one scope differs from the spec's base declaration."""

    default: Any = INHERIT
    help: str | None = None
    kind: Kind | None = None
    choices: tuple[str, ...] | None = None
    param: str | None = None
    hidden: bool = False


@dataclass(frozen=True)
class FilterSpec:
    key: str  # Settings field (and profile/config key)
    flag: str  # canonical long option, e.g. "--max-price-per-hour"
    kind: Kind
    help: str = ""
    default: Any = None  # the Settings default
    minimum: float | None = None
    maximum: float | None = None
    aliases: tuple[str, ...] = ()  # extra option strings, e.g. ("-n",)
    metavar: str | None = None
    valid: frozenset[str] | None = None  # every accepted value of a TEXT filter
    scopes: frozenset[Scope] = frozenset()
    variants: Mapping[Scope, ScopeVariant] = field(default_factory=dict)
    refine: Refine = Refine.LOCAL
    recipe_key: str | None = None  # ResultRecipe field if it differs from key
    context_field: str | None = None  # FilterContext field that applies it
    label: str | None = None  # name shown in --dry-run "active filters"
    complete: str | None = None  # shell-completion provider ("genre")

    # -- derived --------------------------------------------------------

    @property
    def flag_name(self) -> str:
        return self.flag.lstrip("-")

    @property
    def recipe_name(self) -> str:
        return self.recipe_key or self.key

    @property
    def integer(self) -> bool:
        return self.kind in (Kind.INT, Kind.PERCENT)

    @property
    def is_bool(self) -> bool:
        return self.kind in (Kind.BOOL_PAIR, Kind.FLAG)

    @property
    def is_multi(self) -> bool:
        return self.kind is Kind.MULTI_TEXT

    @property
    def is_numeric(self) -> bool:
        return self.kind in (Kind.FLOAT, Kind.INT, Kind.PERCENT, Kind.RATING)

    @property
    def nullable(self) -> bool:
        """True when "unset" is ``None`` rather than an empty/zero value."""
        return self.default is None

    @property
    def lower_bound(self) -> float | None:
        if self.kind is Kind.PERCENT and self.minimum is None:
            return 0
        if self.kind is Kind.RATING and self.minimum is None:
            return 0
        return self.minimum

    @property
    def upper_bound(self) -> float | None:
        if self.kind is Kind.PERCENT and self.maximum is None:
            return 100
        if self.kind is Kind.RATING and self.maximum is None:
            return 5
        return self.maximum

    @property
    def config_type(self) -> type:
        if self.is_bool:
            return bool
        if self.kind in (Kind.FLOAT, Kind.RATING):
            return float
        if self.kind in (Kind.INT, Kind.PERCENT):
            return int
        return str

    def variant(self, scope: Scope) -> ScopeVariant:
        return self.variants.get(scope, _NO_VARIANT)

    def param_name(self, scope: Scope) -> str:
        return self.variant(scope).param or self.key

    def scope_kind(self, scope: Scope) -> Kind:
        return self.variant(scope).kind or self.kind

    def scope_help(self, scope: Scope) -> str:
        override = self.variant(scope).help
        return self.help if override is None else override

    def scope_choices(self, scope: Scope) -> tuple[str, ...] | None:
        return self.variant(scope).choices

    def scope_default(self, scope: Scope) -> Any:
        """The click default of the option in *scope*."""
        if scope not in RESOLVING_SCOPES:
            return None
        override = self.variant(scope).default
        return self.default if override is INHERIT else override

    def option_strings(self, scope: Scope) -> list[str]:
        kind = self.scope_kind(scope)
        if kind is Kind.BOOL_PAIR:
            declared = [f"{self.flag}/--no-{self.flag_name}"]
        else:
            declared = [self.flag]
        return declared + list(self.aliases)


_NO_VARIANT = ScopeVariant()


def _spec(
    key: str,
    flag: str,
    kind: Kind,
    *,
    scopes: set[Scope],
    variants: Mapping[Scope, ScopeVariant] | None = None,
    **kwargs: Any,
) -> FilterSpec:
    return FilterSpec(
        key=key,
        flag=flag,
        kind=kind,
        scopes=frozenset(scopes),
        variants=MappingProxyType(dict(variants or {})),
        **kwargs,
    )


S = Scope
_V = ScopeVariant
_ALL_CLI = {S.FIND, S.SEARCH, S.LAST, S.PROFILE, S.MONITOR}
_FIND_LIKE = {S.FIND, S.SEARCH}

# Sort choices per command (kept exactly as each command accepted them).
_FIND_SORTS = tuple(sorted(CLIENT_SORT_OPTIONS)) + tuple(SORT_OPTIONS)
_SEARCH_SORTS = tuple(SORT_OPTIONS) + tuple(sorted(CLIENT_SORT_OPTIONS))
_FOR_ME_SORTS = (
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
)
_SERIES_SORTS = (
    "price",
    "-price",
    "discount",
    "price-per-hour",
    "rating",
    "length",
    "date",
    "title",
)
_LAST_SORTS = (*_FOR_ME_SORTS, "relevance")

_FIND_GENRE_HELP = "Genre name (fuzzy match, e.g. 'sci-fi', 'mystery', 'romance')"

# Registry order is the order of --help output and of the --dry-run
# "active filters" line.
FILTERS: tuple[FilterSpec, ...] = (
    # -- what to scan -----------------------------------------------------
    _spec(
        "genre",
        "--genre",
        Kind.TEXT,
        default="",
        help="Genre name to search within (fuzzy match, e.g. 'sci-fi')",
        scopes={S.FIND, S.SEARCH, S.LAST, S.PROFILE, S.MONITOR},
        variants={
            S.FIND: _V(help=_FIND_GENRE_HELP),
            S.LAST: _V(hidden=True),
        },
        refine=Refine.REFETCH,
        complete="genre",
    ),
    _spec(
        "keywords",
        "--keywords",
        Kind.TEXT,
        default="",
        help="Optional keyword filter within the category",
        scopes={S.FIND, S.PROFILE},
        refine=Refine.NONE,
    ),
    _spec(
        "sort",
        "--sort",
        Kind.TEXT,
        default=DEFAULT_SORT,
        help="Sort order (price/discount/price-per-hour/value are client-side)",
        valid=ALL_SORT_OPTIONS,
        scopes={S.FIND, S.SEARCH, S.LAST, S.FOR_ME, S.SERIES, S.PROFILE, S.CONFIG}
        | {S.MONITOR},
        variants={
            S.FIND: _V(choices=_FIND_SORTS),
            S.SEARCH: _V(choices=_SEARCH_SORTS, default="relevance"),
            S.FOR_ME: _V(
                choices=_FOR_ME_SORTS,
                default=None,
                help="Re-sort results after fit ranking (default: fit rank order)",
            ),
            S.SERIES: _V(
                choices=_SERIES_SORTS, help="Sort order (default: price-per-hour)"
            ),
            S.LAST: _V(choices=_LAST_SORTS, help="Re-sort results"),
            S.MONITOR: _V(choices=tuple(sorted(ALL_SORT_OPTIONS))),
        },
        context_field="sort",
    ),
    _spec(
        "pages",
        "--pages",
        Kind.INT,
        default=10,
        minimum=1,
        help="Number of pages to scan (50 items/page)",
        scopes={S.FIND, S.SEARCH, S.LAST, S.SERIES, S.PROFILE, S.CONFIG, S.MONITOR},
        variants={
            S.FIND: _V(
                help="Pages to scan per sort order (50 items/page, default: 10)"
            ),
            S.SEARCH: _V(default=3),
            S.SERIES: _V(
                default=3, help="Pages to scan per series search (default: 3)"
            ),
            S.LAST: _V(hidden=True),
        },
        refine=Refine.REFETCH,
    ),
    _spec(
        "deep",
        "--deep",
        Kind.BOOL_PAIR,
        default=False,
        help="Scan with 3 sort orders for better coverage (3x API calls)",
        scopes={S.FIND, S.SEARCH, S.LAST, S.PROFILE, S.CONFIG, S.MONITOR},
        variants={S.LAST: _V(hidden=True)},
        refine=Refine.REFETCH,
    ),
    _spec(
        "limit",
        "--limit",
        Kind.INT,
        aliases=("-n",),
        default=DEFAULT_LIMIT,
        minimum=0,
        help="Show only the top N results (0 for unlimited, default: 25)",
        scopes={S.FIND, S.SEARCH, S.LAST, S.FOR_ME, S.SERIES, S.PROFILE, S.CONFIG},
        variants={S.LAST: _V(help="Show only the top N results (0 for unlimited)")},
    ),
    # -- price ----------------------------------------------------------
    _spec(
        "max_price",
        "--max-price",
        Kind.FLOAT,
        minimum=0,
        help="Max price filter",
        scopes={
            S.FIND,
            S.SEARCH,
            S.LAST,
            S.FOR_ME,
            S.SERIES,
            S.PROFILE,
            S.CONFIG,
            S.MONITOR,
        },
        variants={
            S.FIND: _V(default=5.0, help="Max price threshold (default: $5.00)"),
            S.SEARCH: _V(help="Max price filter (e.g. 5.00)"),
        },
        context_field="max_price",
        label="max-price",
    ),
    _spec(
        "max_pph",
        "--max-price-per-hour",
        Kind.FLOAT,
        minimum=0,
        help="Max price per hour (e.g. 0.50)",
        scopes=_ALL_CLI | {S.CONFIG},
        context_field="max_pph",
        label="max-price-per-hour",
    ),
    _spec(
        "max_effective_price",
        "--max-effective-price",
        Kind.FLOAT,
        minimum=0,
        help=(
            "Max effective price — the cheaper of cash price and one credit "
            "(set 'deals config set credit-price' first)"
        ),
        scopes=_ALL_CLI | {S.FOR_ME, S.CONFIG},
        variants={
            S.FOR_ME: _V(
                help="Max effective price — the cheaper of cash price and one credit"
            )
        },
        context_field="max_effective_price",
        label="max-effective-price",
    ),
    # -- quality ----------------------------------------------------------
    _spec(
        "min_rating",
        "--min-rating",
        Kind.RATING,
        default=0.0,
        help="Minimum rating (e.g. 4.0)",
        scopes=_ALL_CLI | {S.FOR_ME, S.SERIES, S.CONFIG},
        variants={S.FOR_ME: _V(help="Minimum rating")},
        context_field="min_rating",
        label="min-rating",
    ),
    _spec(
        "min_ratings",
        "--min-ratings",
        Kind.INT,
        default=0,
        minimum=0,
        help="Minimum number of ratings (e.g. 100)",
        scopes=_ALL_CLI | {S.FOR_ME, S.SERIES, S.CONFIG},
        variants={
            S.FIND: _V(
                default=1,
                help="Minimum number of ratings (default: 1, filters unreviewed)",
            ),
            S.FOR_ME: _V(help="Minimum number of ratings"),
            S.SERIES: _V(help="Minimum number of ratings"),
        },
        context_field="min_ratings",
        label="min-ratings",
    ),
    _spec(
        "min_hours",
        "--min-hours",
        Kind.FLOAT,
        default=0.0,
        minimum=0,
        help="Minimum length in hours",
        scopes=_ALL_CLI | {S.FOR_ME, S.SERIES, S.CONFIG},
        variants={S.FIND: _V(help="Minimum length in hours (filters out shorts)")},
        context_field="min_hours",
        label="min-hours",
    ),
    _spec(
        "min_discount",
        "--min-discount",
        Kind.PERCENT,
        default=0,
        help="Minimum discount percentage (e.g. 70)",
        scopes=_ALL_CLI | {S.CONFIG},
        context_field="min_discount",
        label="min-discount",
    ),
    # -- text ---------------------------------------------------------
    _spec(
        "language",
        "--language",
        Kind.TEXT,
        default="",
        help="Language filter (e.g. english)",
        scopes=_ALL_CLI | {S.CONFIG},
        context_field="language",
        label="language",
    ),
    _spec(
        "all_languages",
        "--all-languages",
        Kind.BOOL_PAIR,
        default=False,
        help="Include all languages (default: locale language only)",
        scopes={S.FIND, S.SEARCH, S.PROFILE, S.CONFIG, S.MONITOR},
        refine=Refine.NONE,
    ),
    _spec(
        "author",
        "--author",
        Kind.TEXT,
        default="",
        help="Filter by author name (substring match)",
        scopes=_ALL_CLI | {S.CONFIG},
        context_field="author",
        label="author",
    ),
    _spec(
        "narrator",
        "--narrator",
        Kind.TEXT,
        default="",
        help="Filter by narrator name (substring match, client-side)",
        scopes=_ALL_CLI | {S.FOR_ME, S.CONFIG},
        context_field="narrator",
        label="narrator",
    ),
    _spec(
        "series",
        "--series",
        Kind.TEXT,
        default="",
        help="Filter by series name (substring match)",
        scopes=_ALL_CLI | {S.CONFIG},
        context_field="series",
        label="series",
    ),
    _spec(
        "publisher",
        "--publisher",
        Kind.TEXT,
        default="",
        help="Filter by publisher name (substring match)",
        scopes=_ALL_CLI | {S.CONFIG},
        context_field="publisher",
        label="publisher",
    ),
    # -- collection --------------------------------------------------------
    _spec(
        "on_sale",
        "--on-sale",
        Kind.BOOL_PAIR,
        default=False,
        help="Only show discounted items",
        scopes=_ALL_CLI | {S.FOR_ME, S.SERIES, S.CONFIG},
        variants={S.SERIES: _V(kind=Kind.FLAG)},
        context_field="on_sale",
        label="on-sale",
    ),
    _spec(
        "first_in_series",
        "--first-in-series",
        Kind.BOOL_PAIR,
        default=False,
        help="Show only the first book per series",
        scopes=_ALL_CLI | {S.CONFIG},
        context_field="first_in_series_only",
        label="first-in-series",
    ),
    _spec(
        "skip_owned",
        "--skip-owned",
        Kind.BOOL_PAIR,
        default=False,
        help="Exclude books already in your library",
        scopes=_ALL_CLI | {S.CONFIG},
        label="skip-owned",
    ),
    _spec(
        "skip_plus",
        "--skip-plus",
        Kind.BOOL_PAIR,
        default=False,
        help="Exclude Audible Plus catalog titles",
        scopes=_ALL_CLI | {S.FOR_ME, S.CONFIG},
        context_field="skip_plus",
        label="skip-plus",
    ),
    _spec(
        "only_plus",
        "--only-plus",
        Kind.BOOL_PAIR,
        default=False,
        help="Show only Audible Plus catalog titles",
        scopes=_ALL_CLI | {S.FOR_ME, S.CONFIG},
        context_field="only_plus",
        label="only-plus",
    ),
    _spec(
        "exclude_genre",
        "--exclude-genre",
        Kind.MULTI_TEXT,
        default=(),
        help="Genre(s) to exclude (repeatable, fuzzy match)",
        scopes={S.FIND, S.SEARCH, S.LAST, S.PROFILE, S.MONITOR},
        variants={S.LAST: _V(param="exclude_genres")},
        recipe_key="exclude_genres",
        label="exclude-genres",
        complete="genre",
    ),
    _spec(
        "exclude_authors",
        "--exclude-author",
        Kind.MULTI_TEXT,
        default=(),
        help="Exclude author (substring match, repeatable)",
        scopes=_ALL_CLI | {S.FOR_ME},
        context_field="exclude_authors",
        label="exclude-authors",
    ),
    _spec(
        "exclude_narrators",
        "--exclude-narrator",
        Kind.MULTI_TEXT,
        default=(),
        help="Exclude narrator (substring match, repeatable)",
        scopes=_ALL_CLI | {S.FOR_ME},
        context_field="exclude_narrators",
        label="exclude-narrators",
    ),
    _spec(
        "exclude_keywords",
        "--exclude-keyword",
        Kind.MULTI_TEXT,
        default=(),
        help="Drop results with title/subtitle matching keyword (repeatable)",
        scopes=_ALL_CLI,
        context_field="exclude_keywords",
        label="exclude-keywords",
    ),
    _spec(
        "exclude_seen",
        "--exclude-seen",
        Kind.BOOL_PAIR,
        default=False,
        help="Exclude ASINs from last search/find results",
        scopes={S.FIND, S.SEARCH, S.LAST, S.PROFILE},
        label="exclude-seen",
    ),
    # -- price history -----------------------------------------------------
    _spec(
        "hist_below",
        "--hist-below",
        Kind.PERCENT,
        help=(
            "Keep only items whose current price is at or below the Nth percentile "
            "of their tracked history (requires ≥5 history entries; others pass "
            "through)"
        ),
        scopes=_ALL_CLI,
        context_field="max_hist_percentile",
        label="hist-below",
    ),
    _spec(
        "min_price_drop",
        "--min-price-drop",
        Kind.FLOAT,
        default=0.0,
        minimum=0,
        help=(
            "Keep only items whose price dropped by at least PCT% from their last "
            "tracked price (no history = pass through)"
        ),
        scopes=_ALL_CLI,
        context_field="min_price_drop",
        label="min-price-drop",
    ),
    _spec(
        "require_history",
        "--require-history",
        Kind.BOOL_PAIR,
        default=False,
        help=(
            "With --hist-below/--min-price-drop, drop items lacking enough history "
            "instead of passing them through"
        ),
        scopes=_ALL_CLI,
        context_field="require_history",
        label="require-history",
    ),
    _spec(
        "released_after",
        "--released-after",
        Kind.DATE,
        default="",
        help="Only items released on/after this date (YYYY-MM-DD)",
        scopes=_ALL_CLI,
        context_field="released_after",
        label="released-after",
    ),
    _spec(
        "released_before",
        "--released-before",
        Kind.DATE,
        default="",
        help="Only items released on/before this date (YYYY-MM-DD)",
        scopes=_ALL_CLI,
        context_field="released_before",
        label="released-before",
    ),
    # -- persisted run preference ----------------------------------------
    _spec(
        "interactive",
        "--interactive",
        Kind.BOOL_PAIR,
        aliases=("-i",),
        default=False,
        help="Browse results interactively",
        scopes={S.FIND, S.SEARCH, S.FOR_ME, S.PROFILE, S.CONFIG},
        refine=Refine.NONE,
    ),
)

# ---------------------------------------------------------------------------
# Lookups and derived artifacts
# ---------------------------------------------------------------------------

SPEC_BY_KEY: Mapping[str, FilterSpec] = MappingProxyType(
    {spec.key: spec for spec in FILTERS}
)


def specs_for(scope: Scope) -> tuple[FilterSpec, ...]:
    """The filters available in *scope*, in registry order."""
    return tuple(spec for spec in FILTERS if scope in spec.scopes)


def scope_defaults(scope: Scope) -> dict[str, Any]:
    """Defaults that a resolving scope declares *differently* from Settings."""
    return {
        spec.key: spec.variant(scope).default
        for spec in specs_for(scope)
        if spec.variant(scope).default is not INHERIT
    }


def recipe_specs() -> tuple[FilterSpec, ...]:
    """Filters stored in a result recipe (refinable by ``deals last``)."""
    return tuple(spec for spec in FILTERS if spec.refine is Refine.LOCAL)


def recipe_fields(kinds: set[Kind]) -> frozenset[str]:
    """Recipe field names whose spec kind is in *kinds*."""
    return frozenset(spec.recipe_name for spec in recipe_specs() if spec.kind in kinds)


def validate_date_text(value: str) -> str:
    """Return the canonical ``YYYY-MM-DD`` form of *value* or raise ValueError."""
    import datetime

    return datetime.date.fromisoformat(value).isoformat()


# Config: filter keys come from the registry; non-filter keys are declared by
# config_store (which owns the complete schema).
CONFIG_FILTER_SCHEMA: Mapping[str, type] = MappingProxyType(
    {spec.key: spec.config_type for spec in specs_for(Scope.CONFIG)}
)


def config_bounds() -> dict[str, tuple[float | None, float | None]]:
    """(minimum, maximum) for every numeric config filter."""
    return {
        spec.key: (spec.lower_bound, spec.upper_bound)
        for spec in specs_for(Scope.CONFIG)
        if spec.is_numeric
    }


def config_key_aliases() -> dict[str, str]:
    """Alternative spellings (underscored flag names) of config keys."""
    aliases: dict[str, str] = {}
    for spec in specs_for(Scope.CONFIG):
        spelled = spec.flag_name.replace("-", "_")
        if spelled != spec.key:
            aliases[spelled] = spec.key
    return aliases


def active_filter_labels(values: Any) -> list[str]:
    """``name=value`` for every active filter, in registry order.

    *values* is anything with the registry keys as attributes (a ``Settings``).
    A filter is active when it is set: not ``None`` if "unset" is ``None``,
    otherwise truthy.
    """
    labels: list[str] = []
    for spec in FILTERS:
        if spec.label is None:
            continue
        value = getattr(values, spec.key)
        if (value is not None) if spec.nullable else bool(value):
            if spec.is_bool:
                text = "yes"
            elif spec.is_multi:
                text = ",".join(value)
            else:
                text = str(value)
            labels.append(f"{spec.label}={text}")
    return labels
