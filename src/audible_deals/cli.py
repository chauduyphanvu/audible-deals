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
    deals wishlist add/remove/list Manage your watchlist
    deals watch                    Check wishlist for price drops
    deals notify [--webhook URL]   Send notifications for deals at target
    deals profile save/list/delete Manage saved search profiles
    deals history ASIN             View price history with sparkline
    deals recap [--days N]         Recap of recent price changes
    deals completions SHELL        Generate shell completions
"""

from __future__ import annotations

import csv
import datetime
import ipaddress
import json as json_mod
import math
import os
import re
try:
    import readline  # noqa: F401 — required on macOS for input() with long strings
except ImportError:
    pass  # unavailable on Windows
import socket
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import asdict
from pathlib import Path

import click
from rich.table import Table

from audible_deals.client import AUTH_FILE, CONFIG_DIR, LOCALE_CURRENCY, DealsClient, Product
from audible_deals.display import (
    console,
    create_scan_progress,
    display_categories,
    display_comparison,
    display_product_detail,
    display_products,
    display_summary,
)

_ASIN_RE = re.compile(r"^[A-Za-z0-9]{2,14}$")


def _validate_asin(asin: str) -> None:
    """Validate that an ASIN is alphanumeric and won't cause path traversal."""
    if not _ASIN_RE.fullmatch(asin):
        raise click.BadParameter(f"Invalid ASIN format: {asin!r}")


