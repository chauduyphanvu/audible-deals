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

import json as json_mod
import math
import os
from importlib.metadata import version as _pkg_version

try:
    _VERSION = _pkg_version("audible-deals")
except Exception:
    _VERSION = "0.5.1"  # fallback for PyInstaller frozen builds
try:
    import readline  # noqa: F401 — required on macOS for input() with long strings
except ImportError:
    pass  # unavailable on Windows
import sys
import time
import urllib.request
from pathlib import Path

import click
from rich.table import Table

from audible_deals.constants import (
    _ASIN_RE,
    _CONFIG_SCHEMA,
    CLIENT_SORT_OPTIONS,
    CONFIG_FILE,
    DEEP_SORT_ORDERS,
    HISTORY_DIR,
    LAST_RESULTS_FILE,
    LOCALE_CURRENCY,
    LOCALE_DOMAIN,
    LOCALE_LANGUAGES,
    MAX_PAGE_SIZE,
    PROFILES_FILE,
    SEEN_ASINS_FILE,
    SORT_OPTIONS,
    WISHLIST_FILE,
)
from audible_deals.client import AUTH_FILE, DealsClient, Product
from audible_deals.display import (
    console,
    create_scan_progress,
    display_categories,
    display_comparison,
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
    first_in_series as _first_in_series_fn,
    price_per_hour,
    sort_local,
    value_score,
)
from audible_deals.settings import Settings
from audible_deals.utils import (
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
    find_wishlist_hits,
    has_price_history,
    load_config,
    load_last_results,
    load_price_history,
    load_profiles,
    load_seen_asins,
    load_wishlist,
    merge_seen_asins,
    record_prices,
    resolve_last_references,
    save_config,
    save_last_results,
    save_profiles,
    save_seen_asins,
    save_wishlist,
    scan_price_changes,
    validate_config_key,
    wishlist_entry,
)






def _get_client(locale: str) -> DealsClient:
    return DealsClient(locale=locale)


def _safe_record_prices(products: list[Product]) -> None:
    """Record prices, warning on failure instead of crashing."""
    try:
        record_prices(products)
    except Exception as e:
        console.print(f"[dim]Warning: could not record price history: {e}[/dim]")


_CL = click.core.ParameterSource.COMMANDLINE


def _resolve_scan_settings(
    ctx: click.Context,
    profile_name: str | None,
    cli_flags: dict,
) -> dict:
    """Merge config/profile/CLI and return an updated namespace dict."""
    profile: dict | None = None
    if profile_name:
        profiles = load_profiles()
        if profile_name not in profiles:
            raise click.ClickException(
                f"Profile '{profile_name}' not found. "
                "Use 'deals profile list' to see available profiles."
            )
        profile = profiles[profile_name]
    s = Settings.resolve(
        ctx,
        config=ctx.obj.get("config", {}),
        profile=profile,
        cli_flags=cli_flags,
    )
    # Write resolved settings back into the namespace dict
    result = dict(cli_flags)
    for key in (
        "max_price", "sort", "pages", "min_rating", "min_ratings", "min_hours",
        "min_discount", "max_pph", "limit", "language", "narrator", "author",
        "series", "publisher", "on_sale", "deep", "first_in_series", "all_languages",
        "skip_owned", "interactive", "genre", "exclude_genre", "exclude_authors",
        "exclude_narrators", "keywords",
    ):
        result[key] = getattr(s, key)
    return result


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


def _postprocess_and_output(
    all_products: list[Product],
    *,
    title: str,
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
    first_in_series: bool,
    sort: str,
    limit: int | None,
    output: Path | None,
    json_flag: bool,
    quiet: bool,
    currency: str = "$",
    interactive: bool = False,
    write_cache: bool = True,
    show_url: bool = False,
    max_pph: float | None = None,
    min_discount: int = 0,
    series: str = "",
    publisher: str = "",
) -> None:
    """Shared post-processing pipeline for search and find commands."""
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
    )
    filtered, editions_removed = dedupe_editions(filtered)
    series_collapsed = 0
    if first_in_series:
        filtered, series_collapsed = _first_in_series_fn(filtered)
    filtered = sort_local(filtered, sort)
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

    if output:
        export_products(filtered, output)
        console.print(f"[green]Exported {len(filtered)} items to {output}[/green]")
    if json_flag:
        click.echo(json_mod.dumps(serialized, indent=2, ensure_ascii=False))
    if not json_flag and not quiet:
        console.print()
        display_products(filtered, max_price=max_price, title=title, currency=currency, show_url=show_url)
        display_summary(len(filtered), filter_breakdown, max_price=max_price,
                        editions_removed=editions_removed, series_collapsed=series_collapsed,
                        currency=currency, total_before_limit=total_before_limit)

    if interactive and filtered and not json_flag:
        _interactive_browse(filtered)


