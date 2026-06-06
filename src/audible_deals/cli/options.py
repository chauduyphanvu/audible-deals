"""Shared click options and shell-completion callbacks."""

from __future__ import annotations

from pathlib import Path

import click

from audible_deals.config_store import load_profiles
from audible_deals.constants import DEFAULT_LIMIT, GENRE_ALIASES


def _complete_profile_names(ctx, param, incomplete):
    from click.shell_completion import CompletionItem

    try:
        names = load_profiles().keys()
    except Exception:
        return []
    return [CompletionItem(n) for n in names if n.startswith(incomplete)]


def _complete_genre_names(ctx, param, incomplete):
    from click.shell_completion import CompletionItem

    return [CompletionItem(k) for k in GENRE_ALIASES if k.startswith(incomplete)]


def _common_filter_options(func):
    """Apply the shared filter/output click options used by search and find."""
    # Applied in reverse order (click decorators stack bottom-up)
    options = [
        click.option(
            "--max-price-per-hour",
            "max_pph",
            type=click.FloatRange(min=0),
            default=None,
            help="Max price per hour (e.g. 0.50)",
        ),
        click.option(
            "--exclude-genre",
            multiple=True,
            help="Genre(s) to exclude (repeatable, fuzzy match)",
            shell_complete=_complete_genre_names,
        ),
        click.option(
            "--min-rating", type=float, default=0.0, help="Minimum rating (e.g. 4.0)"
        ),
        click.option(
            "--narrator",
            default="",
            help="Filter by narrator name (substring match, client-side)",
        ),
        click.option(
            "--author", default="", help="Filter by author name (substring match)"
        ),
        click.option(
            "--series", default="", help="Filter by series name (substring match)"
        ),
        click.option(
            "--publisher", default="", help="Filter by publisher name (substring match)"
        ),
        click.option(
            "--exclude-author",
            "exclude_authors",
            multiple=True,
            help="Exclude author (substring match, repeatable)",
        ),
        click.option(
            "--exclude-narrator",
            "exclude_narrators",
            multiple=True,
            help="Exclude narrator (substring match, repeatable)",
        ),
        click.option(
            "--on-sale/--no-on-sale", default=False, help="Only show discounted items"
        ),
        click.option(
            "--min-discount",
            type=click.IntRange(min=0, max=100),
            default=0,
            help="Minimum discount percentage (e.g. 70)",
        ),
        click.option(
            "--deep/--no-deep",
            default=False,
            help="Scan with 3 sort orders for better coverage (3x API calls)",
        ),
        click.option("--language", default="", help="Language filter (e.g. english)"),
        click.option(
            "--all-languages/--no-all-languages",
            default=False,
            help="Include all languages (default: locale language only)",
        ),
        click.option(
            "--first-in-series/--no-first-in-series",
            default=False,
            help="Show only the first book per series",
        ),
        click.option(
            "--skip-owned/--no-skip-owned",
            default=False,
            help="Exclude books already in your library",
        ),
        click.option(
            "--exclude-seen",
            is_flag=True,
            default=False,
            help="Exclude ASINs from last search/find results",
        ),
        click.option(
            "--limit",
            "-n",
            type=click.IntRange(min=0),
            default=DEFAULT_LIMIT,
            help="Show only the top N results (0 for unlimited, default: 25)",
        ),
        click.option(
            "--output",
            "-o",
            type=click.Path(path_type=Path),
            default=None,
            help="Export results to file (.json or .csv)",
        ),
        click.option(
            "--json",
            "json_flag",
            is_flag=True,
            default=False,
            help="Output results as JSON to stdout",
        ),
        click.option(
            "--quiet",
            "-q",
            is_flag=True,
            default=False,
            help="Suppress table output (useful with --output)",
        ),
        click.option(
            "--show-url",
            is_flag=True,
            default=False,
            help="Show Audible URL for each item in the table",
        ),
        click.option(
            "--interactive/--no-interactive",
            "-i",
            default=False,
            help="Browse results interactively",
        ),
        click.option(
            "--profile",
            "profile_name",
            default=None,
            help="Load a saved search profile (overrides defaults, CLI flags take precedence)",
            shell_complete=_complete_profile_names,
        ),
        click.option(
            "--dry-run",
            is_flag=True,
            default=False,
            help="Show what would be scanned without making API calls",
        ),
        click.option(
            "--skip-plus/--no-skip-plus",
            default=False,
            help="Exclude Audible Plus catalog titles",
        ),
        click.option(
            "--only-plus/--no-only-plus",
            default=False,
            help="Show only Audible Plus catalog titles",
        ),
        click.option(
            "--exclude-keyword",
            "exclude_keywords",
            multiple=True,
            help="Drop results with title/subtitle matching keyword (repeatable)",
        ),
        click.option(
            "--hist-below",
            "hist_below",
            type=click.IntRange(min=0, max=100),
            default=None,
            help="Keep only items whose current price is at or below the Nth percentile of their tracked history (requires ≥5 history entries; others pass through)",
        ),
        click.option(
            "--min-price-drop",
            "min_price_drop",
            type=click.FloatRange(min=0),
            default=0.0,
            help="Keep only items whose price dropped by at least PCT%% from their last tracked price (no history = pass through)",
        ),
        click.option(
            "--require-history",
            "require_history",
            is_flag=True,
            default=False,
            help="With --hist-below/--min-price-drop, drop items lacking enough history instead of passing them through",
        ),
        click.option(
            "--released-after",
            "released_after",
            default="",
            help="Only items released on/after this date (YYYY-MM-DD)",
        ),
        click.option(
            "--released-before",
            "released_before",
            default="",
            help="Only items released on/before this date (YYYY-MM-DD)",
        ),
    ]
    for option in reversed(options):
        func = option(func)
    return func