def _validate_webhook_url(url: str) -> None:
    """Validate webhook URL: must be http(s) and must not resolve to private IPs."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise click.BadParameter(
            f"Webhook URL must use http:// or https://, got {parsed.scheme!r}",
            param_hint="'--webhook'",
        )
    hostname = parsed.hostname
    if not hostname:
        raise click.BadParameter(
            "Webhook URL must include a host",
            param_hint="'--webhook'",
        )
    try:
        addrinfos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise click.BadParameter(
            f"Cannot resolve webhook host {hostname!r}: {e}",
            param_hint="'--webhook'",
        )
    for _family, _type, _proto, _canonname, sockaddr in addrinfos:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise click.BadParameter(
                f"Webhook URL resolves to non-public address {ip}",
                param_hint="'--webhook'",
            )


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


# Sort orders used by --deep to maximize item coverage
DEEP_SORT_ORDERS = ["BestSellers", "-ReleaseDate", "AvgRating"]

# Default language per marketplace locale
LOCALE_LANGUAGES: dict[str, str] = {
    "us": "english", "uk": "english", "ca": "english",
    "au": "english", "in": "english", "de": "german",
    "fr": "french", "jp": "japanese", "es": "spanish",
}

# Server-side sort values accepted by Audible's catalog API
SORT_OPTIONS = {
    "rating": "AvgRating",
    "bestsellers": "BestSellers",
    "length": "-RuntimeLength",
    "date": "-ReleaseDate",
    "relevance": "Relevance",
    "title": "Title",
}


def _get_client(locale: str) -> DealsClient:
    return DealsClient(locale=locale)


def _filter_products(
    products: list[Product],
    *,
    max_price: float | None = None,
    min_rating: float = 0.0,
    min_ratings: int = 0,
    min_hours: float = 0.0,
    language: str = "",
    narrator: str = "",
    on_sale: bool = False,
    skip_asins: set[str] | None = None,
    exclude_category_ids: set[str] | None = None,
) -> tuple[list[Product], int]:
    """Apply client-side filters. Returns (filtered, num_excluded)."""
    original = len(products)
    filtered = products

    if skip_asins:
        filtered = [p for p in filtered if p.asin not in skip_asins]

    if max_price is not None:
        filtered = [p for p in filtered if p.price is not None and p.price <= max_price]

    if min_rating > 0:
        filtered = [p for p in filtered if p.rating >= min_rating]

    if min_ratings > 0:
        filtered = [p for p in filtered if p.num_ratings >= min_ratings]

    if min_hours > 0:
        filtered = [p for p in filtered if p.hours >= min_hours]

    if language:
        lang_lower = language.lower()
        filtered = [p for p in filtered if p.language.lower() == lang_lower]

    if narrator:
        narrator_lower = narrator.lower()
        filtered = [
            p for p in filtered
            if any(narrator_lower in n.lower() for n in p.narrators)
        ]

    if on_sale:
        filtered = [p for p in filtered if p.discount_pct is not None and p.discount_pct > 0]

    if exclude_category_ids:
        filtered = [
            p for p in filtered
            if not any(cid in exclude_category_ids for cid in p.category_ids)
        ]

    return filtered, original - len(filtered)


def _price_per_hour(p: Product) -> float:
    """Calculate price per hour of audio. Returns inf for missing data."""
    if p.price is None or p.hours <= 0:
        return float("inf")
    return p.price / p.hours


def _sort_local(products: list[Product], sort: str) -> list[Product]:
    """Re-sort locally when combining pages (server sort is per-page)."""
    if sort == "price":
        return sorted(products, key=lambda p: (p.price if p.price is not None else 9999))
    elif sort == "-price":
        return sorted(products, key=lambda p: (p.price if p.price is not None else 0), reverse=True)
    elif sort == "rating":
        return sorted(products, key=lambda p: p.rating, reverse=True)
    elif sort == "length":
        return sorted(products, key=lambda p: p.length_minutes, reverse=True)
    elif sort == "date":
        return sorted(products, key=lambda p: p.release_date or "", reverse=True)
    elif sort == "discount":
        return sorted(
            products,
            key=lambda p: p.discount_pct if p.discount_pct is not None else 0,
            reverse=True,
        )
    elif sort == "price-per-hour":
        return sorted(products, key=_price_per_hour)
    return products


def _dedupe_editions(products: list[Product]) -> tuple[list[Product], int]:
    """Remove duplicate editions of the same book (same series + position).

    Keeps the cheapest edition. Always-on — no flag needed.
    """
    best: dict[tuple[str, str], Product] = {}
    for p in products:
        if not p.series_name or not p.series_position:
            continue
        key = (p.series_name.lower(), p.series_position.lower())
        existing = best.get(key)
        if existing is None:
            best[key] = p
        else:
            p_price = p.price if p.price is not None else float("inf")
            e_price = existing.price if existing.price is not None else float("inf")
            if p_price < e_price:
                best[key] = p

    best_asins = {p.asin for p in best.values()}
    result = []
    removed = 0
    for p in products:
        if not p.series_name or not p.series_position:
            result.append(p)
        elif p.asin in best_asins:
            result.append(p)
            best_asins.discard(p.asin)  # only include first occurrence
        else:
            removed += 1
    return result, removed


def _first_in_series(products: list[Product]) -> tuple[list[Product], int]:
    """Keep only the lowest-position item per series.

    Non-series items pass through unchanged.
    """
    best: dict[str, Product] = {}
    for p in products:
        if not p.series_name:
            continue
        key = p.series_name.lower()
        try:
            pos = float(p.series_position) if p.series_position else float("inf")
        except ValueError:
            pos = float("inf")
        existing = best.get(key)
        if existing is None:
            best[key] = p
        else:
            try:
                existing_pos = float(existing.series_position) if existing.series_position else float("inf")
            except ValueError:
                existing_pos = float("inf")
            if pos < existing_pos:
                best[key] = p

    best_asins = {p.asin for p in best.values()}
    result = []
    collapsed = 0
    for p in products:
        if not p.series_name:
            result.append(p)
        elif p.asin in best_asins:
            result.append(p)
            best_asins.discard(p.asin)
        else:
            collapsed += 1
    return result, collapsed


def _serialize_product(p: Product) -> dict:
    """Convert a Product to a plain dict for export."""
    d = asdict(p)
    if d["price"] is not None:
        d["price"] = round(d["price"], 2)
    if d["list_price"] is not None:
        d["list_price"] = round(d["list_price"], 2)
    d["full_title"] = p.full_title
    d["hours"] = p.hours
    d["discount_pct"] = p.discount_pct
    pph = _price_per_hour(p)
    d["price_per_hour"] = round(pph, 2) if pph != float("inf") else None
    d["url"] = p.url
    return d


def _export_products(products: list[Product], path: Path) -> None:
    """Export products to file, detecting format from extension."""
    suffix = path.suffix.lower()
    rows = [_serialize_product(p) for p in products]

    if suffix == ".json":
        path.write_text(json_mod.dumps(rows, indent=2, ensure_ascii=False))
    elif suffix == ".csv":
        if not rows:
            path.write_text("")
            return
        for row in rows:
            for key in ("authors", "narrators", "categories", "category_ids"):
                if isinstance(row[key], list):
                    row[key] = "; ".join(str(v) for v in row[key])
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    else:
        raise click.BadParameter(
            f"Unsupported extension '{suffix}'. Use .json or .csv.",
            param_hint="--output",
        )


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
) -> None:
    """Shared post-processing pipeline for search and find commands."""
    filtered, excluded = _filter_products(
        all_products,
        max_price=max_price,
        min_rating=min_rating,
        min_ratings=min_ratings,
        min_hours=min_hours,
        narrator=narrator,
        language=language,
        on_sale=on_sale,
        skip_asins=skip_asins,
        exclude_category_ids=exclude_category_ids,
    )
    filtered, editions_removed = _dedupe_editions(filtered)
    series_collapsed = 0
    if first_in_series:
        filtered, series_collapsed = _first_in_series(filtered)
    filtered = _sort_local(filtered, sort)
    _record_prices(filtered)
    total_before_limit = len(filtered)
    if limit:
        filtered = filtered[:limit]

    if output:
        _export_products(filtered, output)
        console.print(f"[green]Exported {len(filtered)} items to {output}[/green]")
    if json_flag:
        click.echo(json_mod.dumps([_serialize_product(p) for p in filtered], indent=2, ensure_ascii=False))
    if not json_flag and not quiet:
        console.print()
        display_products(filtered, max_price=max_price, title=title)
        display_summary(len(filtered), excluded, max_price=max_price,
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
            items = _load_wishlist()
            if any(item["asin"] == p.asin for item in items):
                console.print(f"[dim]{p.asin} already on wishlist[/dim]")
            else:
                items.append({"asin": p.asin, "title": p.title, "max_price": None, "added": ""})
                _save_wishlist(items)
                console.print(f"[green]+[/green] {p.title} added to wishlist")


class _HandleAuthErrors(click.Group):
    """Catch RuntimeError from missing auth and show a friendly message."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except RuntimeError as e:
            if "Not authenticated" in str(e):
                raise click.ClickException(str(e))
            raise


