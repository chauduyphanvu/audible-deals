"""CLI for finding Audible audiobook deals.

Usage:
    deals login                    Authenticate with Audible
    deals import-auth PATH         Import auth from audible-cli or Libation
    deals categories [--parent ID] List categories
    deals search QUERY [options]   Search catalog with filters
    deals find [options]           Browse & filter deals (main command)
    deals detail ASIN              Show detailed product info
    deals open ASIN                Open Audible page in browser
    deals compare ASIN ASIN ...    Side-by-side comparison
    deals wishlist add/remove/list/sync Manage your watchlist
    deals watch                    Check wishlist for price drops
    deals notify [--webhook URL]   Send notifications for deals at target
    deals profile save/list/delete Manage saved search profiles
    deals history ASIN             View price history with sparkline
    deals recap [--days N]         Recap of recent price changes
    deals completions SHELL        Generate shell completions
"""

from __future__ import annotations

import dataclasses
import datetime
import json as json_mod
import logging
import math
import os

try:
    import readline  # noqa: F401 — required on macOS for input() with long strings
except ImportError:
    pass  # unavailable on Windows
import sys
import time
from pathlib import Path

import click
from rich.table import Table

from audible_deals.constants import (
    _CONFIG_SCHEMA,
    AUTH_FILE,
    CLIENT_SORT_OPTIONS,
    CONFIG_DIR,
    CONFIG_FILE,
    DEEP_SORT_ORDERS,
    DEFAULT_LIMIT,
    GENRE_ALIASES,
    HISTORY_DIR,
    LOCALE_CURRENCY,
    LOCALE_LANGUAGES,
    MAX_PAGE_SIZE,
    SORT_OPTIONS,
    product_url,
)

# Re-exported so tests can reference these paths via the cli module namespace.
from audible_deals.constants import LAST_RESULTS_FILE, SEEN_ASINS_FILE  # noqa: F401
from audible_deals.client import DealsClient, Product
from audible_deals.logging_setup import configure_logging
from audible_deals.display import (
    console,
    create_scan_progress,
    display_categories,
    display_comparison,
    display_library_stats,
    display_price_history,
    display_product_detail,
    display_products,
    display_recap,
    display_summary,
    display_watch_table,
)
from audible_deals.filtering import (
    dedupe_editions,
    filter_products,
    first_in_series,
    sort_local,
)
from audible_deals.settings import Settings
from audible_deals.utils import (
    format_recap_payload,
    format_webhook_payload,
    looks_like_person_name,
    parse_interval,
    validate_asin,
    validate_webhook_url,
)
from audible_deals.serialization import (
    deserialize_product,
    export_products,
    serialize_product,
)
from audible_deals.state import (
    clear_last_results,
    clear_seen_asins,
    coerce_config_value,
    find_wishlist_atl_hits,
    find_wishlist_hits,
    has_price_history,
    load_config,
    load_last_results,
    load_notify_state,
    load_price_history,
    load_profiles,
    load_seen_asins,
    load_wishlist,
    merge_seen_asins,
    price_history_context,
    record_prices,
    resolve_last_references,
    save_config,
    save_last_results,
    save_notify_state,
    save_profiles,
    save_seen_asins,
    save_wishlist,
    scan_price_changes,
    validate_config_key,
    wishlist_entry,
)


logger = logging.getLogger(__name__)


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


def _collect_asins(asins: tuple[str, ...], last_refs: tuple[str, ...]) -> list[str]:
    """Combine positional ASINs with resolved --last references."""
    all_asins = list(asins)
    if last_refs:
        for ref_asin, desc in resolve_last_references(last_refs):
            console.print(f"[dim]{desc}[/dim]")
            all_asins.append(ref_asin)
    return all_asins


def _resolve_single_last_ref(last_ref: str) -> tuple[str, str]:
    resolved = resolve_last_references((last_ref,))
    if len(resolved) != 1:
        raise click.ClickException(
            f"--last {last_ref!r} expanded to {len(resolved)} results; this command accepts a single position."
        )
    return resolved[0]


def _get_client(locale: str) -> DealsClient:
    return DealsClient(locale=locale)


def _currency(ctx: click.Context) -> str:
    return LOCALE_CURRENCY.get(ctx.obj["locale"], "$")


def _resolve_skip_asins(
    dc: DealsClient, skip_owned: bool, exclude_seen: bool
) -> set[str] | None:
    """Build the set of ASINs to exclude from owned library and seen history."""
    skip_asins = dc.get_library_asins() if skip_owned else None
    return merge_seen_asins(skip_asins, exclude_seen)


def _safe_record_prices(products: list[Product]) -> None:
    """Record prices, warning on failure instead of crashing."""
    try:
        record_prices(products)
    except Exception as e:
        logger.exception("record_prices failed for %d products", len(products))
        console.print(f"[dim]Warning: could not record price history: {e}[/dim]")


_CL = click.core.ParameterSource.COMMANDLINE


def _load_profile(profile_name: str | None) -> dict | None:
    if not profile_name:
        return None
    profiles = load_profiles()
    if profile_name not in profiles:
        raise click.ClickException(
            f"Profile '{profile_name}' not found. "
            "Use 'deals profile list' to see available profiles."
        )
    return profiles[profile_name]


def _resolve_categories(
    dc: DealsClient,
    genre: str,
    category: str,
    exclude_genre: tuple[str, ...],
) -> tuple[str, str, set[str]]:
    """Resolve genre/category names to IDs.

    Returns (category_id, category_name, exclude_category_ids).
    """
    category_name = ""
    exclude_category_ids: set[str] = set()
    if genre:
        try:
            category, category_name = dc.resolve_genre(genre)
        except ValueError as e:
            raise click.ClickException(str(e))
    elif category:
        try:
            category_name = dc.get_category_name(category)
        except ValueError as e:
            raise click.ClickException(str(e))
    for eg in exclude_genre:
        try:
            eid, _ = dc.resolve_genre(eg)
            exclude_category_ids.add(eid)
        except ValueError as e:
            raise click.ClickException(str(e))
    return category, category_name, exclude_category_ids


def _apply_filters(
    all_products: list[Product],
    *,
    max_price: float | None,
    min_rating: float,
    min_ratings: int = 0,
    min_hours: float,
    narrator: str = "",
    language: str,
    author: str = "",
    exclude_authors: tuple[str, ...] = (),
    exclude_narrators: tuple[str, ...] = (),
    on_sale: bool,
    skip_asins: set[str] | None,
    exclude_category_ids: set[str],
    first_in_series_only: bool,
    sort: str,
    max_pph: float | None = None,
    min_discount: int = 0,
    series: str = "",
    publisher: str = "",
    skip_plus: bool = False,
    only_plus: bool = False,
    exclude_keywords: tuple[str, ...] = (),
    drop_zero_length: bool = True,
) -> tuple[list[Product], dict[str, int], int, int]:
    """Filter, deduplicate, and sort products. Returns (filtered, breakdown, editions_removed, series_collapsed)."""
    no_runtime_count = sum(1 for p in all_products if p.length_minutes == 0)
    if drop_zero_length and no_runtime_count:
        all_products = [p for p in all_products if p.length_minutes != 0]
    filtered, filter_breakdown = filter_products(
        all_products,
        max_price=max_price,
        min_rating=min_rating,
        min_ratings=min_ratings,
        min_hours=min_hours,
        narrator=narrator,
        language=language,
        author=author,
        exclude_authors=exclude_authors,
        exclude_narrators=exclude_narrators,
        on_sale=on_sale,
        skip_asins=skip_asins,
        exclude_category_ids=exclude_category_ids,
        max_pph=max_pph,
        min_discount=min_discount,
        series=series,
        publisher=publisher,
        skip_plus=skip_plus,
        only_plus=only_plus,
        exclude_keywords=exclude_keywords,
    )
    if drop_zero_length and no_runtime_count:
        filter_breakdown["no runtime"] = no_runtime_count
    filtered, editions_removed = dedupe_editions(filtered)
    series_collapsed = 0
    if first_in_series_only:
        filtered, series_collapsed = first_in_series(filtered)
    filtered = sort_local(filtered, sort)
    return filtered, filter_breakdown, editions_removed, series_collapsed


def _record_and_cache(
    filtered: list[Product],
    *,
    title: str,
    write_cache: bool = True,
    limit: int | None,
) -> tuple[list[Product], list[dict], int]:
    """Record prices, persist cache, apply limit. Returns (filtered_limited, serialized, total_before_limit)."""
    _safe_record_prices(filtered)
    serialized_all = [serialize_product(p) for p in filtered]
    if write_cache:
        try:
            save_last_results(title, serialized_all)
        except Exception:
            pass
    total_before_limit = len(filtered)
    if limit is not None and limit > 0:
        filtered = filtered[:limit]
        serialized = serialized_all[:limit]
    else:
        serialized = serialized_all
    if write_cache:
        save_seen_asins({p.asin for p in filtered})
    return filtered, serialized, total_before_limit


def _emit_output(
    filtered: list[Product],
    serialized: list[dict],
    *,
    title: str,
    output: Path | None,
    json_flag: bool,
    quiet: bool,
    max_price: float | None,
    filter_breakdown: dict[str, int],
    editions_removed: int,
    series_collapsed: int,
    total_before_limit: int,
    currency: str = "$",
    interactive: bool = False,
    show_url: bool = False,
) -> None:
    """Write results to file, JSON stdout, or the terminal table."""
    if output:
        export_products(filtered, output)
        console.print(f"[green]Exported {len(filtered)} items to {output}[/green]")
    if json_flag:
        click.echo(json_mod.dumps(serialized, indent=2, ensure_ascii=False))
    if not json_flag and not quiet:
        atl_asins, hist_context = price_history_context(filtered)
        console.print()
        display_products(
            filtered,
            max_price=max_price,
            title=title,
            currency=currency,
            show_url=show_url,
            atl_asins=atl_asins,
            hist_context=hist_context,
        )
        display_summary(
            len(filtered),
            filter_breakdown,
            max_price=max_price,
            editions_removed=editions_removed,
            series_collapsed=series_collapsed,
            currency=currency,
            total_before_limit=total_before_limit,
        )
    if interactive and filtered and not json_flag:
        _interactive_browse(filtered, currency=currency)


