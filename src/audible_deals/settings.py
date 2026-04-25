"""Resolved scan settings as a frozen dataclass.

Merges defaults <- config_file <- profile <- CLI flags in a single pass.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

import click

from audible_deals.constants import DEFAULT_LIMIT, DEFAULT_SORT


_CL = click.core.ParameterSource.COMMANDLINE

# Keys covered by config-file defaults
_CONFIG_KEYS: tuple[str, ...] = (
    "max_price",
    "sort",
    "pages",
    "min_rating",
    "min_ratings",
    "min_hours",
    "min_discount",
    "max_pph",
    "limit",
    "language",
    "narrator",
    "author",
    "series",
    "publisher",
    "on_sale",
    "deep",
    "first_in_series",
    "all_languages",
    "skip_owned",
    "interactive",
)

# Additional keys that only profiles supply (not in config schema)
_PROFILE_EXTRA_KEYS: tuple[str, ...] = (
    "genre",
    "exclude_genre",
    "exclude_authors",
    "exclude_narrators",
    "keywords",
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

    @classmethod
    def resolve(
        cls,
        ctx: click.Context,
        *,
        config: dict[str, Any],
        profile: dict[str, Any] | None,
        cli_flags: dict[str, Any],
    ) -> "Settings":
        """Return a Settings built from merged cli_flags <- config <- profile <- cli_flags.

        Precedence (highest wins): CLI > profile > config > dataclass defaults.
        Only overrides when the source is not COMMANDLINE.
        """
        merged: dict[str, Any] = dict(cli_flags)

        for key in _CONFIG_KEYS:
            if config.get(key) is not None and ctx.get_parameter_source(key) != _CL:
                merged[key] = config[key]

        if profile:
            for key in _CONFIG_KEYS + _PROFILE_EXTRA_KEYS:
                if profile.get(key) is not None and ctx.get_parameter_source(key) != _CL:
                    merged[key] = profile[key]

        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in merged.items() if k in known}
        return cls(**kwargs)