@click.group(cls=_HandleAuthErrors)
@click.option("--locale", default="us", help="Audible marketplace (us, uk, ca, de, fr, au, jp, in, es)")
@click.pass_context
def cli(ctx, locale):
    """Audible deal finder - find cheap audiobooks during sales."""
    ctx.ensure_object(dict)
    ctx.obj["locale"] = locale


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


@cli.command()
@click.argument("query")
@click.option("--max-price", type=click.FloatRange(min=0), default=None, help="Max price filter (e.g. 5.00)")
@click.option("--category", default="", help="Category ID to search within")
@click.option("--genre", default="", help="Genre name to search within (fuzzy match, e.g. 'sci-fi')")
@click.option("--exclude-genre", multiple=True, help="Genre(s) to exclude (repeatable, fuzzy match)")
@click.option("--sort", type=click.Choice(list(SORT_OPTIONS.keys()) + ["price", "-price", "discount", "price-per-hour"]), default="relevance", help="Sort order (price/discount/price-per-hour are client-side)")
@click.option("--min-rating", type=float, default=0.0, help="Minimum rating (e.g. 4.0)")
@click.option("--min-ratings", type=int, default=0, help="Minimum number of ratings (e.g. 100)")
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours")
@click.option("--narrator", default="", help="Filter by narrator name (substring match)")
@click.option("--on-sale", is_flag=True, default=False, help="Only show discounted items")
@click.option("--pages", type=click.IntRange(min=1), default=3, help="Number of pages to scan (50 items/page)")
@click.option("--language", default="", help="Language filter (e.g. english)")
@click.option("--all-languages", is_flag=True, default=False, help="Include all languages (default: locale language only)")
@click.option("--first-in-series", is_flag=True, default=False, help="Show only the first book per series")
@click.option("--skip-owned", is_flag=True, default=False, help="Exclude books already in your library")
@click.option("--limit", "-n", type=int, default=None, help="Show only the top N results")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None, help="Export results to file (.json or .csv)")
@click.option("--json", "json_flag", is_flag=True, default=False, help="Output results as JSON to stdout")
@click.option("--quiet", "-q", is_flag=True, default=False, help="Suppress table output (useful with --output)")
@click.option("--interactive", "-i", is_flag=True, default=False, help="Browse results interactively")
@click.pass_context
def search(ctx, query, max_price, category, genre, exclude_genre, sort, min_rating, min_ratings, min_hours, narrator, on_sale, pages, language, all_languages, first_in_series, skip_owned, limit, output, json_flag, quiet, interactive):
    """Search the Audible catalog by keyword."""
    if genre and category:
        raise click.UsageError("Use --genre or --category, not both.")
    if json_flag:
        console.file = sys.stderr
    if not language and not all_languages:
        language = LOCALE_LANGUAGES.get(ctx.obj["locale"], "")

    dc = _get_client(ctx.obj["locale"])
    server_sort = SORT_OPTIONS.get(sort, "Relevance")
    all_products: list[Product] = []
    skip_asins: set[str] | None = None
    category_name = ""
    exclude_category_ids: set[str] = set()

    with dc:
        if skip_owned:
            skip_asins = dc.get_library_asins()
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

        scope = f"'{query}'"
        if category_name:
            scope += f" in {category_name}"

        with create_scan_progress() as progress:
            task = progress.add_task(f"Searching {scope}", total=pages, items=0)
            for products, page_num, total in dc.search_pages(
                keywords=query,
                category_id=category,
                sort_by=server_sort,
                max_pages=pages,
            ):
                all_products.extend(products)
                if page_num == 1:
                    actual = min(pages, math.ceil(total / 50)) if total else 1
                    progress.update(task, total=actual)
                progress.update(task, completed=page_num, items=len(all_products))

    cur = LOCALE_CURRENCY.get(ctx.obj["locale"], "$")
    _postprocess_and_output(
        all_products,
        title=f'Search: "{query}"',
        max_price=max_price, min_rating=min_rating, min_ratings=min_ratings,
        min_hours=min_hours, narrator=narrator,
        language=language, on_sale=on_sale, skip_asins=skip_asins,
        exclude_category_ids=exclude_category_ids,
        first_in_series=first_in_series, sort=sort, limit=limit,
        output=output, json_flag=json_flag, quiet=quiet,
        currency=cur, interactive=interactive,
    )