def _interactive_browse(products: list[Product]) -> None:
    """Interactive mode: let user pick items to view details, open, or wishlist."""
    console.print("\n  [dim]Enter a # to view details, 'o #' to open in browser, "
                  "'w #' to add to wishlist, or 'q' to quit.[/dim]")
    while True:
        try:
            choice = click.prompt("\n>", default="q", show_default=False).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if choice.lower() == "q":
            break

        # Parse "o 3" or "w 5" or just "3"
        parts = choice.split()
        action = "detail"
        try:
            if len(parts) == 2 and parts[0].lower() in ("o", "w"):
                action = "open" if parts[0].lower() == "o" else "wishlist"
                idx = int(parts[1]) - 1
            else:
                idx = int(parts[0]) - 1
        except (ValueError, IndexError):
            console.print("[dim]Invalid input. Enter a number, 'o #', 'w #', or 'q'.[/dim]")
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
        elif action == "wishlist":
            items = load_wishlist()
            if any(item["asin"] == p.asin for item in items):
                console.print(f"[dim]{p.asin} already on wishlist[/dim]")
            else:
                target_price = None
                try:
                    raw = click.prompt("  Target price (or Enter to skip)", default="", show_default=False).strip()
                    if raw:
                        target_price = float(raw)
                except (ValueError, EOFError):
                    pass
                items.append(wishlist_entry(p, target_price))
                save_wishlist(items)
                target_note = f" (target: {p.currency}{target_price:.2f})" if target_price is not None else ""
                console.print(f"[green]+[/green] {p.title} added to wishlist{target_note}")