def _interactive_browse(products: list[Product], currency: str = "$") -> None:
    """Interactive mode: let user pick items to view details, open, or wishlist."""
    console.print(
        "\n  [dim]Enter a # for details, 'o #' open, 'w #' wishlist, "
        "'c # #' compare, 'h #' history, 'q' quit.[/dim]"
    )
    while True:
        try:
            choice = click.prompt("\n>", default="q", show_default=False).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if choice.lower() == "q":
            break

        parts = choice.split()
        action = "detail"
        idx = -1
        idx2 = -1
        try:
            if len(parts) >= 1 and parts[0].lower() == "c":
                if len(parts) != 3:
                    console.print(
                        "[dim]Invalid input. Enter a number, 'o #', 'w #', 'c # #', 'h #', or 'q'.[/dim]"
                    )
                    continue
                action = "compare"
                idx = int(parts[1]) - 1
                idx2 = int(parts[2]) - 1
            elif len(parts) == 2 and parts[0].lower() in ("o", "w", "h"):
                verb = parts[0].lower()
                if verb == "o":
                    action = "open"
                elif verb == "w":
                    action = "wishlist"
                else:
                    action = "history"
                idx = int(parts[1]) - 1
            else:
                idx = int(parts[0]) - 1
        except (ValueError, IndexError):
            console.print(
                "[dim]Invalid input. Enter a number, 'o #', 'w #', 'c # #', 'h #', or 'q'.[/dim]"
            )
            continue

        if action == "compare":
            if idx < 0 or idx >= len(products) or idx2 < 0 or idx2 >= len(products):
                console.print(f"[dim]Number must be 1-{len(products)}.[/dim]")
                continue
            display_comparison([products[idx], products[idx2]])
            continue

        if idx < 0 or idx >= len(products):
            console.print(f"[dim]Number must be 1-{len(products)}.[/dim]")
            continue

        p = products[idx]
        if action == "detail":
            display_product_detail(p)
        elif action == "open":
            console.print(f"[dim]Opening {p.url}[/dim]")
            click.launch(p.url)
        elif action == "history":
            entries = load_price_history(p.asin)
            if not entries:
                console.print(f"[dim]No price history for {p.asin}[/dim]")
            else:
                display_price_history(entries, p.asin, currency)
        elif action == "wishlist":
            items = load_wishlist()
            if any(item["asin"] == p.asin for item in items):
                console.print(f"[dim]{p.asin} already on wishlist[/dim]")
            else:
                target_price = None
                try:
                    raw = click.prompt(
                        "  Target price (or Enter to skip)",
                        default="",
                        show_default=False,
                    ).strip()
                    if raw:
                        target_price = float(raw)
                except (ValueError, EOFError):
                    pass
                items.append(wishlist_entry(p, target_price))
                save_wishlist(items)
                target_note = (
                    f" (target: {p.currency}{target_price:.2f})"
                    if target_price is not None
                    else ""
                )
                console.print(
                    f"[green]+[/green] {p.title} added to wishlist{target_note}"
                )