@cli.command()
@click.option("--category", default="", help="Category ID (use 'deals categories' to find IDs)")
@click.option("--genre", default="", help="Genre name (fuzzy match, e.g. 'sci-fi', 'mystery', 'romance')")
@click.option("--exclude-genre", multiple=True, help="Genre(s) to exclude (repeatable, fuzzy match)")
@click.option("--keywords", default="", help="Optional keyword filter within the category")
@click.option("--max-price", type=click.FloatRange(min=0), default=5.00, help="Max price threshold (default: $5.00)")
@click.option("--sort", type=click.Choice(["price", "-price", "discount", "price-per-hour"] + list(SORT_OPTIONS.keys())), default="price", help="Sort order (price/discount/price-per-hour are client-side)")
@click.option("--min-rating", type=float, default=0.0, help="Minimum rating (e.g. 4.0)")
@click.option("--min-ratings", type=int, default=0, help="Minimum number of ratings (e.g. 100)")
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours (filters out shorts)")
@click.option("--narrator", default="", help="Filter by narrator name (substring match)")
@click.option("--on-sale", is_flag=True, default=False, help="Only show discounted items")
@click.option("--deep", is_flag=True, default=False, help="Scan with 3 sort orders for better coverage (3x API calls)")
@click.option("--pages", type=click.IntRange(min=1), default=10, help="Pages to scan per sort order (50 items/page, default: 10)")
@click.option("--language", default="", help="Language filter (e.g. english)")
@click.option("--all-languages", is_flag=True, default=False, help="Include all languages (default: locale language only)")
@click.option("--first-in-series", is_flag=True, default=False, help="Show only the first book per series")
@click.option("--skip-owned", is_flag=True, default=False, help="Exclude books already in your library")
@click.option("--limit", "-n", type=int, default=None, help="Show only the top N results")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None, help="Export results to file (.json or .csv)")
@click.option("--json", "json_flag", is_flag=True, default=False, help="Output results as JSON to stdout")
@click.option("--quiet", "-q", is_flag=True, default=False, help="Suppress table output (useful with --output)")
@click.option("--profile", "profile_name", default=None, help="Load a saved search profile (overrides defaults, CLI flags take precedence)")
@click.option("--interactive", "-i", is_flag=True, default=False, help="Browse results interactively")
@click.pass_context
def find(ctx, category, genre, exclude_genre, keywords, max_price, sort, min_rating, min_ratings, min_hours, narrator, on_sale, deep, pages, language, all_languages, first_in_series, skip_owned, limit, output, json_flag, quiet, profile_name, interactive):
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
    """
    # Apply saved profile as defaults (CLI flags override)
    if profile_name:
        profiles = _load_profiles()
        if profile_name not in profiles:
            raise click.ClickException(f"Profile '{profile_name}' not found. Use 'deals profile list' to see available profiles.")
        p = profiles[profile_name]
        # Only apply profile values for options that weren't explicitly set by the user
        source = ctx.get_parameter_source("genre")
        if not genre and p.get("genre"):
            genre = p["genre"]
        if not exclude_genre and p.get("exclude_genre"):
            exclude_genre = p["exclude_genre"]
        if not keywords and p.get("keywords"):
            keywords = p["keywords"]
        if ctx.get_parameter_source("max_price") != click.core.ParameterSource.COMMANDLINE and p.get("max_price"):
            max_price = p["max_price"]
        if ctx.get_parameter_source("sort") != click.core.ParameterSource.COMMANDLINE and p.get("sort"):
            sort = p["sort"]
        if not min_rating and p.get("min_rating"):
            min_rating = p["min_rating"]
        if not min_ratings and p.get("min_ratings"):
            min_ratings = p["min_ratings"]
        if not min_hours and p.get("min_hours"):
            min_hours = p["min_hours"]
        if not narrator and p.get("narrator"):
            narrator = p["narrator"]
        if not on_sale and p.get("on_sale"):
            on_sale = True
        if not deep and p.get("deep"):
            deep = True
        if ctx.get_parameter_source("pages") != click.core.ParameterSource.COMMANDLINE and p.get("pages"):
            pages = p["pages"]
        if not first_in_series and p.get("first_in_series"):
            first_in_series = True
        if not all_languages and p.get("all_languages"):
            all_languages = True
        if not limit and p.get("limit"):
            limit = p["limit"]
    if genre and category:
        raise click.UsageError("Use --genre or --category, not both.")
    if json_flag:
        console.file = sys.stderr
    if not language and not all_languages:
        language = LOCALE_LANGUAGES.get(ctx.obj["locale"], "")

    dc = _get_client(ctx.obj["locale"])
    server_sort = SORT_OPTIONS.get(sort, "BestSellers")
    all_products: list[Product] = []
    seen_asins: set[str] = set()
    category_name = ""
    skip_asins: set[str] | None = None
    exclude_category_ids: set[str] = set()

    sort_orders = DEEP_SORT_ORDERS if deep else [server_sort]
    total_pages = pages * len(sort_orders)

    with dc:
        if skip_owned:
            skip_asins = dc.get_library_asins()
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

        desc_parts = []
        if keywords:
            desc_parts.append(f'"{keywords}"')
        if category:
            desc_parts.append(category_name or category)
        if not desc_parts:
            desc_parts.append("entire catalog")
        desc_str = ", ".join(desc_parts)

        with create_scan_progress() as progress:
            task = progress.add_task(
                f"Scanning {desc_str}", total=total_pages, items=0,
            )
            pages_done = 0

            for sort_order in sort_orders:
                for products, page_num, total in dc.search_pages(
                    keywords=keywords,
                    category_id=category,
                    sort_by=sort_order,
                    max_pages=pages,
                ):
                    new_products = [p for p in products if p.asin not in seen_asins]
                    seen_asins.update(p.asin for p in new_products)
                    all_products.extend(new_products)
                    pages_done += 1

                    # Refine total on first page of each sort pass
                    if page_num == 1:
                        actual = min(pages, math.ceil(total / 50)) if total else 1
                        remaining_sorts = len(sort_orders) - sort_orders.index(sort_order) - 1
                        total_pages = (pages_done - 1) + actual + remaining_sorts * pages
                        progress.update(task, total=total_pages)

                    progress.update(task, completed=pages_done, items=len(all_products))

    cur = LOCALE_CURRENCY.get(ctx.obj["locale"], "$")
    find_title = f"Deals under {cur}{max_price:.2f}"
    if keywords:
        find_title += f' matching "{keywords}"'
    _postprocess_and_output(
        all_products,
        title=find_title,
        max_price=max_price, min_rating=min_rating, min_ratings=min_ratings,
        min_hours=min_hours, narrator=narrator,
        language=language, on_sale=on_sale, skip_asins=skip_asins,
        exclude_category_ids=exclude_category_ids,
        first_in_series=first_in_series, sort=sort, limit=limit,
        output=output, json_flag=json_flag, quiet=quiet,
        currency=cur, interactive=interactive,
    )


@cli.command()
@click.argument("asin")
@click.pass_context
def detail(ctx, asin):
    """Show detailed info for a product by ASIN."""
    _validate_asin(asin)
    dc = _get_client(ctx.obj["locale"])
    with dc:
        try:
            product = dc.get_product(asin)
        except ValueError as e:
            raise click.ClickException(str(e))

    display_product_detail(product)


@cli.command("open")
@click.argument("asin")
@click.pass_context
def open_cmd(ctx, asin):
    """Open an audiobook's Audible page in your browser."""
    _validate_asin(asin)
    from audible_deals.client import LOCALE_DOMAIN
    domain = LOCALE_DOMAIN.get(ctx.obj["locale"], "www.audible.com")
    url = f"https://{domain}/pd/{asin}"
    console.print(f"[dim]Opening {url}[/dim]")
    click.launch(url)