class _HandleAuthErrors(click.Group):
    """Catch RuntimeError from missing auth and show a friendly message."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except RuntimeError as e:
            if "Not authenticated" in str(e):
                raise click.ClickException(str(e))
            raise


@click.group(cls=_HandleAuthErrors, invoke_without_command=True)
@click.version_option(version=_VERSION, prog_name="deals")
@click.option("--locale", default="us", help="Audible marketplace (us, uk, ca, de, fr, au, jp, in, es)")
@click.pass_context
def cli(ctx, locale):
    """Audible deal finder - find cheap audiobooks during sales."""
    ctx.ensure_object(dict)
    cfg = load_config()
    ctx.obj["config"] = cfg
    if ctx.get_parameter_source("locale") != _CL:
        cfg_locale = cfg.get("locale")
        if cfg_locale:
            locale = cfg_locale
    ctx.obj["locale"] = locale
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        console.print("\n  [dim]Quick start: deals find --genre sci-fi --max-price 5[/dim]")


@cli.command()
@click.option("--external", is_flag=True, help="Login via external browser (for captcha/2FA)")
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
        click.option("--max-price-per-hour", "max_pph", type=click.FloatRange(min=0), default=None, help="Max price per hour (e.g. 0.50)"),
        click.option("--exclude-genre", multiple=True, help="Genre(s) to exclude (repeatable, fuzzy match)"),
        click.option("--min-rating", type=float, default=0.0, help="Minimum rating (e.g. 4.0)"),
        click.option("--narrator", default="", help="Filter by narrator name (substring match, client-side)"),
        click.option("--author", default="", help="Filter by author name (substring match)"),
        click.option("--series", default="", help="Filter by series name (substring match)"),
        click.option("--publisher", default="", help="Filter by publisher name (substring match)"),
        click.option("--exclude-author", "exclude_authors", multiple=True, help="Exclude author (substring match, repeatable)"),
        click.option("--exclude-narrator", "exclude_narrators", multiple=True, help="Exclude narrator (substring match, repeatable)"),
        click.option("--on-sale/--no-on-sale", default=False, help="Only show discounted items"),
        click.option("--min-discount", type=click.IntRange(min=0, max=100), default=0, help="Minimum discount percentage (e.g. 70)"),
        click.option("--deep/--no-deep", default=False, help="Scan with 3 sort orders for better coverage (3x API calls)"),
        click.option("--language", default="", help="Language filter (e.g. english)"),
        click.option("--all-languages/--no-all-languages", default=False, help="Include all languages (default: locale language only)"),
        click.option("--first-in-series/--no-first-in-series", default=False, help="Show only the first book per series"),
        click.option("--skip-owned/--no-skip-owned", default=False, help="Exclude books already in your library"),
        click.option("--exclude-seen", is_flag=True, default=False, help="Exclude ASINs from last search/find results"),
        click.option("--limit", "-n", type=click.IntRange(min=0), default=25, help="Show only the top N results (0 for unlimited, default: 25)"),
        click.option("--output", "-o", type=click.Path(path_type=Path), default=None, help="Export results to file (.json or .csv)"),
        click.option("--json", "json_flag", is_flag=True, default=False, help="Output results as JSON to stdout"),
        click.option("--quiet", "-q", is_flag=True, default=False, help="Suppress table output (useful with --output)"),
        click.option("--show-url", is_flag=True, default=False, help="Show Audible URL for each item in the table"),
        click.option("--interactive/--no-interactive", "-i", default=False, help="Browse results interactively"),
        click.option("--profile", "profile_name", default=None, help="Load a saved search profile (overrides defaults, CLI flags take precedence)"),
        click.option("--dry-run", is_flag=True, default=False, help="Show what would be scanned without making API calls"),
    ]
    for option in reversed(options):
        func = option(func)
    return func


def _build_scan_namespace(
    ctx: click.Context,
    profile_name: str | None,
    **kwargs,
) -> dict:
    """Build a resolved namespace dict from command kwargs + config/profile defaults."""
    ns = _resolve_scan_settings(ctx, profile_name, dict(kwargs))
    if ns.get("output") and ctx.get_parameter_source("quiet") != _CL:
        ns["quiet"] = True
    if ns.get("json_flag"):
        console.file = sys.stderr
    if not ns.get("language") and not ns.get("all_languages"):
        ns["language"] = LOCALE_LANGUAGES.get(ctx.obj["locale"], "")
    return ns


@cli.command()
@click.argument("query", required=False, default="")
@click.option("--max-price", type=click.FloatRange(min=0), default=None, help="Max price filter (e.g. 5.00)")
@click.option("--category", default="", help="Category ID to search within")
@click.option("--genre", default="", help="Genre name to search within (fuzzy match, e.g. 'sci-fi')")
@click.option("--sort", type=click.Choice(list(SORT_OPTIONS.keys()) + sorted(CLIENT_SORT_OPTIONS)), default="relevance", help="Sort order (price/discount/price-per-hour/value are client-side)")
@click.option("--min-ratings", type=int, default=0, help="Minimum number of ratings (e.g. 100)")
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours")
@click.option("--pages", type=click.IntRange(min=1), default=3, help="Number of pages to scan (50 items/page)")
@_common_filter_options
@click.pass_context
def search(ctx, query, max_price, max_pph, category, genre, exclude_genre, sort, min_rating, min_ratings, min_hours, narrator, author, series, publisher, exclude_authors, exclude_narrators, on_sale, min_discount, deep, pages, language, all_languages, first_in_series, skip_owned, exclude_seen, limit, output, json_flag, quiet, show_url, interactive, profile_name, dry_run):
    """Search the Audible catalog by keyword."""
    if not query and not genre and not category:
        raise click.UsageError("Provide a QUERY or use --genre / --category to browse.")
    ns = _build_scan_namespace(
        ctx, profile_name,
        max_price=max_price, max_pph=max_pph, sort=sort, min_rating=min_rating,
        min_ratings=min_ratings, min_hours=min_hours, min_discount=min_discount,
        language=language, narrator=narrator, author=author,
        pages=pages, limit=limit,
        on_sale=on_sale, deep=deep, first_in_series=first_in_series,
        all_languages=all_languages, skip_owned=skip_owned, interactive=interactive,
        genre=genre, exclude_genre=exclude_genre, exclude_authors=exclude_authors,
        exclude_narrators=exclude_narrators, keywords="", series=series,
        publisher=publisher, output=output, json_flag=json_flag, quiet=quiet,
    )
    (max_price, sort, min_rating, min_ratings, min_hours,
     language, narrator, author, pages, limit, on_sale, deep, first_in_series,
     skip_owned, interactive, genre, exclude_genre, exclude_authors,
     exclude_narrators, series, publisher, quiet, max_pph, min_discount) = (
        ns["max_price"], ns["sort"], ns["min_rating"], ns["min_ratings"],
        ns["min_hours"], ns["language"], ns["narrator"], ns["author"],
        ns["pages"], ns["limit"], ns["on_sale"], ns["deep"],
        ns["first_in_series"], ns["skip_owned"], ns["interactive"],
        ns["genre"], ns["exclude_genre"], ns["exclude_authors"], ns["exclude_narrators"],
        ns["series"], ns["publisher"], ns["quiet"], ns["max_pph"], ns["min_discount"],
    )
    if genre and category:
        raise click.UsageError("Use --genre or --category, not both.")

    dc = _get_client(ctx.obj["locale"])
    server_sort = SORT_OPTIONS.get(sort, "Relevance")
    sort_orders = DEEP_SORT_ORDERS if deep else [server_sort]
    skip_asins: set[str] | None = None

    with dc:
        category, category_name, exclude_category_ids = _resolve_categories(
            dc, genre, category, exclude_genre
        )

        if dry_run:
            _print_dry_run_summary(category_name=category_name, query=query, sort_orders=sort_orders, pages=pages)
            return

        if skip_owned:
            skip_asins = dc.get_library_asins()
        skip_asins = merge_seen_asins(skip_asins, exclude_seen)

        queries = [q.strip() for q in query.split("|") if q.strip()] if "|" in query else [query]
        if not queries:
            raise click.UsageError("No keywords found after splitting on '|'.")

        if len(queries) > 1:
            all_products: list[Product] = []
            fetched_asins: set[str] = set()
            for q in queries:
                sub_products = _fetch_with_progress(
                    dc,
                    keywords=q,
                    category_id=category,
                    sort_orders=sort_orders,
                    pages=pages,
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
                category_id=category,
                sort_orders=sort_orders,
                pages=pages,
                description=f"Searching {scope}",
            )

    cur = LOCALE_CURRENCY.get(ctx.obj["locale"], "$")
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
    _postprocess_and_output(
        all_products,
        title=search_title,
        max_price=max_price, min_rating=min_rating, min_ratings=min_ratings,
        min_hours=min_hours, narrator=narrator, author=author, exclude_authors=exclude_authors,
        exclude_narrators=exclude_narrators,
        language=language, on_sale=on_sale, skip_asins=skip_asins,
        exclude_category_ids=exclude_category_ids,
        first_in_series=first_in_series, sort=sort, limit=limit,
        output=output, json_flag=json_flag, quiet=quiet,
        currency=cur, interactive=interactive, show_url=show_url, max_pph=max_pph,
        min_discount=min_discount, series=series, publisher=publisher,
    )
    display_query = queries[0] if len(queries) == 1 else None
    if display_query and not author and not json_flag and not quiet and looks_like_person_name(display_query):
        console.print(f"\n  [dim]Tip: Use --author '{display_query}' for exact author filtering.[/dim]")


def _print_dry_run_summary(
    *,
    category_name: str,
    query: str,
    sort_orders: list[str],
    pages: int,
) -> None:
    """Print a dry-run scan summary."""
    sort_label = ", ".join(sort_orders)
    console.print(f"\n[bold]Dry run[/bold] — would scan:")
    if category_name:
        console.print(f"  Category: {category_name}")
    if query:
        console.print(f"  Query: {query}")
    console.print(f"  Sort orders: {sort_label}")
    console.print(f"  Pages per sort: {pages}")
    console.print(f"  Max items: ~{pages * len(sort_orders) * MAX_PAGE_SIZE}")
    console.print(f"  API calls: {pages * len(sort_orders)}")


def _fetch_with_progress(
    dc: DealsClient,
    *,
    keywords: str,
    category_id: str,
    sort_orders: list[str],
    pages: int,
    description: str,
) -> list[Product]:
    """Fetch products across one or more sort orders with a progress bar.

    Deduplicates by ASIN across sort orders. Returns a flat list.
    """
    all_products: list[Product] = []
    seen_asins: set[str] = set()
    total_pages = pages * len(sort_orders)

    with create_scan_progress() as progress:
        task = progress.add_task(description, total=total_pages, items=0)
        pages_done = 0

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
                    remaining_sorts = len(sort_orders) - sort_idx - 1
                    total_pages = (pages_done - 1) + actual + remaining_sorts * pages
                    progress.update(task, total=total_pages)

                progress.update(task, completed=pages_done, items=len(all_products))

    return all_products


@cli.command()
@click.option("--category", default="", help="Category ID (use 'deals categories' to find IDs)")
@click.option("--genre", default="", help="Genre name (fuzzy match, e.g. 'sci-fi', 'mystery', 'romance')")
@click.option("--keywords", default="", help="Optional keyword filter within the category")
@click.option("--max-price", type=click.FloatRange(min=0), default=5.00, help="Max price threshold (default: $5.00)")
@click.option("--sort", type=click.Choice(sorted(CLIENT_SORT_OPTIONS) + list(SORT_OPTIONS.keys())), default="price-per-hour", help="Sort order (price/discount/price-per-hour/value are client-side)")
@click.option("--min-ratings", type=int, default=1, help="Minimum number of ratings (default: 1, filters unreviewed)")
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours (filters out shorts)")
@click.option("--pages", type=click.IntRange(min=1), default=10, help="Pages to scan per sort order (50 items/page, default: 10)")
@_common_filter_options
@click.pass_context
def find(ctx, category, genre, exclude_genre, keywords, max_price, max_pph, sort, min_rating, min_ratings, min_hours, narrator, author, series, publisher, exclude_authors, exclude_narrators, on_sale, min_discount, deep, pages, language, all_languages, first_in_series, skip_owned, exclude_seen, limit, output, json_flag, quiet, show_url, profile_name, interactive, dry_run):
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
    """
    ns = _build_scan_namespace(
        ctx, profile_name,
        max_price=max_price, max_pph=max_pph, sort=sort, min_rating=min_rating,
        min_ratings=min_ratings, min_hours=min_hours, min_discount=min_discount,
        language=language, narrator=narrator, author=author,
        pages=pages, limit=limit,
        on_sale=on_sale, deep=deep, first_in_series=first_in_series,
        all_languages=all_languages, skip_owned=skip_owned, interactive=interactive,
        genre=genre, exclude_genre=exclude_genre, exclude_authors=exclude_authors,
        exclude_narrators=exclude_narrators, keywords=keywords, series=series,
        publisher=publisher, output=output, json_flag=json_flag, quiet=quiet,
    )
    (max_price, sort, min_rating, min_ratings, min_hours,
     language, narrator, author, pages, limit, on_sale, deep, first_in_series,
     skip_owned, interactive, genre, exclude_genre, exclude_authors,
     exclude_narrators, keywords, series, publisher, quiet, max_pph, min_discount) = (
        ns["max_price"], ns["sort"], ns["min_rating"], ns["min_ratings"],
        ns["min_hours"], ns["language"], ns["narrator"], ns["author"],
        ns["pages"], ns["limit"], ns["on_sale"], ns["deep"],
        ns["first_in_series"], ns["skip_owned"], ns["interactive"],
        ns["genre"], ns["exclude_genre"], ns["exclude_authors"], ns["exclude_narrators"],
        ns["keywords"], ns["series"], ns["publisher"], ns["quiet"], ns["max_pph"], ns["min_discount"],
    )
    if genre and category:
        raise click.UsageError("Use --genre or --category, not both.")

    dc = _get_client(ctx.obj["locale"])
    server_sort = SORT_OPTIONS.get(sort, "BestSellers")
    skip_asins: set[str] | None = None

    sort_orders = DEEP_SORT_ORDERS if deep else [server_sort]

    with dc:
        category, category_name, exclude_category_ids = _resolve_categories(
            dc, genre, category, exclude_genre
        )

        if dry_run:
            _print_dry_run_summary(category_name=category_name, query=keywords, sort_orders=sort_orders, pages=pages)
            return

        if skip_owned:
            skip_asins = dc.get_library_asins()
        skip_asins = merge_seen_asins(skip_asins, exclude_seen)

        desc_parts = []
        if keywords:
            desc_parts.append(f'"{keywords}"')
        if category:
            desc_parts.append(category_name or category)
        if not desc_parts:
            desc_parts.append("entire catalog")
        desc_str = ", ".join(desc_parts)

        all_products = _fetch_with_progress(
            dc,
            keywords=keywords,
            category_id=category,
            sort_orders=sort_orders,
            pages=pages,
            description=f"Scanning {desc_str}",
        )

    cur = LOCALE_CURRENCY.get(ctx.obj["locale"], "$")
    find_title = f"Deals under {cur}{max_price:.2f}"
    if category_name:
        find_title += f" in {category_name}"
    if keywords:
        find_title += f' matching "{keywords}"'
    _postprocess_and_output(
        all_products,
        title=find_title,
        max_price=max_price, min_rating=min_rating, min_ratings=min_ratings,
        min_hours=min_hours, narrator=narrator, author=author, exclude_authors=exclude_authors,
        exclude_narrators=exclude_narrators,
        language=language, on_sale=on_sale, skip_asins=skip_asins,
        exclude_category_ids=exclude_category_ids,
        first_in_series=first_in_series, sort=sort, limit=limit,
        output=output, json_flag=json_flag, quiet=quiet,
        currency=cur, interactive=interactive, show_url=show_url, max_pph=max_pph,
        min_discount=min_discount, series=series, publisher=publisher,
    )


