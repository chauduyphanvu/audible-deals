"""Resolved scan settings as a frozen dataclass.

Merges defaults <- config_file <- profile <- CLI flags in a single pass.
"""

from __future__ import annotations

import logging
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field, fields
from typing import Any

from audible_deals.constants import DEFAULT_LIMIT, DEFAULT_SORT
from audible_deals.filter_specs import (
    CONFIG_FILTER_SCHEMA,
    FILTERS,
    Kind,
    Scope,
    specs_for,
    validate_date_text,
)
from audible_deals.validation import validate_finite_number

logger = logging.getLogger(__name__)


def resolve_plus_flags(
    skip_plus: bool,
    only_plus: bool,
    *,
    skip_rank: int = 0,
    only_rank: int = 0,
) -> tuple[bool, bool]:
    if type(skip_plus) is not bool:
        raise ValueError("skip_plus must be boolean")
    if type(only_plus) is not bool:
        raise ValueError("only_plus must be boolean")
    if skip_plus and only_plus:
        if skip_rank > only_rank:
            only_plus = False
        elif only_rank > skip_rank:
            skip_plus = False
        else:
            raise ValueError("--skip-plus and --only-plus are mutually exclusive")
    return skip_plus, only_plus


# Keys covered by config-file defaults (locale is resolved once in the CLI
# group callback, not per-scan, and is not a filter).
_CONFIG_KEYS: tuple[str, ...] = tuple(CONFIG_FILTER_SCHEMA)

# Additional keys that only profiles supply (not in the config schema).
_PROFILE_EXTRA_KEYS: tuple[str, ...] = tuple(
    spec.key
    for spec in specs_for(Scope.PROFILE)
    if spec.key not in CONFIG_FILTER_SCHEMA
)

# How a stored profile value must look, by registry kind.
_PROFILE_SPECS = specs_for(Scope.PROFILE)


@dataclass(frozen=True)
class Settings:
    """Fully-resolved options for a scan command."""

    max_price: float | None = None
    sort: str = DEFAULT_SORT
    pages: int = 10
    min_rating: float = 0.0
    min_ratings: int = 0
    min_hours: float = 0.0
    min_discount: int = 0
    max_pph: float | None = None
    limit: int | None = DEFAULT_LIMIT
    language: str = ""
    narrator: str = ""
    author: str = ""
    series: str = ""
    publisher: str = ""
    on_sale: bool = False
    deep: bool = False
    first_in_series: bool = False
    all_languages: bool = False
    skip_owned: bool = False
    interactive: bool = False
    genre: str = ""
    exclude_genre: tuple[str, ...] = ()
    exclude_authors: tuple[str, ...] = ()
    exclude_narrators: tuple[str, ...] = ()
    keywords: str = ""
    skip_plus: bool = False
    only_plus: bool = False
    exclude_keywords: tuple[str, ...] = ()
    max_effective_price: float | None = None
    exclude_seen: bool = False
    hist_below: int | None = None
    min_price_drop: float = 0.0
    require_history: bool = False
    released_after: str = ""
    released_before: str = ""

    def __post_init__(self) -> None:
        resolve_plus_flags(self.skip_plus, self.only_plus)
        for spec in FILTERS:
            value = getattr(self, spec.key)
            if spec.is_numeric:
                if value is not None:
                    validate_finite_number(
                        spec.key,
                        value,
                        spec.lower_bound,
                        spec.upper_bound,
                        integer=spec.integer,
                    )
            elif spec.kind is Kind.DATE and value:
                try:
                    canonical = validate_date_text(value)
                except (TypeError, ValueError):
                    canonical = None
                if canonical != value:
                    raise ValueError(
                        f"Invalid stored setting '{spec.key}': expected a "
                        "YYYY-MM-DD date"
                    )


@dataclass(frozen=True)
class SettingsResolutionRequest:
    config: dict[str, Any]
    profile: dict[str, Any] | None
    cli_flags: dict[str, Any]
    # Any set; __post_init__ stores it as a frozenset.
    explicit_options: AbstractSet[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "explicit_options", frozenset(self.explicit_options))