@cli.command()
@click.argument("asins", nargs=-1, required=True)
@click.pass_context
def compare(ctx, asins):
    """Compare multiple products side-by-side.

    \b
    Example:
        deals compare B00R6S1RCY B00I2VWW5U B019NMZ6FE
    """
    if len(asins) < 2:
        raise click.UsageError("Provide at least 2 ASINs to compare.")

    for asin in asins:
        _validate_asin(asin)

    dc = _get_client(ctx.obj["locale"])
    with dc:
        products = dc.get_products_batch(list(asins))

    found_asins = {p.asin for p in products}
    for asin in asins:
        if asin not in found_asins:
            console.print(f"[red]Not found: {asin}[/red]")

    if len(products) < 2:
        raise click.ClickException("Need at least 2 valid products to compare.")

    # Preserve the order the user specified
    asin_order = {asin: i for i, asin in enumerate(asins)}
    products.sort(key=lambda p: asin_order.get(p.asin, 999))

    display_comparison(products)


# ---------------------------------------------------------------------------
# Wishlist management
# ---------------------------------------------------------------------------
WISHLIST_FILE = CONFIG_DIR / "wishlist.json"


def _load_wishlist() -> list[dict]:
    if WISHLIST_FILE.exists():
        try:
            return json_mod.loads(WISHLIST_FILE.read_text())
        except (json_mod.JSONDecodeError, KeyError):
            pass
    return []


def _save_wishlist(items: list[dict]) -> None:
    _atomic_write(WISHLIST_FILE, json_mod.dumps(items, indent=2, ensure_ascii=False))