class _HandleAuthErrors(click.Group):
    """Catch RuntimeError from missing auth and show a friendly message."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except RuntimeError as e:
            if "Not authenticated" in str(e):
                raise click.ClickException(str(e))
            raise


def _print_version(ctx: click.Context, param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    try:
        from importlib.metadata import version as _pkg_version

        v = _pkg_version("audible-deals")
    except Exception:
        v = "0.7.0"  # fallback for PyInstaller frozen builds
    click.echo(f"deals, version {v}")
    ctx.exit()


@click.group(cls=_HandleAuthErrors, invoke_without_command=True)
@click.option(
    "--version",
    is_flag=True,
    expose_value=False,
    is_eager=True,
    callback=_print_version,
    help="Show the version and exit.",
)
@click.option(
    "--locale",
    default="us",
    help="Audible marketplace (us, uk, ca, de, fr, au, jp, in, es)",
)
@click.option(
    "-v",
    "--verbose",
    "verbose",
    count=True,
    help="Enable debug logging (-v for INFO, -vv for DEBUG). DEALS_DEBUG=1 also enables it.",
)
@click.pass_context
def cli(ctx, locale, verbose):
    """Audible deal finder - find cheap audiobooks during sales."""
    configure_logging(verbose)
    ctx.ensure_object(dict)
    cfg = load_config()
    ctx.obj["config"] = cfg
    if ctx.get_parameter_source("locale") != _CL:
        cfg_locale = cfg.get("locale")
        if cfg_locale:
            locale = cfg_locale
    ctx.obj["locale"] = locale
    logger.debug("cli start locale=%s subcommand=%s", locale, ctx.invoked_subcommand)
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        console.print(
            "\n  [dim]Quick start: deals find --genre sci-fi --max-price 5[/dim]"
        )


@cli.command()
@click.option(
    "--external", is_flag=True, help="Login via external browser (for captcha/2FA)"
)
@click.option(
    "--via-file",
    type=click.Path(path_type=Path),
    default=None,
    help="File path for the callback URL (you save the URL there after login, then press Enter)",
)
@click.pass_context
def login(ctx, external, via_file):
    """Authenticate with Audible.

    \b
    Recommended flow for macOS:
        deals login --external --via-file /tmp/url.txt
    This prints the sign-in URL, waits for you to log in and save the
    callback URL to the file, then press Enter to finish auth.
    """
    dc = _get_client(ctx.obj["locale"])

    if external:
        dc.login_external(callback_url_file=via_file)
    else:
        username = click.prompt("Audible email")
        password = click.prompt("Audible password", hide_input=True)
        dc.login(username, password)

    console.print(f"[green]Authenticated.[/green] Auth saved to {dc.auth_file}")


@cli.command("import-auth")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def import_auth(ctx, path: Path):
    """Import auth from an audible-cli JSON file or Libation AccountsSettings.json."""
    dc = _get_client(ctx.obj["locale"])
    dc.import_auth(path)
    console.print(f"[green]Auth imported.[/green] Saved to {dc.auth_file}")


@cli.command()
@click.option("--parent", default="", help="Parent category ID (omit for top-level)")
@click.pass_context
def categories(ctx, parent):
    """List Audible categories. Use --parent to drill into subcategories."""
    dc = _get_client(ctx.obj["locale"])
    with dc:
        try:
            cats = dc.get_categories(root=parent)
        except ValueError as e:
            raise click.ClickException(str(e))

    title = "Subcategories" if parent else "Top-Level Categories"
    display_categories(cats, title=title)
    console.print(
        "\n  [dim]Tip: use --parent ID to see subcategories, "
        "or pass the ID to 'deals find --category ID'[/dim]"
    )


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
    ]
    for option in reversed(options):
        func = option(func)
    return func


def _resolve_output_quiet(ctx: click.Context, output, json_flag, quiet) -> bool:
    """Output file implies quiet (unless -q was given explicitly); JSON output moves console chatter to stderr."""
    if output and ctx.get_parameter_source("quiet") != _CL:
        quiet = True
    if json_flag:
        console.file = sys.stderr
    return quiet


def _build_scan_settings(
    ctx: click.Context,
    profile_name: str | None,
    **kwargs,
) -> Settings:
    """Resolve command kwargs + config/profile defaults into a Settings."""
    s = Settings.resolve(
        ctx,
        config=ctx.obj.get("config", {}),
        profile=_load_profile(profile_name),
        cli_flags=dict(kwargs),
    )
    if not s.language and not s.all_languages:
        s = dataclasses.replace(s, language=LOCALE_LANGUAGES.get(ctx.obj["locale"], ""))
    if logger.isEnabledFor(logging.DEBUG):
        debug_keys = (
            "genre",
            "keywords",
            "max_price",
            "max_pph",
            "sort",
            "pages",
            "limit",
            "min_rating",
            "min_ratings",
            "min_hours",
            "min_discount",
            "language",
            "on_sale",
            "deep",
            "first_in_series",
            "skip_owned",
        )
        snapshot = {k: getattr(s, k) for k in debug_keys}
        logger.debug("resolved scan settings: %s", snapshot)
    return s


def _apply_settings_filters(
    products: list[Product],
    s: Settings,
    *,
    skip_asins: set[str] | None,
    exclude_category_ids: set[str],
) -> tuple[list[Product], dict[str, int], int, int]:
    """Run _apply_filters with all filter options taken from a resolved Settings."""
    return _apply_filters(
        products,
        max_price=s.max_price,
        min_rating=s.min_rating,
        min_ratings=s.min_ratings,
        min_hours=s.min_hours,
        narrator=s.narrator,
        author=s.author,
        exclude_authors=s.exclude_authors,
        exclude_narrators=s.exclude_narrators,
        language=s.language,
        on_sale=s.on_sale,
        skip_asins=skip_asins,
        exclude_category_ids=exclude_category_ids,
        first_in_series_only=s.first_in_series,
        sort=s.sort,
        max_pph=s.max_pph,
        min_discount=s.min_discount,
        series=s.series,
        publisher=s.publisher,
        skip_plus=s.skip_plus,
        only_plus=s.only_plus,
        exclude_keywords=s.exclude_keywords,
    )


@cli.command()
@click.argument("query", required=False, default="")
@click.option(
    "--max-price",
    type=click.FloatRange(min=0),
    default=None,
    help="Max price filter (e.g. 5.00)",
)
@click.option("--category", default="", help="Category ID to search within")
@click.option(
    "--genre",
    default="",
    help="Genre name to search within (fuzzy match, e.g. 'sci-fi')",
    shell_complete=_complete_genre_names,
)
@click.option(
    "--sort",
    type=click.Choice(list(SORT_OPTIONS.keys()) + sorted(CLIENT_SORT_OPTIONS)),
    default="relevance",
    help="Sort order (price/discount/price-per-hour/value are client-side)",
)
@click.option(
    "--min-ratings", type=int, default=0, help="Minimum number of ratings (e.g. 100)"
)
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours")
@click.option(
    "--pages",
    type=click.IntRange(min=1),
    default=3,
    help="Number of pages to scan (50 items/page)",
)
@_common_filter_options
@click.pass_context
def search(
    ctx,
    query,
    max_price,
    max_pph,
    category,
    genre,
    exclude_genre,
    sort,
    min_rating,
    min_ratings,
    min_hours,
    narrator,
    author,
    series,
    publisher,
    exclude_authors,
    exclude_narrators,
    on_sale,
    min_discount,
    deep,
    pages,
    language,
    all_languages,
    first_in_series,
    skip_owned,
    exclude_seen,
    limit,
    output,
    json_flag,
    quiet,
    show_url,
    interactive,
    profile_name,
    dry_run,
    skip_plus,
    only_plus,
    exclude_keywords,
):
    """Search the Audible catalog by keyword."""
    logger.info(
        "search query=%r genre=%r category=%r max_price=%s pages=%s sort=%s deep=%s",
        query,
        genre,
        category,
        max_price,
        pages,
        sort,
        deep,
    )
    if not query and not genre and not category:
        raise click.UsageError("Provide a QUERY or use --genre / --category to browse.")
    if skip_plus and only_plus:
        raise click.UsageError("--skip-plus and --only-plus are mutually exclusive")
    s = _build_scan_settings(
        ctx,
        profile_name,
        max_price=max_price,
        max_pph=max_pph,
        sort=sort,
        min_rating=min_rating,
        min_ratings=min_ratings,
        min_hours=min_hours,
        min_discount=min_discount,
        language=language,
        narrator=narrator,
        author=author,
        pages=pages,
        limit=limit,
        on_sale=on_sale,
        deep=deep,
        first_in_series=first_in_series,
        all_languages=all_languages,
        skip_owned=skip_owned,
        interactive=interactive,
        genre=genre,
        exclude_genre=exclude_genre,
        exclude_authors=exclude_authors,
        exclude_narrators=exclude_narrators,
        keywords="",
        series=series,
        publisher=publisher,
        skip_plus=skip_plus,
        only_plus=only_plus,
        exclude_keywords=exclude_keywords,
    )
    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)
    if s.genre and category:
        raise click.UsageError("Use --genre or --category, not both.")

    dc = _get_client(ctx.obj["locale"])
    server_sort = SORT_OPTIONS.get(s.sort, "Relevance")
    sort_orders = DEEP_SORT_ORDERS if s.deep else [server_sort]

    with dc:
        category, category_name, exclude_category_ids = _resolve_categories(
            dc, s.genre, category, s.exclude_genre
        )

        if dry_run:
            _print_dry_run_summary(
                category_name=category_name,
                query=query,
                sort_orders=sort_orders,
                pages=s.pages,
            )
            return

        skip_asins = _resolve_skip_asins(dc, s.skip_owned, exclude_seen)

        queries = (
            [q.strip() for q in query.split("|") if q.strip()]
            if "|" in query
            else [query]
        )
        if not queries:
            raise click.UsageError("No keywords found after splitting on '|'.")

        if len(queries) > 1:
            all_products: list[Product] = []
            fetched_asins: set[str] = set()
            for q in queries:
                sub_products = _fetch_with_progress(
                    dc,
                    keywords=q,
                    category_ids=[category],
                    sort_orders=sort_orders,
                    pages=s.pages,
                    description=f"Searching '{q}'",
                )
                for p in sub_products:
                    if p.asin not in fetched_asins:
                        fetched_asins.add(p.asin)
                        all_products.append(p)
            scope = " | ".join(f"'{q}'" for q in queries)
            if category_name:
                scope += f" in {category_name}"
        else:
            if queries[0]:
                scope = f"'{queries[0]}'"
                if category_name:
                    scope += f" in {category_name}"
            elif category_name:
                scope = category_name
            else:
                scope = "catalog"

            all_products = _fetch_with_progress(
                dc,
                keywords=queries[0],
                category_ids=[category],
                sort_orders=sort_orders,
                pages=s.pages,
                description=f"Searching {scope}",
            )

    cur = _currency(ctx)
    if len(queries) > 1:
        combined_query = " | ".join(queries)
        search_title = f'Search: "{combined_query}"'
        if category_name:
            search_title += f" in {category_name}"
    elif queries[0]:
        search_title = f'Search: "{queries[0]}"'
        if category_name:
            search_title += f" in {category_name}"
    else:
        search_title = f"Search: {category_name or 'All'}"
    filtered, filter_breakdown, editions_removed, series_collapsed = (
        _apply_settings_filters(
            all_products,
            s,
            skip_asins=skip_asins,
            exclude_category_ids=exclude_category_ids,
        )
    )
    filtered, serialized, total_before_limit = _record_and_cache(
        filtered,
        title=search_title,
        limit=s.limit,
    )
    _emit_output(
        filtered,
        serialized,
        title=search_title,
        output=output,
        json_flag=json_flag,
        quiet=quiet,
        max_price=s.max_price,
        filter_breakdown=filter_breakdown,
        editions_removed=editions_removed,
        series_collapsed=series_collapsed,
        total_before_limit=total_before_limit,
        currency=cur,
        interactive=s.interactive,
        show_url=show_url,
    )
    display_query = queries[0] if len(queries) == 1 else None
    if (
        display_query
        and not s.author
        and not json_flag
        and not quiet
        and looks_like_person_name(display_query)
    ):
        console.print(
            f"\n  [dim]Tip: Use --author '{display_query}' for exact author filtering.[/dim]"
        )


def _print_dry_run_summary(
    *,
    category_name: str,
    query: str,
    sort_orders: list[str],
    pages: int,
    subcategory_count: int | None = None,
) -> None:
    """Print a dry-run scan summary."""
    sort_label = ", ".join(sort_orders)
    multiplier = subcategory_count if subcategory_count is not None else 1
    console.print("\n[bold]Dry run[/bold] — would scan:")
    if category_name:
        console.print(f"  Category: {category_name}")
    if subcategory_count is not None:
        console.print(f"  Subcategories: {subcategory_count}")
    if query:
        console.print(f"  Query: {query}")
    console.print(f"  Sort orders: {sort_label}")
    console.print(f"  Pages per sort: {pages}")
    console.print(
        f"  Max items: ~{pages * len(sort_orders) * MAX_PAGE_SIZE * multiplier}"
    )
    console.print(f"  API calls: {pages * len(sort_orders) * multiplier}")


def _fetch_with_progress(
    dc: DealsClient,
    *,
    keywords: str,
    category_ids: list[str],
    sort_orders: list[str],
    pages: int,
    description: str,
) -> list[Product]:
    """Fetch products across one or more category ids and sort orders with a progress bar.

    Deduplicates by ASIN across all segments. Returns a flat list.
    """
    all_products: list[Product] = []
    seen_asins: set[str] = set()
    total_segments = len(category_ids) * len(sort_orders)
    total_pages = pages * total_segments

    with create_scan_progress() as progress:
        task = progress.add_task(description, total=total_pages, items=0)
        pages_done = 0
        segments_done = 0

        for category_id in category_ids:
            for sort_idx, sort_order in enumerate(sort_orders):
                for products, page_num, total in dc.search_pages(
                    keywords=keywords,
                    category_id=category_id,
                    sort_by=sort_order,
                    max_pages=pages,
                ):
                    new_products = [p for p in products if p.asin not in seen_asins]
                    seen_asins.update(p.asin for p in new_products)
                    all_products.extend(new_products)
                    pages_done += 1

                    if page_num == 1:
                        actual = min(pages, math.ceil(total / 50)) if total else 1
                        segments_remaining = total_segments - segments_done - 1
                        total_pages = (
                            (pages_done - 1) + actual + segments_remaining * pages
                        )
                        progress.update(task, total=total_pages)

                    progress.update(task, completed=pages_done, items=len(all_products))

                segments_done += 1

    return all_products


@cli.command()
@click.option(
    "--category", default="", help="Category ID (use 'deals categories' to find IDs)"
)
@click.option(
    "--genre",
    default="",
    help="Genre name (fuzzy match, e.g. 'sci-fi', 'mystery', 'romance')",
    shell_complete=_complete_genre_names,
)
@click.option(
    "--keywords", default="", help="Optional keyword filter within the category"
)
@click.option(
    "--max-price",
    type=click.FloatRange(min=0),
    default=5.00,
    help="Max price threshold (default: $5.00)",
)
@click.option(
    "--sort",
    type=click.Choice(sorted(CLIENT_SORT_OPTIONS) + list(SORT_OPTIONS.keys())),
    default="price-per-hour",
    help="Sort order (price/discount/price-per-hour/value are client-side)",
)
@click.option(
    "--min-ratings",
    type=int,
    default=1,
    help="Minimum number of ratings (default: 1, filters unreviewed)",
)
@click.option(
    "--min-hours",
    type=float,
    default=0.0,
    help="Minimum length in hours (filters out shorts)",
)
@click.option(
    "--pages",
    type=click.IntRange(min=1),
    default=10,
    help="Pages to scan per sort order (50 items/page, default: 10)",
)
@click.option(
    "--subcategories/--no-subcategories",
    default=False,
    help="Scan each subcategory of the genre separately for deeper coverage (multiplies API calls)",
)
@_common_filter_options
@click.pass_context
def find(
    ctx,
    category,
    genre,
    exclude_genre,
    keywords,
    max_price,
    max_pph,
    sort,
    min_rating,
    min_ratings,
    min_hours,
    narrator,
    author,
    series,
    publisher,
    exclude_authors,
    exclude_narrators,
    on_sale,
    min_discount,
    deep,
    pages,
    subcategories,
    language,
    all_languages,
    first_in_series,
    skip_owned,
    exclude_seen,
    limit,
    output,
    json_flag,
    quiet,
    show_url,
    profile_name,
    interactive,
    dry_run,
    skip_plus,
    only_plus,
    exclude_keywords,
):
    """Find deals: browse the catalog filtered by price and genre.

    Scans multiple pages of the catalog, then filters client-side for
    items under your price threshold. Price and discount sorting happen
    after fetching since the Audible API doesn't support price sort.

    Use --deep to scan with multiple sort orders (BestSellers, newest,
    highest rated) for broader coverage at the cost of more API calls.

    \b
    Examples:
        deals find --genre "sci-fi" --max-price 5
        deals find --genre thriller --sort discount --on-sale --deep
        deals find --profile my-scifi
        deals find --author "Andy Weir" --max-price 10
        deals find --genre sci-fi --exclude-author "Sarah J. Maas" --max-price 5
        deals find --genre "sci-fi" --subcategories --max-price 5
    """
    logger.info(
        "find genre=%r category=%r keywords=%r max_price=%s pages=%s sort=%s deep=%s",
        genre,
        category,
        keywords,
        max_price,
        pages,
        sort,
        deep,
    )
    if skip_plus and only_plus:
        raise click.UsageError("--skip-plus and --only-plus are mutually exclusive")
    s = _build_scan_settings(
        ctx,
        profile_name,
        max_price=max_price,
        max_pph=max_pph,
        sort=sort,
        min_rating=min_rating,
        min_ratings=min_ratings,
        min_hours=min_hours,
        min_discount=min_discount,
        language=language,
        narrator=narrator,
        author=author,
        pages=pages,
        limit=limit,
        on_sale=on_sale,
        deep=deep,
        first_in_series=first_in_series,
        all_languages=all_languages,
        skip_owned=skip_owned,
        interactive=interactive,
        genre=genre,
        exclude_genre=exclude_genre,
        exclude_authors=exclude_authors,
        exclude_narrators=exclude_narrators,
        keywords=keywords,
        series=series,
        publisher=publisher,
        skip_plus=skip_plus,
        only_plus=only_plus,
        exclude_keywords=exclude_keywords,
    )
    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)
    if s.genre and category:
        raise click.UsageError("Use --genre or --category, not both.")

    dc = _get_client(ctx.obj["locale"])
    server_sort = SORT_OPTIONS.get(s.sort, "BestSellers")
    sort_orders = DEEP_SORT_ORDERS if s.deep else [server_sort]

    with dc:
        category, category_name, exclude_category_ids = _resolve_categories(
            dc, s.genre, category, s.exclude_genre
        )

        if subcategories and not category:
            raise click.UsageError("--subcategories requires --genre or --category")

        child_ids: list[str] = []
        if subcategories and category:
            children = dc.get_categories(root=category)
            child_ids = [c["id"] for c in children if c.get("id")]

        if dry_run:
            sub_count = len(child_ids) if subcategories and child_ids else None
            _print_dry_run_summary(
                category_name=category_name,
                query=s.keywords,
                sort_orders=sort_orders,
                pages=s.pages,
                subcategory_count=sub_count,
            )
            return

        skip_asins = _resolve_skip_asins(dc, s.skip_owned, exclude_seen)

        desc_parts = []
        if s.keywords:
            desc_parts.append(f'"{s.keywords}"')
        if category:
            desc_parts.append(category_name or category)
        if not desc_parts:
            desc_parts.append("entire catalog")
        desc_str = ", ".join(desc_parts)

        if subcategories and child_ids:
            scan_category_ids = child_ids
            description = f"Scanning {desc_str} ({len(child_ids)} subcategories)"
        else:
            if subcategories:
                console.print(
                    "[dim]No subcategories found; scanning the category directly.[/dim]"
                )
            scan_category_ids = [category]
            description = f"Scanning {desc_str}"

        all_products = _fetch_with_progress(
            dc,
            keywords=s.keywords,
            category_ids=scan_category_ids,
            sort_orders=sort_orders,
            pages=s.pages,
            description=description,
        )

    cur = _currency(ctx)
    find_title = f"Deals under {cur}{s.max_price:.2f}"
    if category_name:
        find_title += f" in {category_name}"
    if s.keywords:
        find_title += f' matching "{s.keywords}"'
    filtered, filter_breakdown, editions_removed, series_collapsed = (
        _apply_settings_filters(
            all_products,
            s,
            skip_asins=skip_asins,
            exclude_category_ids=exclude_category_ids,
        )
    )
    filtered, serialized, total_before_limit = _record_and_cache(
        filtered,
        title=find_title,
        limit=s.limit,
    )
    _emit_output(
        filtered,
        serialized,
        title=find_title,
        output=output,
        json_flag=json_flag,
        quiet=quiet,
        max_price=s.max_price,
        filter_breakdown=filter_breakdown,
        editions_removed=editions_removed,
        series_collapsed=series_collapsed,
        total_before_limit=total_before_limit,
        currency=cur,
        interactive=s.interactive,
        show_url=show_url,
    )


@cli.command()
@click.option(
    "--sort",
    type=click.Choice(
        ["title", "rating", "length", "date", "price", "-price", "price-per-hour"]
    ),
    default="date",
    help="Sort order (default: date — newest first)",
)
@click.option(
    "-n",
    "--limit",
    type=click.IntRange(min=0),
    default=None,
    help="Show only the top N results",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Export to file (.json or .csv)",
)
@click.option(
    "--json", "json_flag", is_flag=True, default=False, help="Output as JSON to stdout"
)
@click.option(
    "-q", "--quiet", is_flag=True, default=False, help="Suppress table output"
)
@click.option("--author", default="", help="Filter by author name (substring match)")
@click.option(
    "--narrator",
    default="",
    help="Filter by narrator name (substring match, client-side)",
)
@click.option(
    "--genre",
    default="",
    help="Filter by genre/category (substring match on categories)",
)
@click.option("--min-rating", type=float, default=0.0, help="Minimum rating")
@click.option("--min-ratings", type=int, default=0, help="Minimum number of ratings")
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours")
@click.option(
    "--stats",
    is_flag=True,
    default=False,
    help="Show aggregate library statistics instead of the table",
)
@click.pass_context
def library(
    ctx,
    sort,
    limit,
    output,
    json_flag,
    quiet,
    author,
    narrator,
    genre,
    min_rating,
    min_ratings,
    min_hours,
    stats,
):
    """List all audiobooks in your Audible library.

    Fetches your full library with metadata — useful for exporting to
    a file for analysis or feeding to other tools.

    \b
    Examples:
        deals library
        deals library --json > my-books.json
        deals library -o library.csv
        deals library --sort rating -n 20
        deals library --author "Andy Weir"
        deals library --genre sci-fi --min-rating 4.0
    """
    logger.info(
        "library sort=%s limit=%s author=%r narrator=%r genre=%r",
        sort,
        limit,
        author,
        narrator,
        genre,
    )
    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)

    dc = _get_client(ctx.obj["locale"])
    all_products: list[Product] = []
    with dc:
        with create_scan_progress() as progress:
            task = progress.add_task("Fetching library", total=None, items=0)
            page_count = 0
            for page_products, page_num in dc.get_library_pages():
                all_products.extend(page_products)
                page_count = page_num
                progress.update(task, completed=page_num, items=len(all_products))
            progress.update(task, total=page_count, completed=page_count)

    filtered, filter_breakdown = filter_products(
        all_products,
        author=author,
        narrator=narrator,
        min_rating=min_rating,
        min_ratings=min_ratings,
        min_hours=min_hours,
        genre=genre,
    )

    filtered = sort_local(filtered, sort)
    stats_products = filtered  # stats always use the full filtered list
    total_before_limit = len(filtered)
    if limit is not None and limit > 0:
        filtered = filtered[:limit]

    cur = _currency(ctx)

    if output:
        export_products(filtered, output)
        console.print(f"[green]Exported {len(filtered)} items to {output}[/green]")
    if json_flag:
        serialized = [serialize_product(p) for p in filtered]
        click.echo(json_mod.dumps(serialized, indent=2, ensure_ascii=False))
    if not json_flag and not quiet:
        console.print()
        if stats:
            display_library_stats(stats_products, cur)
        else:
            title = "Your Library"
            display_products(filtered, title=title, currency=cur)
            if filter_breakdown:
                display_summary(
                    len(filtered),
                    filter_breakdown,
                    currency=cur,
                    total_before_limit=total_before_limit,
                    noun="books",
                )
            elif total_before_limit > len(filtered):
                console.print(
                    f"  [bold]{len(filtered)}[/bold] of {total_before_limit} books shown"
                )
            else:
                console.print(f"  [bold]{len(filtered)}[/bold] books in library")


@cli.command()
@click.option(
    "--min-books",
    type=click.IntRange(min=1),
    default=2,
    help="Minimum books owned in a series to consider it 'invested' (default: 2)",
)
@click.option(
    "--max-series",
    type=click.IntRange(min=1),
    default=20,
    help="Maximum number of series to scan (default: 20, most-invested first)",
)
@click.option(
    "--series",
    "series_filter",
    default="",
    help="Filter to a specific series name (substring match)",
)
@click.option(
    "--max-price", type=click.FloatRange(min=0), default=None, help="Max price filter"
)
@click.option("--min-rating", type=float, default=0.0, help="Minimum rating (e.g. 4.0)")
@click.option("--min-ratings", type=int, default=0, help="Minimum number of ratings")
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours")
@click.option(
    "--on-sale", is_flag=True, default=False, help="Only show discounted items"
)
@click.option(
    "--sort",
    type=click.Choice(
        [
            "price",
            "-price",
            "discount",
            "price-per-hour",
            "rating",
            "length",
            "date",
            "title",
        ]
    ),
    default="price-per-hour",
    help="Sort order (default: price-per-hour)",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(min=0),
    default=25,
    help="Show only the top N results (0 for unlimited, default: 25)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Export results to file (.json or .csv)",
)
@click.option(
    "--json",
    "json_flag",
    is_flag=True,
    default=False,
    help="Output results as JSON to stdout",
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    default=False,
    help="Suppress table output (useful with --output)",
)
@click.option(
    "--interactive",
    "-i",
    is_flag=True,
    default=False,
    help="Browse results interactively",
)
@click.option(
    "--pages",
    type=click.IntRange(min=1),
    default=3,
    help="Pages to scan per series search (default: 3)",
)
@click.pass_context
def series(
    ctx,
    min_books,
    max_series,
    series_filter,
    max_price,
    min_rating,
    min_ratings,
    min_hours,
    on_sale,
    sort,
    limit,
    output,
    json_flag,
    quiet,
    interactive,
    pages,
):
    """Find continuation books in series you're invested in.

    Scans your library for series where you own multiple books, then
    searches the catalog for other books in those series that you don't
    own yet. Great for catching up on series during sales.

    \b
    Examples:
        deals series
        deals series --min-books 3 --max-price 10
        deals series --series "Expeditionary Force" --on-sale
        deals series --sort discount -n 50
        deals series --json -o series-deals.json
    """
    logger.info(
        "series min_books=%s max_series=%s filter=%r max_price=%s sort=%s",
        min_books,
        max_series,
        series_filter,
        max_price,
        sort,
    )
    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)

    s = Settings.resolve(
        ctx,
        config=ctx.obj.get("config", {}),
        profile=None,
        cli_flags=dict(
            max_price=max_price,
            min_rating=min_rating,
            min_ratings=min_ratings,
            min_hours=min_hours,
            on_sale=on_sale,
            limit=limit,
            sort=sort,
            pages=pages,
        ),
    )
    max_price, min_rating, min_ratings = s.max_price, s.min_rating, s.min_ratings
    min_hours, on_sale, limit = s.min_hours, s.on_sale, s.limit
    sort, pages = s.sort, s.pages

    dc = _get_client(ctx.obj["locale"])
    cur = _currency(ctx)

    with dc:
        # 1. Fetch library
        if not quiet and not json_flag:
            console.print("[dim]Fetching library...[/dim]")
        lib_products = dc.get_library()
        owned_asins = {p.asin for p in lib_products}

        # 2. Identify invested series (user owns min_books+ books)
        series_map: dict[str, list[Product]] = {}  # series_name -> [products]
        for p in lib_products:
            if not p.series_name:
                continue
            series_map.setdefault(p.series_name, []).append(p)

        invested = {
            name: books for name, books in series_map.items() if len(books) >= min_books
        }

        if series_filter:
            filter_lower = series_filter.lower()
            invested = {
                name: books
                for name, books in invested.items()
                if filter_lower in name.lower()
            }

        if not invested:
            if series_filter:
                console.print(
                    f"[dim]No invested series matching '{series_filter}' "
                    f"(need {min_books}+ owned books).[/dim]"
                )
            else:
                console.print(
                    f"[dim]No series with {min_books}+ owned books found.[/dim]"
                )
            return

        # Sort by most-invested (most owned books) first, then limit
        invested_sorted = sorted(
            invested.items(), key=lambda x: len(x[1]), reverse=True
        )
        if len(invested_sorted) > max_series:
            if not quiet and not json_flag:
                console.print(
                    f"[dim]Found {len(invested_sorted)} invested series, scanning top {max_series} (use --max-series to adjust).[/dim]"
                )
            invested_sorted = invested_sorted[:max_series]
        elif not quiet and not json_flag:
            console.print(
                f"[dim]Found {len(invested_sorted)} invested series. Searching for continuation books...[/dim]"
            )

        # 3. Fetch catalog entries for each series
        all_candidates: list[Product] = []
        seen_asins: set[str] = set(owned_asins)

        with create_scan_progress() as progress:
            task = progress.add_task(
                f"Scanning {len(invested_sorted)} series",
                total=len(invested_sorted),
                items=0,
            )

            for series_idx, (sname, owned_books) in enumerate(invested_sorted):
                series_asin = next(
                    (ob.series_asin for ob in owned_books if ob.series_asin), ""
                )

                if series_asin:
                    # Direct lookup via series ASIN
                    series_products = dc.get_series_products(series_asin)
                else:
                    # Fallback: keyword search when no series ASIN available
                    series_products = []
                    author_hint = next(
                        (ob.authors[0] for ob in owned_books if ob.authors), ""
                    )
                    keywords = f"{sname} {author_hint}".strip()
                    sname_lower = sname.lower()
                    for page_products, _, _ in dc.search_pages(
                        keywords=keywords,
                        sort_by="Relevance",
                        max_pages=pages,
                    ):
                        for p in page_products:
                            if p.series_name and p.series_name.lower() == sname_lower:
                                series_products.append(p)

                for p in series_products:
                    if p.asin in seen_asins:
                        continue
                    seen_asins.add(p.asin)
                    all_candidates.append(p)

                progress.update(
                    task, completed=series_idx + 1, items=len(all_candidates)
                )

                # Rate limit between series lookups
                if series_idx < len(invested_sorted) - 1:
                    time.sleep(0.3)

    # 4. Post-process using shared pipeline
    series_title = f"Series Continuation Books ({len(invested_sorted)} series)"
    filtered, filter_breakdown, editions_removed, series_collapsed = _apply_filters(
        all_candidates,
        max_price=max_price,
        min_rating=min_rating,
        min_ratings=min_ratings,
        min_hours=min_hours,
        narrator="",
        author="",
        exclude_authors=(),
        exclude_narrators=(),
        language="",
        on_sale=on_sale,
        skip_asins=None,
        exclude_category_ids=set(),
        first_in_series_only=False,
        sort=sort,
        drop_zero_length=False,
    )
    filtered, serialized, total_before_limit = _record_and_cache(
        filtered,
        title=series_title,
        limit=limit,
    )
    _emit_output(
        filtered,
        serialized,
        title=series_title,
        output=output,
        json_flag=json_flag,
        quiet=quiet,
        max_price=max_price,
        filter_breakdown=filter_breakdown,
        editions_removed=editions_removed,
        series_collapsed=series_collapsed,
        total_before_limit=total_before_limit,
        currency=cur,
        interactive=interactive,
    )


@cli.command("last")
@click.option(
    "--sort",
    type=click.Choice(
        [
            "price",
            "-price",
            "discount",
            "price-per-hour",
            "value",
            "rating",
            "length",
            "date",
            "relevance",
        ]
    ),
    default=None,
    help="Re-sort results",
)
@click.option(
    "--max-price", type=click.FloatRange(min=0), default=None, help="Max price filter"
)
@click.option(
    "--max-price-per-hour",
    "max_pph",
    type=click.FloatRange(min=0),
    default=None,
    help="Max price per hour (e.g. 0.50)",
)
@click.option("--min-rating", type=float, default=0.0, help="Minimum rating")
@click.option("--min-ratings", type=int, default=0, help="Minimum number of ratings")
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours")
@click.option(
    "--narrator",
    default="",
    help="Filter by narrator name (substring match, client-side)",
)
@click.option("--author", default="", help="Filter by author name (substring match)")
@click.option("--series", default="", help="Filter by series name (substring match)")
@click.option(
    "--publisher", default="", help="Filter by publisher name (substring match)"
)
@click.option(
    "--exclude-author",
    "exclude_authors",
    multiple=True,
    help="Exclude author (substring match, repeatable)",
)
@click.option(
    "--exclude-narrator",
    "exclude_narrators",
    multiple=True,
    help="Exclude narrator (substring match, repeatable)",
)
@click.option("--language", default="", help="Language filter")
@click.option(
    "--on-sale", is_flag=True, default=False, help="Only show discounted items"
)
@click.option(
    "--min-discount",
    type=click.IntRange(min=0, max=100),
    default=0,
    help="Minimum discount percentage (e.g. 70)",
)
@click.option(
    "--first-in-series",
    is_flag=True,
    default=False,
    help="Show only first book per series",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(min=0),
    default=None,
    help="Show only the top N results",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Export results to file (.json or .csv)",
)
@click.option(
    "--json",
    "json_flag",
    is_flag=True,
    default=False,
    help="Output results as JSON to stdout",
)
@click.option(
    "--quiet", "-q", is_flag=True, default=False, help="Suppress table output"
)
@click.option(
    "--show-url",
    is_flag=True,
    default=False,
    help="Show Audible URL for each item in the table",
)
@click.option(
    "--interactive",
    "-i",
    is_flag=True,
    default=False,
    help="Browse results interactively",
)
@click.option(
    "--clear", is_flag=True, default=False, help="Delete the cached results and exit"
)
@click.option(
    "--clear-seen",
    is_flag=True,
    default=False,
    help="Clear the cumulative seen-ASINs list and exit",
)
@click.option(
    "--count",
    "count_only",
    is_flag=True,
    default=False,
    help="Show total cached result count (ignores filters)",
)
@click.option(
    "--skip-plus/--no-skip-plus",
    default=False,
    help="Exclude Audible Plus catalog titles",
)
@click.option(
    "--only-plus/--no-only-plus",
    default=False,
    help="Show only Audible Plus catalog titles",
)
@click.option(
    "--exclude-keyword",
    "exclude_keywords",
    multiple=True,
    help="Drop results with title/subtitle matching keyword (repeatable)",
)
@click.pass_context
def last_cmd(
    ctx,
    sort,
    max_price,
    max_pph,
    min_rating,
    min_ratings,
    min_hours,
    narrator,
    author,
    series,
    publisher,
    exclude_authors,
    exclude_narrators,
    language,
    on_sale,
    min_discount,
    first_in_series,
    limit,
    output,
    json_flag,
    quiet,
    show_url,
    interactive,
    clear,
    clear_seen,
    count_only,
    skip_plus,
    only_plus,
    exclude_keywords,
):
    """Re-display results from the last search or find, with optional re-filtering.

    No API calls are made — results are read from the local cache.

    \b
    Examples:
        deals last
        deals last --sort discount
        deals last --max-price 3 --min-rating 4
        deals last --narrator "R.C. Bray" --min-ratings 100
        deals last --author "Andy Weir"
        deals last --clear
        deals last --clear-seen
    """
    logger.info(
        "last sort=%s max_price=%s clear=%s clear_seen=%s count=%s",
        sort,
        max_price,
        clear,
        clear_seen,
        count_only,
    )
    if skip_plus and only_plus:
        raise click.UsageError("--skip-plus and --only-plus are mutually exclusive")
    did_clear = False
    if clear_seen:
        if clear_seen_asins():
            console.print("[green]Seen ASINs list cleared.[/green]")
        else:
            console.print("[dim]No seen ASINs to clear.[/dim]")
        did_clear = True
    if clear:
        if clear_last_results():
            console.print("[green]Last results cache cleared.[/green]")
        else:
            console.print("[dim]No cached results to clear.[/dim]")
        did_clear = True
    if did_clear:
        return
    if count_only:
        cached_title, data = load_last_results()
        click.echo(len(data))
        return
    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)
    cached_title, data = load_last_results()
    products = [p for d in data if (p := deserialize_product(d)) is not None]

    effective_sort = sort or ""  # preserve original cache order when no --sort given
    cur = _currency(ctx)
    filtered, filter_breakdown, editions_removed, series_collapsed = _apply_filters(
        products,
        max_price=max_price,
        min_rating=min_rating,
        min_ratings=min_ratings,
        min_hours=min_hours,
        narrator=narrator,
        author=author,
        exclude_authors=exclude_authors,
        exclude_narrators=exclude_narrators,
        language=language,
        on_sale=on_sale,
        skip_asins=None,
        exclude_category_ids=set(),
        first_in_series_only=first_in_series,
        sort=effective_sort,
        max_pph=max_pph,
        min_discount=min_discount,
        series=series,
        publisher=publisher,
        skip_plus=skip_plus,
        only_plus=only_plus,
        exclude_keywords=exclude_keywords,
    )
    filtered, serialized, total_before_limit = _record_and_cache(
        filtered,
        title=cached_title,
        write_cache=False,
        limit=limit,
    )
    _emit_output(
        filtered,
        serialized,
        title=cached_title,
        output=output,
        json_flag=json_flag,
        quiet=quiet,
        max_price=max_price,
        filter_breakdown=filter_breakdown,
        editions_removed=editions_removed,
        series_collapsed=series_collapsed,
        total_before_limit=total_before_limit,
        currency=cur,
        interactive=interactive,
        show_url=show_url,
    )


@cli.command()
@click.argument("asin", required=False, default=None)
@click.option(
    "--last",
    "last_ref",
    type=str,
    default=None,
    help="Use result #N from last search/find",
)
@click.pass_context
def detail(ctx, asin, last_ref):
    """Show detailed info for a product by ASIN."""
    if last_ref is not None:
        asin, desc = _resolve_single_last_ref(last_ref)
        console.print(f"[dim]{desc}[/dim]")
    if not asin:
        raise click.UsageError("Provide an ASIN or use --last N.")
    validate_asin(asin)
    dc = _get_client(ctx.obj["locale"])
    with dc:
        try:
            product = dc.get_product(asin)
        except ValueError as e:
            raise click.ClickException(str(e))

    display_product_detail(product)


@cli.command("open")
@click.argument("asin", required=False, default=None)
@click.option(
    "--last",
    "last_ref",
    type=str,
    default=None,
    help="Use result #N from last search/find",
)
@click.pass_context
def open_cmd(ctx, asin, last_ref):
    """Open an audiobook's Audible page in your browser."""
    if last_ref is not None:
        asin, desc = _resolve_single_last_ref(last_ref)
        console.print(f"[dim]{desc}[/dim]")
    if not asin:
        raise click.UsageError("Provide an ASIN or use --last N.")
    validate_asin(asin)
    url = product_url(asin, ctx.obj["locale"])
    console.print(f"[dim]Opening {url}[/dim]")
    click.launch(url)