@cli.command()
@click.option("--sort", type=click.Choice(["title", "rating", "length", "date", "price", "-price", "price-per-hour"]), default="date", help="Sort order (default: date — newest first)")
@click.option("-n", "--limit", type=click.IntRange(min=0), default=None, help="Show only the top N results")
@click.option("-o", "--output", type=click.Path(path_type=Path), default=None, help="Export to file (.json or .csv)")
@click.option("--json", "json_flag", is_flag=True, default=False, help="Output as JSON to stdout")
@click.option("-q", "--quiet", is_flag=True, default=False, help="Suppress table output")
@click.option("--author", default="", help="Filter by author name (substring match)")
@click.option("--narrator", default="", help="Filter by narrator name (substring match, client-side)")
@click.option("--genre", default="", help="Filter by genre/category (substring match on categories)")
@click.option("--min-rating", type=float, default=0.0, help="Minimum rating")
@click.option("--min-ratings", type=int, default=0, help="Minimum number of ratings")
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours")
@click.pass_context
def library(ctx, sort, limit, output, json_flag, quiet, author, narrator, genre, min_rating, min_ratings, min_hours):
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
    if output and ctx.get_parameter_source("quiet") != _CL:
        quiet = True
    if json_flag:
        console.file = sys.stderr

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
    total_before_limit = len(filtered)
    if limit is not None and limit > 0:
        filtered = filtered[:limit]

    cur = LOCALE_CURRENCY.get(ctx.obj["locale"], "$")

    if output:
        export_products(filtered, output)
        console.print(f"[green]Exported {len(filtered)} items to {output}[/green]")
    if json_flag:
        serialized = [serialize_product(p) for p in filtered]
        click.echo(json_mod.dumps(serialized, indent=2, ensure_ascii=False))
    if not json_flag and not quiet:
        console.print()
        title = "Your Library"
        display_products(filtered, title=title, currency=cur)
        if filter_breakdown:
            display_summary(len(filtered), filter_breakdown, currency=cur,
                            total_before_limit=total_before_limit, noun="books")
        elif total_before_limit > len(filtered):
            console.print(f"  [bold]{len(filtered)}[/bold] of {total_before_limit} books shown")
        else:
            console.print(f"  [bold]{len(filtered)}[/bold] books in library")