@cli.group()
def wishlist():
    """Manage your audiobook wishlist."""


@wishlist.command("add")
@click.argument("asins", nargs=-1, required=True)
@click.option("--max-price", type=float, default=None, help="Alert when price drops below this")
@click.pass_context
def wishlist_add(ctx, asins, max_price):
    """Add ASINs to your wishlist.

    \b
    Example:
        deals wishlist add B00R6S1RCY B00I2VWW5U --max-price 5
    """
    items = _load_wishlist()
    existing = {item["asin"] for item in items}

    for asin in asins:
        _validate_asin(asin)

    dc = _get_client(ctx.obj["locale"])
    added = 0
    with dc:
        for asin in asins:
            if asin in existing:
                console.print(f"[dim]{asin} already on wishlist[/dim]")
                continue
            try:
                p = dc.get_product(asin)
            except ValueError:
                console.print(f"[red]Not found: {asin}[/red]")
                continue
            items.append({
                "asin": p.asin,
                "title": p.title,
                "max_price": max_price,
                "added": p.release_date or "",
            })
            existing.add(p.asin)
            added += 1
            console.print(f"[green]+[/green] {p.title} ({p.asin})")

    _save_wishlist(items)
    console.print(f"\n[bold]{added}[/bold] added, {len(items)} total on wishlist")


@wishlist.command("remove")
@click.argument("asins", nargs=-1, required=True)
def wishlist_remove(asins):
    """Remove ASINs from your wishlist."""
    items = _load_wishlist()
    remove_set = set(asins)
    before = len(items)
    items = [i for i in items if i["asin"] not in remove_set]
    _save_wishlist(items)
    removed = before - len(items)
    console.print(f"[bold]{removed}[/bold] removed, {len(items)} remaining")


@wishlist.command("list")
def wishlist_list():
    """Show your wishlist."""
    items = _load_wishlist()
    if not items:
        console.print("[dim]Wishlist is empty. Use 'deals wishlist add ASIN' to add items.[/dim]")
        return

    table = Table(title="Wishlist", show_lines=False, padding=(0, 1), title_style="bold")
    table.add_column("ASIN", style="cyan", width=14)
    table.add_column("Title", max_width=40)
    table.add_column("Target", justify="right", width=10)

    for item in items:
        target = f"${item['max_price']:.2f}" if item.get("max_price") else "-"
        table.add_row(item["asin"], item["title"], target)

    console.print(table)


@cli.command()
@click.pass_context
def watch(ctx):
    """Check wishlist prices and highlight deals.

    Fetches current prices for all wishlist items and shows which ones
    are at or below your target price.
    """
    items = _load_wishlist()
    if not items:
        console.print("[dim]Wishlist is empty. Use 'deals wishlist add ASIN' to add items.[/dim]")
        return

    dc = _get_client(ctx.obj["locale"])
    targets: dict[str, float | None] = {item["asin"]: item.get("max_price") for item in items}
    item_titles: dict[str, str] = {item["asin"]: item.get("title", "") for item in items}

    with dc:
        products = dc.get_products_batch([item["asin"] for item in items])

    found_asins = {p.asin for p in products}
    for item in items:
        if item["asin"] not in found_asins:
            console.print(f"[red]Not found: {item['asin']} ({item['title']})[/red]")

    if not products:
        return

    table = Table(title="Wishlist Price Check", show_lines=False, padding=(0, 1), title_style="bold")
    table.add_column("Title", max_width=35)
    table.add_column("Price", justify="right", width=12)
    table.add_column("Target", justify="right", width=10)
    table.add_column("Status", width=10)

    cur = LOCALE_CURRENCY.get(ctx.obj["locale"], "$")
    hits = 0
    for p in products:
        target = targets.get(p.asin)
        target_str = f"{cur}{target:.2f}" if target else "-"
        p_str = f"{cur}{p.price:.2f}" if p.price is not None else "-"
        if target and p.price is not None and p.price <= target:
            status = "[bold green]BUY[/bold green]"
            p_str = f"[bold green]{p_str}[/bold green]"
            hits += 1
        elif p.discount_pct and p.discount_pct > 0:
            status = f"[yellow]-{p.discount_pct}%[/yellow]"
        else:
            status = "[dim]waiting[/dim]"
        table.add_row(
            f"{p.title}\n[dim]{p.authors_str}  [cyan]{p.asin}[/cyan][/dim]",
            p_str,
            target_str,
            status,
        )

    console.print(table)
    if hits:
        console.print(f"\n  [bold green]{hits} item(s) at or below target price![/bold green]")
    else:
        console.print(f"\n  [dim]No items at target price yet. {len(products)} watched.[/dim]")


# ---------------------------------------------------------------------------
# Saved search profiles
# ---------------------------------------------------------------------------
PROFILES_FILE = CONFIG_DIR / "profiles.json"