@cli.command()
@click.argument("asins", nargs=-1, required=False)
@click.option(
    "--last",
    "last_refs",
    type=str,
    multiple=True,
    help="Use result #N from last search/find (repeatable)",
)
@click.pass_context
def compare(ctx, asins, last_refs):
    """Compare multiple products side-by-side.

    \b
    Example:
        deals compare B00R6S1RCY B00I2VWW5U B019NMZ6FE
        deals compare --last 1 --last 3
    """
    all_asins = _collect_asins(asins, last_refs)

    if len(all_asins) < 2:
        raise click.UsageError("Provide at least 2 ASINs to compare.")

    for asin in all_asins:
        validate_asin(asin)

    dc = _get_client(ctx.obj["locale"])
    with dc:
        products = dc.get_products_batch(all_asins)

    found_asins = {p.asin for p in products}
    for asin in all_asins:
        if asin not in found_asins:
            console.print(f"[red]Not found: {asin}[/red]")

    if len(products) < 2:
        raise click.ClickException("Need at least 2 valid products to compare.")

    # Preserve the order the user specified
    asin_order = {asin: i for i, asin in enumerate(all_asins)}
    products.sort(key=lambda p: asin_order.get(p.asin, 999))

    display_comparison(products)


@cli.group()
def wishlist():
    """Manage your audiobook wishlist."""