@cli.command()
@click.option("--min-books", type=click.IntRange(min=1), default=2, help="Minimum books owned in a series to consider it 'invested' (default: 2)")
@click.option("--max-series", type=click.IntRange(min=1), default=20, help="Maximum number of series to scan (default: 20, most-invested first)")
@click.option("--series", "series_filter", default="", help="Filter to a specific series name (substring match)")
@click.option("--max-price", type=click.FloatRange(min=0), default=None, help="Max price filter")
@click.option("--min-rating", type=float, default=0.0, help="Minimum rating (e.g. 4.0)")
@click.option("--min-ratings", type=int, default=0, help="Minimum number of ratings")
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours")
@click.option("--on-sale", is_flag=True, default=False, help="Only show discounted items")
@click.option("--sort", type=click.Choice(["price", "-price", "discount", "price-per-hour", "rating", "length", "date", "title"]), default="price-per-hour", help="Sort order (default: price-per-hour)")
@click.option("--limit", "-n", type=click.IntRange(min=0), default=25, help="Show only the top N results (0 for unlimited, default: 25)")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None, help="Export results to file (.json or .csv)")
@click.option("--json", "json_flag", is_flag=True, default=False, help="Output results as JSON to stdout")
@click.option("--quiet", "-q", is_flag=True, default=False, help="Suppress table output (useful with --output)")
@click.option("--interactive", "-i", is_flag=True, default=False, help="Browse results interactively")
@click.option("--pages", type=click.IntRange(min=1), default=3, help="Pages to scan per series search (default: 3)")
@click.pass_context
def series(ctx, min_books, max_series, series_filter, max_price, min_rating, min_ratings, min_hours, on_sale, sort, limit, output, json_flag, quiet, interactive, pages):
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
    if output and ctx.get_parameter_source("quiet") != _CL:
        quiet = True
    if json_flag:
        console.file = sys.stderr

    ns = _resolve_scan_settings(ctx, None, dict(
        max_price=max_price, min_rating=min_rating, min_ratings=min_ratings,
        min_hours=min_hours, on_sale=on_sale, limit=limit, sort=sort, pages=pages,
    ))
    max_price, min_rating, min_ratings = ns["max_price"], ns["min_rating"], ns["min_ratings"]
    min_hours, on_sale, limit = ns["min_hours"], ns["on_sale"], ns["limit"]
    sort, pages = ns["sort"], ns["pages"]

    dc = _get_client(ctx.obj["locale"])
    cur = LOCALE_CURRENCY.get(ctx.obj["locale"], "$")

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
            name: books
            for name, books in series_map.items()
            if len(books) >= min_books
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
                console.print(f"[dim]No invested series matching '{series_filter}' "
                              f"(need {min_books}+ owned books).[/dim]")
            else:
                console.print(f"[dim]No series with {min_books}+ owned books found.[/dim]")
            return

        # Sort by most-invested (most owned books) first, then limit
        invested_sorted = sorted(invested.items(), key=lambda x: len(x[1]), reverse=True)
        if len(invested_sorted) > max_series:
            if not quiet and not json_flag:
                console.print(f"[dim]Found {len(invested_sorted)} invested series, scanning top {max_series} (use --max-series to adjust).[/dim]")
            invested_sorted = invested_sorted[:max_series]
        elif not quiet and not json_flag:
            console.print(f"[dim]Found {len(invested_sorted)} invested series. Searching for continuation books...[/dim]")

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
                series_asin = next((ob.series_asin for ob in owned_books if ob.series_asin), "")

                if series_asin:
                    # Direct lookup via series ASIN
                    series_products = dc.get_series_products(series_asin)
                else:
                    # Fallback: keyword search when no series ASIN available
                    series_products = []
                    author_hint = next((ob.authors[0] for ob in owned_books if ob.authors), "")
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

                progress.update(task, completed=series_idx + 1, items=len(all_candidates))

                # Rate limit between series lookups
                if series_idx < len(invested_sorted) - 1:
                    time.sleep(0.3)

    # 4. Post-process using shared pipeline
    _postprocess_and_output(
        all_candidates,
        title=f"Series Continuation Books ({len(invested_sorted)} series)",
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
        skip_asins=None,  # already filtered above
        exclude_category_ids=set(),
        first_in_series=False,
        sort=sort,
        limit=limit,
        output=output,
        json_flag=json_flag,
        quiet=quiet,
        currency=cur,
        interactive=interactive,
    )