def _load_profiles() -> dict[str, dict]:
    if PROFILES_FILE.exists():
        try:
            return json_mod.loads(PROFILES_FILE.read_text())
        except (json_mod.JSONDecodeError, KeyError):
            pass
    return {}


def _save_profiles(profiles: dict[str, dict]) -> None:
    _atomic_write(PROFILES_FILE, json_mod.dumps(profiles, indent=2, ensure_ascii=False))


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
@click.option("--on-sale", is_flag=True, default=False)
@click.option("--deep", is_flag=True, default=False)
@click.option("--pages", type=int, default=None)
@click.option("--first-in-series", is_flag=True, default=False)
@click.option("--all-languages", is_flag=True, default=False)
@click.option("--limit", "-n", type=int, default=None)
def profile_save(name, **kwargs):
    """Save a search profile.

    \b
    Example:
        deals profile save my-scifi --genre sci-fi --max-price 5 --min-rating 4 --first-in-series
        deals find --profile my-scifi
    """
    profiles = _load_profiles()
    # Only save non-default values
    saved = {k: v for k, v in kwargs.items() if v}
    profiles[name] = saved
    _save_profiles(profiles)
    console.print(f"[green]Profile '{name}' saved[/green] ({len(saved)} options)")


@profile.command("list")
def profile_list():
    """List saved profiles."""
    profiles = _load_profiles()
    if not profiles:
        console.print("[dim]No profiles saved. Use 'deals profile save NAME --flags...' to create one.[/dim]")
        return

    for name, opts in profiles.items():
        flags = " ".join(f"--{k.replace('_', '-')} {v}" if not isinstance(v, bool) else f"--{k.replace('_', '-')}"
                         for k, v in opts.items())
        console.print(f"  [bold]{name}[/bold]  [dim]{flags}[/dim]")


@profile.command("delete")
@click.argument("name")
def profile_delete(name):
    """Delete a saved profile."""
    profiles = _load_profiles()
    if name not in profiles:
        raise click.ClickException(f"Profile '{name}' not found.")
    del profiles[name]
    _save_profiles(profiles)
    console.print(f"[green]Profile '{name}' deleted[/green]")


# ---------------------------------------------------------------------------
# Price history
# ---------------------------------------------------------------------------
HISTORY_DIR = CONFIG_DIR / "history"


_history_dir_created = False


def _record_prices(products: list[Product]) -> None:
    """Append today's prices to per-ASIN history files.

    Batches writes: reads all existing files, updates in-memory,
    then writes only changed files.
    """
    global _history_dir_created
    priced = [p for p in products if p.price is not None]
    if not priced:
        return
    if not _history_dir_created:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        _history_dir_created = True

    today = datetime.date.today().isoformat()
    to_write: dict[Path, list[dict]] = {}

    for p in priced:
        if not _ASIN_RE.fullmatch(p.asin):
            continue
        hist_file = HISTORY_DIR / f"{p.asin}.json"
        entries: list[dict] = []
        if hist_file.exists():
            try:
                entries = json_mod.loads(hist_file.read_text())
            except json_mod.JSONDecodeError:
                entries = []
        if entries and entries[-1].get("date") == today:
            continue
        entries.append({"date": today, "price": round(p.price, 2)})
        to_write[hist_file] = entries[-365:]

    for path, entries in to_write.items():
        _atomic_write(path, json_mod.dumps(entries))


@cli.command()
@click.argument("asin")
@click.pass_context
def history(ctx, asin):
    """Show price history for an ASIN.

    History is recorded automatically each time an ASIN appears in
    search/find results. Use 'deals history ASIN' to view past prices.
    """
    _validate_asin(asin)
    hist_file = HISTORY_DIR / f"{asin}.json"
    if not hist_file.exists():
        console.print(
            f"[dim]No price history for {asin}. "
            "History is recorded when items appear in search/find results.[/dim]"
        )
        return

    try:
        entries = json_mod.loads(hist_file.read_text())
    except json_mod.JSONDecodeError:
        raise click.ClickException(f"Corrupted history file for {asin}")

    if not entries:
        console.print(f"[dim]No price history for {asin}.[/dim]")
        return

    today = datetime.date.today()

    def _relative_date(date_str: str) -> str:
        try:
            d = datetime.date.fromisoformat(date_str)
            delta = (today - d).days
            if delta == 0:
                return "today"
            elif delta == 1:
                return "yesterday"
            elif delta < 7:
                return f"{delta}d ago"
            elif delta < 30:
                return f"{delta // 7}w ago"
            else:
                return f"{delta // 30}mo ago"
        except ValueError:
            return ""

    cur = LOCALE_CURRENCY.get(ctx.obj["locale"], "$")

    table = Table(title=f"Price History: {asin}", show_lines=False, padding=(0, 1), title_style="bold")
    table.add_column("Date", width=12)
    table.add_column("Ago", width=10, style="dim")
    table.add_column("Price", justify="right", width=10)
    table.add_column("Change", justify="right", width=10)

    prev_price = None
    for entry in entries:
        price = entry["price"]
        p_str = f"{cur}{price:.2f}"
        if prev_price is not None:
            diff = price - prev_price
            if diff < 0:
                change = f"[green]{diff:+.2f}[/green]"
            elif diff > 0:
                change = f"[red]+{diff:.2f}[/red]"
            else:
                change = "[dim]-[/dim]"
        else:
            change = "[dim]-[/dim]"
        table.add_row(entry["date"], _relative_date(entry["date"]), p_str, change)
        prev_price = price

    console.print(table)

    low = min(e["price"] for e in entries)
    high = max(e["price"] for e in entries)
    current = entries[-1]["price"]
    console.print(f"\n  Low: [green]{cur}{low:.2f}[/green]  High: [red]{cur}{high:.2f}[/red]  Current: {cur}{current:.2f}")

    # Sparkline if more than 1 entry
    if len(entries) > 1:
        prices = [e["price"] for e in entries]
        lo, hi = min(prices), max(prices)
        sparks = " ▁▂▃▄▅▆▇█"
        if hi == lo:
            line = sparks[4] * len(prices)
        else:
            line = "".join(sparks[min(8, int((p - lo) / (hi - lo) * 8))] for p in prices)
        console.print(f"  [dim]{line}[/dim]")