@wishlist.command("add")
@click.argument("asins", nargs=-1, required=False)
@click.option(
    "--max-price", type=float, default=None, help="Alert when price drops below this"
)
@click.option(
    "--last",
    "last_refs",
    type=str,
    multiple=True,
    help="Use result #N from last search/find (repeatable)",
)
@click.pass_context
def wishlist_add(ctx, asins, max_price, last_refs):
    """Add ASINs to your wishlist.

    \b
    Example:
        deals wishlist add B00R6S1RCY B00I2VWW5U --max-price 5
        deals wishlist add --last 1 --last 2 --max-price 5
    """
    all_asins = _collect_asins(asins, last_refs)
    if not all_asins:
        raise click.UsageError("Provide at least one ASIN or use --last N.")

    items = load_wishlist()
    existing = {item["asin"] for item in items}

    for asin in all_asins:
        validate_asin(asin)

    dc = _get_client(ctx.obj["locale"])
    added = 0
    with dc:
        for asin in all_asins:
            if asin in existing:
                console.print(f"[dim]{asin} already on wishlist[/dim]")
                continue
            try:
                p = dc.get_product(asin)
            except ValueError:
                console.print(f"[red]Not found: {asin}[/red]")
                continue
            items.append(wishlist_entry(p, max_price))
            existing.add(p.asin)
            added += 1
            console.print(f"[green]+[/green] {p.title} ({p.asin})")

    save_wishlist(items)
    console.print(f"\n[bold]{added}[/bold] added, {len(items)} total on wishlist")