@cli.command("last")
@click.option("--sort", type=click.Choice(["price", "-price", "discount", "price-per-hour", "value", "rating", "length", "date", "relevance"]), default=None, help="Re-sort results")
@click.option("--max-price", type=click.FloatRange(min=0), default=None, help="Max price filter")
@click.option("--max-price-per-hour", "max_pph", type=click.FloatRange(min=0), default=None, help="Max price per hour (e.g. 0.50)")
@click.option("--min-rating", type=float, default=0.0, help="Minimum rating")
@click.option("--min-ratings", type=int, default=0, help="Minimum number of ratings")
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours")
@click.option("--narrator", default="", help="Filter by narrator name (substring match, client-side)")
@click.option("--author", default="", help="Filter by author name (substring match)")
@click.option("--series", default="", help="Filter by series name (substring match)")
@click.option("--publisher", default="", help="Filter by publisher name (substring match)")
@click.option("--exclude-author", "exclude_authors", multiple=True, help="Exclude author (substring match, repeatable)")
@click.option("--exclude-narrator", "exclude_narrators", multiple=True, help="Exclude narrator (substring match, repeatable)")
@click.option("--language", default="", help="Language filter")
@click.option("--on-sale", is_flag=True, default=False, help="Only show discounted items")
@click.option("--min-discount", type=click.IntRange(min=0, max=100), default=0, help="Minimum discount percentage (e.g. 70)")
@click.option("--first-in-series", is_flag=True, default=False, help="Show only first book per series")
@click.option("--limit", "-n", type=click.IntRange(min=0), default=None, help="Show only the top N results")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None, help="Export results to file (.json or .csv)")
@click.option("--json", "json_flag", is_flag=True, default=False, help="Output results as JSON to stdout")
@click.option("--quiet", "-q", is_flag=True, default=False, help="Suppress table output")
@click.option("--show-url", is_flag=True, default=False, help="Show Audible URL for each item in the table")
@click.option("--interactive", "-i", is_flag=True, default=False, help="Browse results interactively")
@click.option("--clear", is_flag=True, default=False, help="Delete the cached results and exit")
@click.option("--clear-seen", is_flag=True, default=False, help="Clear the cumulative seen-ASINs list and exit")
@click.option("--count", "count_only", is_flag=True, default=False, help="Show total cached result count (ignores filters)")
@click.pass_context
def last_cmd(ctx, sort, max_price, max_pph, min_rating, min_ratings, min_hours, narrator, author, series, publisher, exclude_authors, exclude_narrators, language, on_sale, min_discount, first_in_series, limit, output, json_flag, quiet, show_url, interactive, clear, clear_seen, count_only):
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
    if output and ctx.get_parameter_source("quiet") != _CL:
        quiet = True
    cached_title, data = load_last_results()
    products = [p for d in data if (p := deserialize_product(d)) is not None]
    if json_flag:
        console.file = sys.stderr

    effective_sort = sort or ""  # preserve original cache order when no --sort given
    cur = LOCALE_CURRENCY.get(ctx.obj["locale"], "$")
    _postprocess_and_output(
        products,
        title=cached_title,
        max_price=max_price, min_rating=min_rating, min_ratings=min_ratings,
        min_hours=min_hours, narrator=narrator, author=author, exclude_authors=exclude_authors,
        exclude_narrators=exclude_narrators,
        language=language, on_sale=on_sale, skip_asins=None,
        exclude_category_ids=set(),
        first_in_series=first_in_series, sort=effective_sort, limit=limit,
        output=output, json_flag=json_flag, quiet=quiet,
        currency=cur, interactive=interactive, write_cache=False,
        show_url=show_url, max_pph=max_pph, min_discount=min_discount, series=series,
        publisher=publisher,
    )