@cli.command()
@click.option("--days", type=int, default=7, help="Look back this many days (default: 7)")
def recap(days):
    """Show a recap of price changes across tracked items.

    Scans price history files and reports items that dropped in price,
    new items tracked, and wishlist items at target.
    """
    if not HISTORY_DIR.exists():
        console.print("[dim]No price history yet. Run 'deals find' or 'deals search' to start tracking.[/dim]")
        return

    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    drops: list[tuple[str, float, float]] = []  # (asin, old_price, new_price)
    new_items: list[tuple[str, float]] = []  # (asin, price)

    for hist_file in HISTORY_DIR.glob("*.json"):
        asin = hist_file.stem
        try:
            entries = json_mod.loads(hist_file.read_text())
        except json_mod.JSONDecodeError:
            continue
        if not entries:
            continue

        recent = [e for e in entries if e["date"] >= cutoff]
        if not recent:
            continue

        # New item: first entry is within the window
        if entries[0]["date"] >= cutoff and len(entries) == len(recent):
            new_items.append((asin, entries[-1]["price"]))
            continue

        # Price drop: compare earliest in-window to latest
        before = [e for e in entries if e["date"] < cutoff]
        if before and recent:
            old_price = before[-1]["price"]
            new_price = recent[-1]["price"]
            if new_price < old_price:
                drops.append((asin, old_price, new_price))

    # Wishlist hits
    wishlist_items = _load_wishlist()
    wishlist_hits: list[dict] = []
    for item in wishlist_items:
        if not _ASIN_RE.fullmatch(item.get("asin", "")):
            continue
        hist_file = HISTORY_DIR / f"{item['asin']}.json"
        if not hist_file.exists():
            continue
        try:
            entries = json_mod.loads(hist_file.read_text())
        except json_mod.JSONDecodeError:
            continue
        if entries and item.get("max_price") and entries[-1]["price"] <= item["max_price"]:
            wishlist_hits.append(item)

    console.print(f"\n[bold]Recap[/bold] (last {days} days)\n")

    if drops:
        console.print(f"  [green]Price drops: {len(drops)}[/green]")
        for asin, old, new in sorted(drops, key=lambda x: x[1] - x[2], reverse=True)[:10]:
            console.print(f"    {asin}  ${old:.2f} -> [green]${new:.2f}[/green]  ([green]-${old - new:.2f}[/green])")
    else:
        console.print("  [dim]No price drops[/dim]")

    if new_items:
        console.print(f"\n  [cyan]Newly tracked: {len(new_items)}[/cyan]")
    if wishlist_hits:
        console.print(f"\n  [bold green]Wishlist items at target: {len(wishlist_hits)}[/bold green]")
        for item in wishlist_hits:
            console.print(f"    {item['asin']}  {item['title']}")

    if not drops and not new_items and not wishlist_hits:
        console.print("  [dim]Nothing to report.[/dim]")
    console.print()


### Improvement 6: Notification support for watch

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
        _validate_webhook_url(webhook)

    items = _load_wishlist()
    if not items:
        return

    dc = _get_client(ctx.obj["locale"])
    targets = {item["asin"]: item.get("max_price") for item in items}

    with dc:
        products = dc.get_products_batch([item["asin"] for item in items])

    hits = []
    for p in products:
        target = targets.get(p.asin)
        if target and p.price is not None and p.price <= target:
            hits.append({
                "asin": p.asin,
                "title": p.title,
                "price": round(p.price, 2),
                "target": target,
                "url": p.url,
            })

    if not hits:
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