@wishlist.command("remove")
@click.argument("asins", nargs=-1, required=False)
@click.option(
    "--last",
    "last_refs",
    type=str,
    multiple=True,
    help="Use result #N from last search/find (repeatable)",
)
def wishlist_remove(asins, last_refs):
    """Remove ASINs from your wishlist."""
    all_asins = _collect_asins(asins, last_refs)
    if not all_asins:
        raise click.UsageError("Provide at least one ASIN or use --last N.")
    for asin in all_asins:
        validate_asin(asin)
    items = load_wishlist()
    remove_set = set(all_asins)
    before = len(items)
    items = [i for i in items if i["asin"] not in remove_set]
    save_wishlist(items)
    removed = before - len(items)
    console.print(f"[bold]{removed}[/bold] removed, {len(items)} remaining")


@wishlist.command("list")
@click.pass_context
def wishlist_list(ctx):
    """Show your wishlist."""
    cur = _currency(ctx)
    items = load_wishlist()
    if not items:
        console.print(
            "[dim]Wishlist is empty. Use 'deals wishlist add ASIN' to add items.[/dim]"
        )
        return

    table = Table(
        title="Wishlist", show_lines=False, padding=(0, 1), title_style="bold"
    )
    table.add_column("ASIN", style="cyan", width=14)
    table.add_column("Title", max_width=40)
    table.add_column("Target", justify="right", width=10)

    for item in items:
        target = f"{cur}{item['max_price']:.2f}" if item.get("max_price") else "-"
        table.add_row(item["asin"], item["title"], target)

    console.print(table)


@wishlist.command("sync")
@click.option(
    "--max-price",
    type=float,
    default=None,
    help="Set target price for all synced items",
)
@click.option(
    "--update",
    is_flag=True,
    default=False,
    help="Update target price for existing items too",
)
@click.pass_context
def wishlist_sync(ctx, max_price, update):
    """Sync your Audible account wishlist into the local watchlist.

    Fetches all items from your Audible account wishlist and adds any that
    are not already tracked locally. Existing local items are never removed.

    \b
    Examples:
        deals wishlist sync
        deals wishlist sync --max-price 5
        deals wishlist sync --max-price 5 --update
    """
    if update and max_price is None:
        raise click.UsageError("--update requires --max-price to be set")

    dc = _get_client(ctx.obj["locale"])
    with dc:
        audible_items = dc.get_wishlist()

    local_items = load_wishlist()
    local_by_asin = {item["asin"]: item for item in local_items}
    cur = _currency(ctx)

    added = 0
    skipped = 0
    updated = 0
    for product in audible_items:
        if product.asin in local_by_asin:
            if update:
                local_by_asin[product.asin]["max_price"] = max_price
                updated += 1
                console.print(
                    f"[yellow]~[/yellow] {product.title} ({product.asin}) → target {cur}{max_price:.2f}"
                )
            else:
                skipped += 1
            continue
        local_items.append(wishlist_entry(product, max_price))
        added += 1
        console.print(f"[green]+[/green] {product.title} ({product.asin})")

    save_wishlist(local_items)
    console.print(
        f"\n[bold]{added}[/bold] synced, "
        f"{updated} updated, "
        f"{skipped} already tracked, "
        f"{len(local_items)} total on wishlist"
    )


def _watch_once(
    ctx: click.Context,
    buy_only: bool = False,
    sort_by: str | None = None,
    show_url: bool = False,
) -> int:
    """Run a single wishlist price check. Returns the number of BUY hits."""
    items = load_wishlist()
    if not items:
        console.print(
            "[dim]Wishlist is empty. Use 'deals wishlist add ASIN' to add items.[/dim]"
        )
        return 0

    dc = _get_client(ctx.obj["locale"])
    targets: dict[str, float | None] = {
        item["asin"]: item.get("max_price") for item in items
    }

    with dc:
        products = dc.get_products_batch([item["asin"] for item in items])

    _safe_record_prices(products)
    found_asins = {p.asin for p in products}
    for item in items:
        if item["asin"] not in found_asins:
            console.print(f"[red]Not found: {item['asin']} ({item['title']})[/red]")

    if not products:
        return 0

    if sort_by:
        products = sort_local(products, sort_by)

    cur = _currency(ctx)
    return display_watch_table(products, targets, cur, buy_only, show_url)


@cli.command()
@click.option(
    "--every",
    default=None,
    help="Re-check on an interval (e.g. '30m', '2h', '1h30m'). Runs until interrupted.",
)
@click.option(
    "--buy-only",
    is_flag=True,
    default=False,
    help="Only show items at or below target price",
)
@click.option(
    "--sort",
    "sort_by",
    type=click.Choice(
        ["title", "author", "price", "asin", "release-date"], case_sensitive=False
    ),
    default=None,
    help="Sort results by field",
)
@click.option(
    "--show-url", is_flag=True, default=False, help="Show Audible URL for each item"
)
@click.option(
    "--exit-code",
    is_flag=True,
    default=False,
    help="Exit 0 if any items hit target, 1 if none",
)
@click.pass_context
def watch(ctx, every, buy_only, sort_by, show_url, exit_code):
    """Check wishlist prices and highlight deals.

    Fetches current prices for all wishlist items and shows which ones
    are at or below your target price.

    Use --every to keep checking on an interval instead of exiting after
    one check. Press Ctrl+C to stop.

    \b
    Examples:
        deals watch
        deals watch --every 30m
        deals watch --every 2h
        deals watch --buy-only
        deals watch --sort title
        deals watch --show-url
    """
    logger.info("watch every=%s buy_only=%s sort_by=%s", every, buy_only, sort_by)
    if exit_code and every:
        raise click.UsageError("--exit-code requires a single check; drop --every")
    if not every:
        hits = _watch_once(ctx, buy_only=buy_only, sort_by=sort_by, show_url=show_url)
        if exit_code and hits == 0:
            ctx.exit(1)
        return

    interval = parse_interval(every)
    console.print(f"[dim]Watching every {every} (Ctrl+C to stop)...[/dim]\n")
    try:
        while True:
            _watch_once(ctx, buy_only=buy_only, sort_by=sort_by, show_url=show_url)
            console.print(f"\n  [dim]Next check in {every}... (Ctrl+C to stop)[/dim]\n")
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")