@cli.command()
@click.argument("asin", required=False, default=None)
@click.option("--last", "last_ref", type=int, default=None, help="Use result #N from last search/find")
@click.pass_context
def detail(ctx, asin, last_ref):
    """Show detailed info for a product by ASIN."""
    if last_ref is not None:
        resolved = resolve_last_references((last_ref,))
        asin, desc = resolved[0]
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
@click.option("--last", "last_ref", type=int, default=None, help="Use result #N from last search/find")
@click.pass_context
def open_cmd(ctx, asin, last_ref):
    """Open an audiobook's Audible page in your browser."""
    if last_ref is not None:
        resolved = resolve_last_references((last_ref,))
        asin, desc = resolved[0]
        console.print(f"[dim]{desc}[/dim]")
    if not asin:
        raise click.UsageError("Provide an ASIN or use --last N.")
    validate_asin(asin)
    domain = LOCALE_DOMAIN.get(ctx.obj["locale"], "www.audible.com")
    url = f"https://{domain}/pd/{asin}"
    console.print(f"[dim]Opening {url}[/dim]")
    click.launch(url)


@cli.command()
@click.argument("asins", nargs=-1, required=False)
@click.option("--last", "last_refs", type=int, multiple=True, help="Use result #N from last search/find (repeatable)")
@click.pass_context
def compare(ctx, asins, last_refs):
    """Compare multiple products side-by-side.

    \b
    Example:
        deals compare B00R6S1RCY B00I2VWW5U B019NMZ6FE
        deals compare --last 1 --last 3
    """
    all_asins = list(asins)
    if last_refs:
        resolved = resolve_last_references(last_refs)
        for ref_asin, desc in resolved:
            console.print(f"[dim]{desc}[/dim]")
            all_asins.append(ref_asin)

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
@click.option("--max-price", type=float, default=None, help="Alert when price drops below this")
@click.option("--last", "last_refs", type=int, multiple=True, help="Use result #N from last search/find (repeatable)")
@click.pass_context
def wishlist_add(ctx, asins, max_price, last_refs):
    """Add ASINs to your wishlist.

    \b
    Example:
        deals wishlist add B00R6S1RCY B00I2VWW5U --max-price 5
        deals wishlist add --last 1 --last 2 --max-price 5
    """
    all_asins = list(asins)
    if last_refs:
        resolved = resolve_last_references(last_refs)
        for ref_asin, desc in resolved:
            console.print(f"[dim]{desc}[/dim]")
            all_asins.append(ref_asin)
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
@click.option("--last", "last_refs", type=int, multiple=True, help="Use result #N from last search/find (repeatable)")
def wishlist_remove(asins, last_refs):
    """Remove ASINs from your wishlist."""
    all_asins = list(asins)
    if last_refs:
        resolved = resolve_last_references(last_refs)
        for ref_asin, desc in resolved:
            console.print(f"[dim]{desc}[/dim]")
            all_asins.append(ref_asin)
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
    cur = LOCALE_CURRENCY.get(ctx.obj["locale"], "$")
    items = load_wishlist()
    if not items:
        console.print("[dim]Wishlist is empty. Use 'deals wishlist add ASIN' to add items.[/dim]")
        return

    table = Table(title="Wishlist", show_lines=False, padding=(0, 1), title_style="bold")
    table.add_column("ASIN", style="cyan", width=14)
    table.add_column("Title", max_width=40)
    table.add_column("Target", justify="right", width=10)

    for item in items:
        target = f"{cur}{item['max_price']:.2f}" if item.get("max_price") else "-"
        table.add_row(item["asin"], item["title"], target)

    console.print(table)


@wishlist.command("sync")
@click.option("--max-price", type=float, default=None, help="Set target price for all synced items")
@click.option("--update", is_flag=True, default=False, help="Update target price for existing items too")
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
    cur = LOCALE_CURRENCY.get(ctx.obj["locale"], "$")

    added = 0
    skipped = 0
    updated = 0
    for product in audible_items:
        if product.asin in local_by_asin:
            if update:
                local_by_asin[product.asin]["max_price"] = max_price
                updated += 1
                console.print(f"[yellow]~[/yellow] {product.title} ({product.asin}) → target {cur}{max_price:.2f}")
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