def filter_combination_error(settings: Settings) -> str | None:
    """Cross-field problems of a fully resolved Settings, or ``None``.

    Individual values are validated by ``Settings`` itself; these rules only
    make sense once CLI flags, profile and config have been merged.
    """
    if (
        settings.require_history
        and settings.hist_below is None
        and not settings.min_price_drop
    ):
        return "--require-history requires --hist-below or --min-price-drop"
    if (
        settings.released_after
        and settings.released_before
        and settings.released_after > settings.released_before
    ):
        return "--released-after cannot be later than --released-before"
    return None


def profile_validation_error(profile: object) -> str | None:
    if not isinstance(profile, dict):
        return "expected an object"
    for spec in _PROFILE_SPECS:
        if (
            spec.kind in (Kind.TEXT, Kind.DATE)
            and spec.key in profile
            and not isinstance(profile[spec.key], str)
        ):
            return f"{spec.key} must be text"
    for spec in _PROFILE_SPECS:
        if spec.is_bool and spec.key in profile and type(profile[spec.key]) is not bool:
            return f"{spec.key} must be boolean"
    for spec in _PROFILE_SPECS:
        if spec.valid is not None and profile.get(spec.key):
            if profile[spec.key] not in spec.valid:
                return f"{spec.key} must be one of {', '.join(sorted(spec.valid))}"
    for spec in _PROFILE_SPECS:
        if spec.key not in profile or not spec.is_multi:
            continue
        value = profile[spec.key]
        if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, str) for item in value
        ):
            return f"{spec.key} must be a list of text values"
    known = {item.name for item in fields(Settings)}
    try:
        Settings(**{key: value for key, value in profile.items() if key in known})
    except (TypeError, ValueError) as exc:
        return str(exc)
    return None


def resolve_settings(request: SettingsResolutionRequest) -> Settings:
    """Resolve settings with CLI > profile > config > defaults precedence."""
    resolve_plus_flags(
        request.config.get("skip_plus", False),
        request.config.get("only_plus", False),
    )
    if request.profile:
        resolve_plus_flags(
            request.profile.get("skip_plus", False),
            request.profile.get("only_plus", False),
        )
    resolve_plus_flags(
        request.cli_flags.get("skip_plus", False)
        if "skip_plus" in request.explicit_options
        else False,
        request.cli_flags.get("only_plus", False)
        if "only_plus" in request.explicit_options
        else False,
    )
    merged: dict[str, Any] = dict(request.cli_flags)
    debug = logger.isEnabledFor(logging.DEBUG)
    source: dict[str, str] = {}
    if debug:
        for key in request.cli_flags:
            if key in request.explicit_options:
                source[key] = "cli"

    for key in _CONFIG_KEYS:
        if request.config.get(key) is not None and key not in request.explicit_options:
            merged[key] = request.config[key]
            if debug:
                source[key] = "config"

    if request.profile:
        for key in _CONFIG_KEYS + _PROFILE_EXTRA_KEYS:
            if (
                request.profile.get(key) is not None
                and key not in request.explicit_options
            ):
                merged[key] = request.profile[key]
                if debug:
                    source[key] = "profile"

    known = {item.name for item in fields(Settings)}
    kwargs = {key: value for key, value in merged.items() if key in known}
    language_rank = _setting_source_rank(request, "language")
    all_languages_rank = _setting_source_rank(request, "all_languages")
    if kwargs.get("language") and kwargs.get("all_languages"):
        if language_rank > all_languages_rank:
            kwargs["all_languages"] = False
        elif all_languages_rank > language_rank:
            kwargs["language"] = ""
        else:
            raise ValueError("language and all_languages cannot both be enabled")
    kwargs["skip_plus"], kwargs["only_plus"] = resolve_plus_flags(
        kwargs.get("skip_plus", False),
        kwargs.get("only_plus", False),
        skip_rank=_setting_source_rank(request, "skip_plus"),
        only_rank=_setting_source_rank(request, "only_plus"),
    )
    if debug:
        logger.debug("Settings sources: %s", source)
    return Settings(**kwargs)


def _setting_source_rank(request: SettingsResolutionRequest, key: str) -> int:
    if key in request.explicit_options:
        return 3
    if request.profile and request.profile.get(key) is not None:
        return 2
    if request.config.get(key) is not None:
        return 1
    return 0