@cli.group("config")
def config_cmd():
    """Manage global defaults for deals commands."""


@config_cmd.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key, value):
    """Set a global default. KEY uses hyphens or underscores.

    \b
    Valid keys: skip-owned, max-price, min-rating, min-ratings, min-hours,
                language, locale, sort, pages, on-sale, deep, first-in-series,
                all-languages, interactive, limit, narrator
    Example:
        deals config set max-price 5
        deals config set skip-owned true
    """
    norm_key = validate_config_key(key)
    coerced = coerce_config_value(norm_key, value)
    cfg = load_config()
    cfg[norm_key] = coerced
    save_config(cfg)
    console.print(f"[green]Config set:[/green] {norm_key} = {coerced!r}")


@config_cmd.command("get")
@click.argument("key")
def config_get(key):
    """Get a global default value."""
    norm_key = validate_config_key(key)
    cfg = load_config()
    if norm_key not in cfg:
        console.print(f"[dim]{norm_key} is not set[/dim]")
    else:
        console.print(f"{norm_key} = {cfg[norm_key]!r}")


@config_cmd.command("list")
def config_list():
    """List all set global defaults."""
    cfg = load_config()
    if not cfg:
        console.print(
            "[dim]No global defaults set. Use 'deals config set KEY VALUE' to set one.[/dim]"
        )
        return
    for k, v in sorted(cfg.items()):
        console.print(f"  {k} = {v!r}")


@config_cmd.command("reset")
@click.argument("key", required=False, default=None)
def config_reset(key):
    """Remove a key from global defaults, or clear all if no key given."""
    cfg = load_config()
    if key is None:
        if not click.confirm("Remove all global defaults?"):
            console.print("[dim]Cancelled.[/dim]")
            return
        save_config({})
        console.print("[green]All global defaults cleared.[/green]")
        return
    norm_key = validate_config_key(key)
    if norm_key in cfg:
        del cfg[norm_key]
        save_config(cfg)
        console.print(f"[green]Config key '{norm_key}' removed.[/green]")
    else:
        console.print(f"[dim]Config key '{norm_key}' was not set.[/dim]")


@cli.group()
def profile():
    """Manage saved search profiles."""


@profile.command("save")
@click.argument("name")
@click.option("--genre", default="")
@click.option("--exclude-genre", multiple=True)
@click.option("--keywords", default="")
@click.option("--max-price", type=float, default=None)
@click.option("--sort", default="")
@click.option("--min-rating", type=float, default=0.0)
@click.option("--min-ratings", type=int, default=0)
@click.option("--min-hours", type=float, default=0.0)
@click.option("--narrator", default="")
@click.option("--author", default="")
@click.option("--series", default="")
@click.option("--exclude-author", "exclude_authors", multiple=True)
@click.option("--exclude-narrator", "exclude_narrators", multiple=True)
@click.option("--on-sale/--no-on-sale", default=False)
@click.option("--min-discount", type=click.IntRange(min=0, max=100), default=0)
@click.option(
    "--max-price-per-hour", "max_pph", type=click.FloatRange(min=0), default=None
)
@click.option("--publisher", default="")
@click.option("--deep/--no-deep", default=False)
@click.option("--pages", type=int, default=None)
@click.option("--first-in-series/--no-first-in-series", default=False)
@click.option("--all-languages/--no-all-languages", default=False)
@click.option("--limit", "-n", type=click.IntRange(min=0), default=None)
@click.option("--skip-owned/--no-skip-owned", default=False)
@click.option("--language", default="")
@click.option("--interactive/--no-interactive", "-i", default=False)
@click.option("--skip-plus/--no-skip-plus", default=False)
@click.option("--only-plus/--no-only-plus", default=False)
@click.option("--exclude-keyword", "exclude_keywords", multiple=True)
@click.pass_context
def profile_save(ctx, name, **kwargs):
    """Save a search profile.

    \b
    Example:
        deals profile save my-scifi --genre sci-fi --max-price 5 --min-rating 4 --first-in-series
        deals profile save work --skip-owned --language english --interactive
        deals find --profile my-scifi
        deals search "Brandon Sanderson" --profile my-scifi
    """
    profiles = load_profiles()
    # Only save values explicitly passed on the command line
    saved = {k: v for k, v in kwargs.items() if ctx.get_parameter_source(k) == _CL}
    profiles[name] = saved
    save_profiles(profiles)
    console.print(f"[green]Profile '{name}' saved[/green] ({len(saved)} options)")


@profile.command("list")
def profile_list():
    """List saved profiles."""
    profiles = load_profiles()
    if not profiles:
        console.print(
            "[dim]No profiles saved. Use 'deals profile save NAME --flags...' to create one.[/dim]"
        )
        return

    for name, opts in profiles.items():
        flags = " ".join(_opts_to_flag_parts(opts))
        console.print(f"  [bold]{name}[/bold]  [dim]{flags}[/dim]")


@profile.command("delete")
@click.argument("name", shell_complete=_complete_profile_names)
def profile_delete(name):
    """Delete a saved profile."""
    profiles = load_profiles()
    if name not in profiles:
        raise click.ClickException(f"Profile '{name}' not found.")
    del profiles[name]
    save_profiles(profiles)
    console.print(f"[green]Profile '{name}' deleted[/green]")


_KEY_TO_FLAG: dict[str, str] = {
    "exclude_authors": "exclude-author",
    "exclude_narrators": "exclude-narrator",
    "exclude_keywords": "exclude-keyword",
    "max_pph": "max-price-per-hour",
}


def _opts_to_flag_parts(opts: dict) -> list[str]:
    """Convert profile opts dict to a list of CLI flag strings."""
    parts: list[str] = []
    for k, v in opts.items():
        flag = _KEY_TO_FLAG.get(k, k.replace("_", "-"))
        if isinstance(v, bool):
            parts.append(f"--{flag}" if v else f"--no-{flag}")
        elif isinstance(v, (list, tuple)):
            parts.extend(f"--{flag} {item}" for item in v)
        else:
            parts.append(f"--{flag} {v}")
    return parts


@profile.command("show")
@click.argument("name", shell_complete=_complete_profile_names)
def profile_show(name):
    """Show the saved flags for a named profile."""
    profiles = load_profiles()
    if name not in profiles:
        raise click.ClickException(f"Profile '{name}' not found.")
    opts = profiles[name]
    console.print(f"\n[bold]Profile: {name}[/bold]\n")
    for part in _opts_to_flag_parts(dict(sorted(opts.items()))):
        console.print(f"  {part}")
    console.print()


@cli.command()
@click.argument("asin", required=False, default=None)
@click.option(
    "--last",
    "last_ref",
    type=str,
    default=None,
    help="Use result #N from last search/find",
)
@click.pass_context
def history(ctx, asin, last_ref):
    """Show price history for an ASIN.

    History is recorded automatically each time an ASIN appears in
    search/find results. Use 'deals history ASIN' to view past prices.
    """
    if last_ref is not None:
        asin, desc = _resolve_single_last_ref(last_ref)
        console.print(f"[dim]{desc}[/dim]")
    if not asin:
        raise click.UsageError("Provide an ASIN or use --last N.")
    validate_asin(asin)
    entries = load_price_history(asin)
    if not entries:
        console.print(
            f"[dim]No price history for {asin}. "
            "History is recorded when items appear in search/find results.[/dim]"
        )
        return

    cur = _currency(ctx)
    display_price_history(entries, asin, cur)


def _post_webhook(url: str, body: bytes, headers: dict[str, str]) -> None:
    """POST body to url with headers. Raises ClickException on failure."""
    import urllib.request

    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        logger.debug("webhook POST %s payload_bytes=%d", url, len(body))
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.exception("webhook POST failed")
        raise click.ClickException(f"Webhook failed: {e}")


@cli.command()
@click.option(
    "--days",
    type=click.IntRange(min=1),
    default=7,
    help="Look back this many days (default: 7)",
)
@click.option(
    "--show-new",
    is_flag=True,
    default=False,
    help="Include newly tracked item details (only count shown by default)",
)
@click.option(
    "--atl",
    is_flag=True,
    default=False,
    help="Include wishlist items at all-time low price",
)
@click.option(
    "--json",
    "json_flag",
    is_flag=True,
    default=False,
    help="Output recap as JSON to stdout",
)
@click.option("--webhook", default=None, help="Webhook URL to POST results to")
@click.option(
    "--webhook-format",
    type=click.Choice(["generic", "slack", "discord", "teams", "ntfy"]),
    default="generic",
    help="Webhook payload format",
)
@click.pass_context
def recap(ctx, days, show_new, atl, json_flag, webhook, webhook_format):
    """Show a recap of price changes across tracked items.

    Scans price history files and reports items that dropped in price,
    new items tracked, and wishlist items at target.
    """
    logger.info("recap days=%s show_new=%s atl=%s", days, show_new, atl)
    if json_flag and webhook:
        raise click.UsageError("--json and --webhook are mutually exclusive")
    if webhook:
        validate_webhook_url(webhook)
    if json_flag:
        console.file = sys.stderr
    cur = _currency(ctx)
    drops, new_items = scan_price_changes(days)
    if not drops and not new_items and not has_price_history():
        if json_flag:
            empty: dict = {
                "days": days,
                "drops": [],
                "new_count": 0,
                "wishlist_hits": [],
            }
            if atl:
                empty["atl_hits"] = []
            click.echo(json_mod.dumps(empty, indent=2))
            return
        console.print(
            "[dim]No price history yet. Run 'deals find' or 'deals search' to start tracking.[/dim]"
        )
        return
    wishlist_hits = find_wishlist_hits()
    atl_hits = find_wishlist_atl_hits() if atl else None

    if json_flag or webhook:

        def _drop_pct(old: float, new: float) -> int:
            return round((old - new) / old * 100) if old > 0 else 0

        payload: dict = {
            "days": days,
            "drops": [
                {
                    "asin": asin,
                    "title": title,
                    "old_price": old,
                    "new_price": new,
                    "drop_pct": _drop_pct(old, new),
                }
                for asin, title, old, new in sorted(
                    drops, key=lambda x: x[2] - x[3], reverse=True
                )
            ],
            "new_count": len(new_items),
            "wishlist_hits": [
                {"asin": h["asin"], "title": h.get("title", "")} for h in wishlist_hits
            ],
        }
        if atl_hits is not None:
            payload["atl_hits"] = [
                {
                    "asin": h["asin"],
                    "title": h.get("title", ""),
                    "price": h["price"],
                    "target": h.get("target"),
                }
                for h in atl_hits
            ]
        if json_flag:
            click.echo(json_mod.dumps(payload, indent=2))
            return
        if webhook:
            nothing = (
                not drops
                and not new_items
                and not wishlist_hits
                and not (atl_hits or [])
            )
            if nothing:
                console.print("[dim]Nothing to send.[/dim]")
                return
            try:
                body, headers = format_recap_payload(
                    payload, webhook_format, currency=cur
                )
            except ValueError as e:
                raise click.ClickException(str(e))
            _post_webhook(webhook, body, headers)
            console.print("[green]Sent recap to webhook[/green]")
            return

    display_recap(
        drops, new_items, wishlist_hits, days, cur, show_new, atl_hits=atl_hits
    )


