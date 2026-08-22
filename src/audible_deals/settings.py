"""Resolved scan settings as a frozen dataclass.

Merges defaults <- config_file <- profile <- CLI flags in a single pass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from typing import Any

from audible_deals.constants import _CONFIG_SCHEMA, DEFAULT_LIMIT, DEFAULT_SORT
from audible_deals.validation import validate_finite_number

logger = logging.getLogger(__name__)


# Keys covered by config-file defaults. locale is resolved once in the CLI
# group callback, not per-scan.
_CONFIG_KEYS: tuple[str, ...] = tuple(k for k in _CONFIG_SCHEMA if k != "locale")

# Additional keys that only profiles supply (not in config schema)
_PROFILE_EXTRA_KEYS: tuple[str, ...] = (
    "genre",
    "exclude_genre",
    "exclude_authors",
    "exclude_narrators",
    "keywords",
    "exclude_keywords",
)


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

    def __post_init__(self) -> None:
        for name, minimum, maximum, integer in (
            ("max_price", 0, None, False),
            ("min_rating", 0, 5, False),
            ("min_ratings", 0, None, True),
            ("min_hours", 0, None, False),
            ("min_discount", 0, 100, True),
            ("max_pph", 0, None, False),
            ("pages", 1, None, True),
            ("limit", 0, None, True),
        ):
            value = getattr(self, name)
            if value is not None:
                validate_finite_number(name, value, minimum, maximum, integer=integer)


@dataclass(frozen=True)
class SettingsResolutionRequest:
    config: dict[str, Any]
    profile: dict[str, Any] | None
    cli_flags: dict[str, Any]
    explicit_options: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "explicit_options", frozenset(self.explicit_options))


def resolve_settings(request: SettingsResolutionRequest) -> Settings:
    """Resolve settings with CLI > profile > config > defaults precedence."""
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