def _watch_once(ctx: click.Context, buy_only: bool = False, sort_by: str | None = None, show_url: bool = False) -> int:
    """Run a single wishlist price check. Returns the number of BUY hits."""
    items = load_wishlist()
    if not items:
        console.print("[dim]Wishlist is empty. Use 'deals wishlist add ASIN' to add items.[/dim]")
        return 0

    dc = _get_client(ctx.obj["locale"])
    targets: dict[str, float | None] = {item["asin"]: item.get("max_price") for item in items}

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

    cur = LOCALE_CURRENCY.get(ctx.obj["locale"], "$")
    return display_watch_table(products, targets, cur, buy_only, show_url)


@cli.command()
@click.option("--every", default=None, help="Re-check on an interval (e.g. '30m', '2h', '1h30m'). Runs until interrupted.")
@click.option("--buy-only", is_flag=True, default=False, help="Only show items at or below target price")
@click.option("--sort", "sort_by", type=click.Choice(["title", "author", "price", "asin", "release-date"], case_sensitive=False), default=None, help="Sort results by field")
@click.option("--show-url", is_flag=True, default=False, help="Show Audible URL for each item")
@click.pass_context
def watch(ctx, every, buy_only, sort_by, show_url):
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
    if not every:
        _watch_once(ctx, buy_only=buy_only, sort_by=sort_by, show_url=show_url)
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
        console.print("[dim]No global defaults set. Use 'deals config set KEY VALUE' to set one.[/dim]")
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
@click.option("--max-price-per-hour", "max_pph", type=click.FloatRange(min=0), default=None)
@click.option("--publisher", default="")
@click.option("--deep/--no-deep", default=False)
@click.option("--pages", type=int, default=None)
@click.option("--first-in-series/--no-first-in-series", default=False)
@click.option("--all-languages/--no-all-languages", default=False)
@click.option("--limit", "-n", type=click.IntRange(min=0), default=None)
@click.option("--skip-owned/--no-skip-owned", default=False)
@click.option("--language", default="")
@click.option("--interactive/--no-interactive", "-i", default=False)
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
        console.print("[dim]No profiles saved. Use 'deals profile save NAME --flags...' to create one.[/dim]")
        return

    for name, opts in profiles.items():
        flags = " ".join(_opts_to_flag_parts(opts))
        console.print(f"  [bold]{name}[/bold]  [dim]{flags}[/dim]")


@profile.command("delete")
@click.argument("name")
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
@click.argument("name")
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
@click.option("--last", "last_ref", type=int, default=None, help="Use result #N from last search/find")
@click.pass_context
def history(ctx, asin, last_ref):
    """Show price history for an ASIN.

    History is recorded automatically each time an ASIN appears in
    search/find results. Use 'deals history ASIN' to view past prices.
    """
    if last_ref is not None:
        resolved = resolve_last_references((last_ref,))
        asin, desc = resolved[0]
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

    cur = LOCALE_CURRENCY.get(ctx.obj["locale"], "$")
    display_price_history(entries, asin, cur)


@cli.command()
@click.option("--days", type=click.IntRange(min=1), default=7, help="Look back this many days (default: 7)")
@click.option("--show-new", is_flag=True, default=False, help="Include newly tracked item details (only count shown by default)")
@click.pass_context
def recap(ctx, days, show_new):
    """Show a recap of price changes across tracked items.

    Scans price history files and reports items that dropped in price,
    new items tracked, and wishlist items at target.
    """
    cur = LOCALE_CURRENCY.get(ctx.obj["locale"], "$")
    drops, new_items = scan_price_changes(days)
    if not drops and not new_items and not has_price_history():
        console.print("[dim]No price history yet. Run 'deals find' or 'deals search' to start tracking.[/dim]")
        return
    wishlist_hits = find_wishlist_hits()
    display_recap(drops, new_items, wishlist_hits, days, cur, show_new)


@cli.command()
@click.option("--webhook", default=None, help="Webhook URL to POST results to")
@click.pass_context
def notify(ctx, webhook):
    """Check wishlist and send notifications for items at target price.

    \b
    Examples:
        deals notify --webhook https://hooks.slack.com/services/...
        deals notify  (prints to stdout as JSON, useful for cron + mail)
    """
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
    hits = []
    for p in products:
        target = targets.get(p.asin)
        if target is not None and p.price is not None and p.price <= target:
            hits.append({
                "asin": p.asin,
                "title": p.title,
                "price": round(p.price, 2),
                "target": target,
                "url": p.url,
            })

    if not hits:
        if not webhook:
            click.echo(json_mod.dumps({"deals": [], "count": 0}, indent=2))
        else:
            console.print("[dim]No items at target price. Nothing sent to webhook.[/dim]")
        return

    payload = json_mod.dumps({"deals": hits, "count": len(hits)}, indent=2)

    if webhook:
        req = urllib.request.Request(
            webhook,
            data=payload.encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            console.print(f"[green]Sent {len(hits)} deal(s) to webhook[/green]")
        except Exception as e:
            raise click.ClickException(f"Webhook failed: {e}")
    else:
        click.echo(payload)


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