@cli.command()
@click.option("--webhook", default=None, help="Webhook URL to POST results to")
@click.option(
    "--webhook-format",
    type=click.Choice(["generic", "slack", "discord", "teams", "ntfy"]),
    default="generic",
    help="Webhook payload format",
)
@click.option(
    "--webhook-template",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    default=None,
    help="Path to a template file for webhook body (one block per hit, joined with newlines). Use {{ and }} for literal braces.",
)
@click.option(
    "--exit-code",
    is_flag=True,
    default=False,
    help="Exit 0 if any items hit target, 1 if none",
)
@click.option(
    "--cooldown",
    type=click.IntRange(min=1),
    default=None,
    help="Suppress repeat notifications for N days unless the price drops further",
)
@click.pass_context
def notify(ctx, webhook, webhook_format, webhook_template, exit_code, cooldown):
    """Check wishlist and send notifications for items at target price.

    \b
    Examples:
        deals notify --webhook https://hooks.slack.com/services/...
        deals notify --webhook https://hooks.slack.com/... --webhook-format slack
        deals notify  (prints to stdout as JSON, useful for cron + mail)
    """
    logger.info(
        "notify webhook_set=%s webhook_format=%s webhook_template=%s",
        bool(webhook),
        webhook_format,
        webhook_template,
    )
    if webhook_template is not None and webhook_format != "generic":
        raise click.UsageError(
            "--webhook-template and --webhook-format are mutually exclusive"
        )
    if webhook_template is not None and not webhook:
        raise click.UsageError("--webhook-template requires --webhook")
    if webhook:
        validate_webhook_url(webhook)

    items = load_wishlist()
    if not items:
        console.print("[dim]Wishlist is empty. Use 'deals wishlist add' first.[/dim]")
        return

    dc = _get_client(ctx.obj["locale"])
    targets = {item["asin"]: item.get("max_price") for item in items}

    with dc:
        products = dc.get_products_batch([item["asin"] for item in items])

    _safe_record_prices(products)
    cur = _currency(ctx)
    hits = []
    extras: dict[str, dict] = {}
    for p in products:
        target = targets.get(p.asin)
        if target is not None and p.price is not None and p.price <= target:
            hits.append(
                {
                    "asin": p.asin,
                    "title": p.title,
                    "price": round(p.price, 2),
                    "target": target,
                    "url": p.url,
                }
            )
            extras[p.asin] = {
                "currency": p.currency,
                "discount_pct": float(p.discount_pct or 0.0),
            }

    suppressed = 0
    if cooldown is not None and hits:
        notify_state = load_notify_state()
        today = datetime.date.today()
        kept: list[dict] = []
        for hit in hits:
            asin = hit["asin"]
            entry = notify_state.get(asin)
            if entry:
                try:
                    recorded_date = datetime.date.fromisoformat(entry["date"])
                    recorded_price = float(entry["price"])
                    age = (today - recorded_date).days
                    if hit["price"] >= recorded_price and age < cooldown:
                        suppressed += 1
                        continue
                except (KeyError, ValueError, TypeError):
                    pass
            kept.append(hit)
        hits = kept

    if not hits:
        if not webhook:
            click.echo(json_mod.dumps({"deals": [], "count": 0}, indent=2))
        else:
            if suppressed:
                console.print(
                    f"[dim]{suppressed} deal(s) suppressed by cooldown. Nothing sent.[/dim]"
                )
            else:
                console.print(
                    "[dim]No items at target price. Nothing sent to webhook.[/dim]"
                )
        if exit_code:
            ctx.exit(1)
        return

    if webhook:
        tmpl_str = (
            webhook_template.read_text(encoding="utf-8")
            if webhook_template is not None
            else None
        )
        try:
            body, headers = format_webhook_payload(
                hits,
                webhook_format,
                currency=cur,
                template=tmpl_str,
                extras=extras,
            )
        except ValueError as e:
            raise click.ClickException(str(e))
        _post_webhook(webhook, body, headers)
        console.print(f"[green]Sent {len(hits)} deal(s) to webhook[/green]")
    else:
        click.echo(json_mod.dumps({"deals": hits, "count": len(hits)}, indent=2))

    if cooldown is not None:
        wishlist_asins = {item["asin"] for item in items}
        today_iso = today.isoformat()
        for hit in hits:
            notify_state[hit["asin"]] = {"price": hit["price"], "date": today_iso}
        notify_state = {k: v for k, v in notify_state.items() if k in wishlist_asins}
        save_notify_state(notify_state)


@cli.command()
@click.pass_context
def doctor(ctx):
    """Diagnostic checks for auth, config, and marketplace reachability."""
    rows: list[tuple[str, str, str]] = []
    failures = 0

    def add(check, status, detail=""):
        nonlocal failures
        if status == "FAIL":
            failures += 1
            rendered = "[bold red]✗ FAIL[/bold red]"
        elif status == "WARN":
            rendered = "[yellow]⚠ WARN[/yellow]"
        else:
            rendered = "[green]✓ PASS[/green]"
        rows.append((check, rendered, detail))

    if CONFIG_DIR.exists():
        add("Config directory", "PASS", str(CONFIG_DIR))
    else:
        add("Config directory", "WARN", f"Not found — will be created at {CONFIG_DIR}")

    auth_ok = AUTH_FILE.exists()
    if not auth_ok:
        add("Auth file present", "FAIL", "Run 'deals login' or 'deals import-auth'")
    else:
        add("Auth file present", "PASS", str(AUTH_FILE))

    auth_data = None
    if auth_ok:
        try:
            auth_data = json_mod.loads(AUTH_FILE.read_text())
            if not isinstance(auth_data, dict):
                raise ValueError("not a JSON object")
            add("Auth file parseable", "PASS")
        except Exception as e:
            add("Auth file parseable", "FAIL", str(e))
            auth_ok = False

    if auth_ok and auth_data is not None:
        expires = auth_data.get("expires")
        if expires is None:
            add(
                "Auth token expiry",
                "WARN",
                "expires field missing — token freshness unknown",
            )
        else:
            try:
                exp = float(expires)
                now = time.time()
                if exp < now:
                    add(
                        "Auth token expiry",
                        "FAIL",
                        "Token has expired — run 'deals login'",
                    )
                    auth_ok = False
                elif exp < now + 86400:
                    add(
                        "Auth token expiry",
                        "WARN",
                        "Token expires within 24h — consider refreshing",
                    )
                else:
                    add("Auth token expiry", "PASS")
            except (TypeError, ValueError):
                add("Auth token expiry", "WARN", "Could not parse expires field")

    if auth_ok:
        try:
            dc = _get_client(ctx.obj["locale"])
            with dc:
                dc._api_get("1.0/catalog/products", num_results=1)
            add("Marketplace reachable", "PASS")
        except Exception as e:
            add("Marketplace reachable", "FAIL", f"{type(e).__name__}: {e}")
    else:
        add("Marketplace reachable", "WARN", "Skipped — auth checks failed")

    try:
        cfg = load_config()
        if CONFIG_FILE.exists():
            errors = [
                f"{k}: expected {_CONFIG_SCHEMA[k].__name__}, got {type(v).__name__}"
                for k, v in cfg.items()
                if k in _CONFIG_SCHEMA and not isinstance(v, _CONFIG_SCHEMA[k])
            ]
            if errors:
                add("Config file valid", "FAIL", "; ".join(errors))
            else:
                add("Config file valid", "PASS")
        else:
            add("Config file valid", "PASS", "No config file (using defaults)")
    except Exception as e:
        add("Config file valid", "FAIL", str(e))

    try:
        load_wishlist()
        add("Wishlist parseable", "PASS")
    except Exception as e:
        add("Wishlist parseable", "FAIL", str(e))

    try:
        load_profiles()
        add("Profiles parseable", "PASS")
    except Exception as e:
        add("Profiles parseable", "FAIL", str(e))

    try:
        load_seen_asins()
        add("Seen-ASINs parseable", "PASS")
    except Exception as e:
        add("Seen-ASINs parseable", "FAIL", str(e))

    if HISTORY_DIR.exists():
        count = sum(1 for _ in HISTORY_DIR.glob("*.json"))
        add("Price history directory", "PASS", f"{count} ASIN(s) tracked")
    else:
        add("Price history directory", "PASS", "Not yet created (optional)")

    table = Table(title="deals doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", style="dim")
    for r in rows:
        table.add_row(*r)
    console.print(table)
    if failures:
        ctx.exit(1)


@cli.command("completions")
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def completions(shell):
    """Generate shell completion script.

    \b
    Install completions:
        deals completions bash >> ~/.bashrc
        deals completions zsh >> ~/.zshrc
        deals completions fish > ~/.config/fish/completions/deals.fish
    """
    import shutil
    import subprocess

    env = {**os.environ, "_DEALS_COMPLETE": f"{shell}_source"}

    deals_bin = shutil.which("deals")
    if deals_bin:
        result = subprocess.run(
            [deals_bin],
            capture_output=True,
            text=True,
            env=env,
        )
    else:
        result = subprocess.run(
            [sys.executable, "-m", "audible_deals"],
            capture_output=True,
            text=True,
            env=env,
        )

    if result.stdout:
        click.echo(result.stdout)
    else:
        click.echo(result.stderr, err=True)
