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

import csv
import datetime
import ipaddress
import json as json_mod
import math
import os
import re
from importlib.metadata import version as _pkg_version
try:
    import readline  # noqa: F401 — required on macOS for input() with long strings
except ImportError:
    pass  # unavailable on Windows
import socket
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import dataclasses
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
    author: str = "",
    exclude_authors: tuple[str, ...] = (),
    exclude_narrators: tuple[str, ...] = (),
    on_sale: bool = False,
    skip_asins: set[str] | None = None,
    exclude_category_ids: set[str] | None = None,
) -> tuple[list[Product], dict[str, int]]:
    """Apply client-side filters. Returns (filtered, breakdown_by_filter)."""
    filtered = products
    breakdown: dict[str, int] = {}

    if skip_asins:
        before = len(filtered)
        filtered = [p for p in filtered if p.asin not in skip_asins]
        if (removed := before - len(filtered)):
            breakdown["owned"] = removed

    if max_price is not None:
        before = len(filtered)
        filtered = [p for p in filtered if p.price is not None and p.price <= max_price]
        if (removed := before - len(filtered)):
            breakdown["max price"] = removed

    if min_rating > 0:
        before = len(filtered)
        filtered = [p for p in filtered if p.rating >= min_rating]
        if (removed := before - len(filtered)):
            breakdown["min rating"] = removed

    if min_ratings > 0:
        before = len(filtered)
        filtered = [p for p in filtered if p.num_ratings >= min_ratings]
        if (removed := before - len(filtered)):
            breakdown["min ratings"] = removed

    if min_hours > 0:
        before = len(filtered)
        filtered = [p for p in filtered if p.hours >= min_hours]
        if (removed := before - len(filtered)):
            breakdown["min hours"] = removed

    if language:
        before = len(filtered)
        lang_lower = language.lower()
        filtered = [p for p in filtered if p.language.lower() == lang_lower]
        if (removed := before - len(filtered)):
            breakdown["language"] = removed

    if narrator:
        before = len(filtered)
        narrator_lower = narrator.lower()
        filtered = [
            p for p in filtered
            if any(narrator_lower in n.lower() for n in p.narrators)
        ]
        if (removed := before - len(filtered)):
            breakdown["narrator"] = removed

    if author:
        before = len(filtered)
        author_lower = author.lower()
        filtered = [
            p for p in filtered
            if any(author_lower in a.lower() for a in p.authors)
        ]
        if (removed := before - len(filtered)):
            breakdown["author"] = removed

    if exclude_authors:
        before = len(filtered)
        exclude_lower = [a.lower() for a in exclude_authors]
        filtered = [
            p for p in filtered
            if not any(
                ex in author_lc
                for author_lc in (a.lower() for a in p.authors)
                for ex in exclude_lower
            )
        ]
        if (removed := before - len(filtered)):
            breakdown["excluded authors"] = removed

    if exclude_narrators:
        before = len(filtered)
        exclude_lower = [n.lower() for n in exclude_narrators]
        filtered = [
            p for p in filtered
            if not any(
                ex in narrator_lc
                for narrator_lc in (n.lower() for n in p.narrators)
                for ex in exclude_lower
            )
        ]
        if (removed := before - len(filtered)):
            breakdown["excluded narrators"] = removed

    if on_sale:
        before = len(filtered)
        filtered = [p for p in filtered if p.discount_pct is not None and p.discount_pct > 0]
        if (removed := before - len(filtered)):
            breakdown["on sale"] = removed

    if exclude_category_ids:
        before = len(filtered)
        filtered = [
            p for p in filtered
            if not any(cid in exclude_category_ids for cid in p.category_ids)
        ]
        if (removed := before - len(filtered)):
            breakdown["excluded genres"] = removed

    return filtered, breakdown


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
    elif sort == "title":
        return sorted(products, key=lambda p: p.title.lower())
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


_PRODUCT_FIELDS: frozenset[str] = frozenset(f.name for f in dataclasses.fields(Product))


def _deserialize_product(d: dict) -> Product:
    """Reconstruct a Product from a serialized dict, ignoring computed fields."""
    return Product(**{k: v for k, v in d.items() if k in _PRODUCT_FIELDS})


_CL = click.core.ParameterSource.COMMANDLINE


def _apply_config_defaults(ctx: click.Context, ns: dict, cfg: dict) -> None:
    """Apply global config values to the command namespace for keys not set on the CLI.

    ``ns`` is mutated in place. Uses ``ctx.get_parameter_source`` to detect CLI flags.
    Handles ``max_price`` specially (falsy 0.0 is a valid price).
    """
    if cfg.get("max_price") is not None and ctx.get_parameter_source("max_price") != _CL:
        ns["max_price"] = cfg["max_price"]
    for key in ("sort", "pages"):
        if cfg.get(key) and ctx.get_parameter_source(key) != _CL:
            ns[key] = cfg[key]
    for key in ("min_rating", "min_ratings", "min_hours", "limit"):
        if cfg.get(key) is not None and ctx.get_parameter_source(key) != _CL:
            ns[key] = cfg[key]
    for key in ("language", "narrator", "author"):
        if cfg.get(key) and not ns.get(key):
            ns[key] = cfg[key]
    for flag in ("on_sale", "deep", "first_in_series", "all_languages", "skip_owned", "interactive"):
        if cfg.get(flag) and not ns.get(flag):
            ns[flag] = True


def _apply_profile_defaults(ctx: click.Context, ns: dict, p: dict) -> None:
    """Apply profile values to the command namespace for keys not set on the CLI.

    ``ns`` is mutated in place. Profile values override config but not CLI flags.
    """
    for key in ("genre", "exclude_genre", "exclude_authors", "exclude_narrators", "keywords", "narrator", "author", "language"):
        if not ns.get(key) and p.get(key):
            ns[key] = p[key]
    for key in ("max_price", "min_rating", "min_ratings", "min_hours", "limit"):
        if ctx.get_parameter_source(key) != _CL and p.get(key) is not None:
            ns[key] = p[key]
    for key in ("sort", "pages"):
        if ctx.get_parameter_source(key) != _CL and p.get(key):
            ns[key] = p[key]
    for flag in ("on_sale", "deep", "first_in_series", "all_languages", "skip_owned", "interactive"):
        if not ns.get(flag) and p.get(flag):
            ns[flag] = True


def _load_last_results() -> tuple[str, list[dict]]:
    """Load the last results cache from disk.

    Returns (title, products) where title is the original query context.
    Raises click.ClickException if the cache is missing or corrupt.
    Handles backward compatibility with the old plain-list format.
    """
    if not LAST_RESULTS_FILE.exists():
        raise click.ClickException(
            "No cached results found. Run 'deals find' or 'deals search' first."
        )
    try:
        data = json_mod.loads(LAST_RESULTS_FILE.read_text())
    except (json_mod.JSONDecodeError, OSError) as e:
        raise click.ClickException(f"Could not read last results cache: {e}")
    if isinstance(data, dict) and "results" in data:
        return data.get("title", "Last results"), data["results"]
    # Backward compat: old format is a plain list
    if isinstance(data, list):
        return "Last results", data
    raise click.ClickException("Last results cache is corrupt.")


def _resolve_last_references(refs: tuple[int, ...]) -> list[str]:
    """Convert 1-indexed position references to ASINs from the last results cache."""
    _, data = _load_last_results()
    asins: list[str] = []
    for ref in refs:
        if ref < 1 or ref > len(data):
            raise click.ClickException(
                f"--last {ref} is out of range (cache has {len(data)} result(s))."
            )
        asins.append(data[ref - 1]["asin"])
    return asins


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
) -> None:
    """Shared post-processing pipeline for search and find commands."""
    filtered, filter_breakdown = _filter_products(
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
    )
    filtered, editions_removed = _dedupe_editions(filtered)
    series_collapsed = 0
    if first_in_series:
        filtered, series_collapsed = _first_in_series(filtered)
    filtered = _sort_local(filtered, sort)
    _record_prices(filtered)
    serialized_all = [_serialize_product(p) for p in filtered]
    if write_cache:
        try:
            cache_obj = {"title": title, "results": serialized_all}
            _atomic_write(LAST_RESULTS_FILE, json_mod.dumps(cache_obj, ensure_ascii=False))
        except Exception:
            pass
    total_before_limit = len(filtered)
    if limit is not None and limit > 0:
        filtered = filtered[:limit]
        serialized = serialized_all[:limit]
    else:
        serialized = serialized_all

    if output:
        _export_products(filtered, output)
        console.print(f"[green]Exported {len(filtered)} items to {output}[/green]")
    if json_flag:
        click.echo(json_mod.dumps(serialized, indent=2, ensure_ascii=False))
    if not json_flag and not quiet:
        console.print()
        display_products(filtered, max_price=max_price, title=title, currency=currency)
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
            items = _load_wishlist()
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
                items.append({"asin": p.asin, "title": p.title, "max_price": target_price, "added": ""})
                _save_wishlist(items)
                target_note = f" (target: {p.currency}{target_price:.2f})" if target_price else ""
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
@click.version_option(version=_pkg_version("audible-deals"), prog_name="deals")
@click.option("--locale", default="us", help="Audible marketplace (us, uk, ca, de, fr, au, jp, in, es)")
@click.pass_context
def cli(ctx, locale):
    """Audible deal finder - find cheap audiobooks during sales."""
    ctx.ensure_object(dict)
    cfg = _load_config()
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


@cli.command()
@click.argument("query", required=False, default="")
@click.option("--max-price", type=click.FloatRange(min=0), default=None, help="Max price filter (e.g. 5.00)")
@click.option("--category", default="", help="Category ID to search within")
@click.option("--genre", default="", help="Genre name to search within (fuzzy match, e.g. 'sci-fi')")
@click.option("--exclude-genre", multiple=True, help="Genre(s) to exclude (repeatable, fuzzy match)")
@click.option("--sort", type=click.Choice(list(SORT_OPTIONS.keys()) + ["price", "-price", "discount", "price-per-hour"]), default="relevance", help="Sort order (price/discount/price-per-hour are client-side)")
@click.option("--min-rating", type=float, default=0.0, help="Minimum rating (e.g. 4.0)")
@click.option("--min-ratings", type=int, default=0, help="Minimum number of ratings (e.g. 100)")
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours")
@click.option("--narrator", default="", help="Filter by narrator name (substring match)")
@click.option("--author", default="", help="Filter by author name (substring match)")
@click.option("--exclude-author", "exclude_authors", multiple=True, help="Exclude author (substring match, repeatable)")
@click.option("--exclude-narrator", "exclude_narrators", multiple=True, help="Exclude narrator (substring match, repeatable)")
@click.option("--on-sale", is_flag=True, default=False, help="Only show discounted items")
@click.option("--deep", is_flag=True, default=False, help="Scan with 3 sort orders for better coverage (3x API calls)")
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
@click.option("--profile", "profile_name", default=None, help="Load a saved search profile (overrides defaults, CLI flags take precedence)")
@click.pass_context
def search(ctx, query, max_price, category, genre, exclude_genre, sort, min_rating, min_ratings, min_hours, narrator, author, exclude_authors, exclude_narrators, on_sale, deep, pages, language, all_languages, first_in_series, skip_owned, limit, output, json_flag, quiet, interactive, profile_name):
    """Search the Audible catalog by keyword."""
    if not query and not genre and not category:
        raise click.UsageError("Provide a QUERY or use --genre / --category to browse.")
    ns = dict(
        max_price=max_price, sort=sort, min_rating=min_rating, min_ratings=min_ratings,
        min_hours=min_hours, language=language, narrator=narrator, author=author,
        pages=pages, limit=limit,
        on_sale=on_sale, deep=deep, first_in_series=first_in_series,
        all_languages=all_languages, skip_owned=skip_owned, interactive=interactive,
        genre=genre, exclude_genre=exclude_genre, exclude_authors=exclude_authors,
        exclude_narrators=exclude_narrators, keywords="",
    )
    _apply_config_defaults(ctx, ns, ctx.obj.get("config", {}))
    if profile_name:
        profiles = _load_profiles()
        if profile_name not in profiles:
            raise click.ClickException(f"Profile '{profile_name}' not found. Use 'deals profile list' to see available profiles.")
        _apply_profile_defaults(ctx, ns, profiles[profile_name])
    (max_price, sort, min_rating, min_ratings, min_hours, language, narrator, author,
     pages, limit, on_sale, deep, first_in_series, all_languages, skip_owned,
     interactive, genre, exclude_genre, exclude_authors, exclude_narrators) = (
        ns["max_price"], ns["sort"], ns["min_rating"], ns["min_ratings"], ns["min_hours"],
        ns["language"], ns["narrator"], ns["author"], ns["pages"], ns["limit"],
        ns["on_sale"], ns["deep"],
        ns["first_in_series"], ns["all_languages"], ns["skip_owned"], ns["interactive"],
        ns["genre"], ns["exclude_genre"], ns["exclude_authors"], ns["exclude_narrators"],
    )
    if output and ctx.get_parameter_source("quiet") != _CL:
        quiet = True
    if genre and category:
        raise click.UsageError("Use --genre or --category, not both.")
    if json_flag:
        console.file = sys.stderr
    if not language and not all_languages:
        language = LOCALE_LANGUAGES.get(ctx.obj["locale"], "")

    dc = _get_client(ctx.obj["locale"])
    server_sort = SORT_OPTIONS.get(sort, "Relevance")
    sort_orders = DEEP_SORT_ORDERS if deep else [server_sort]
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

        if query:
            scope = f"'{query}'"
            if category_name:
                scope += f" in {category_name}"
        elif category_name:
            scope = category_name
        else:
            scope = "catalog"

        all_products = _fetch_with_progress(
            dc,
            keywords=query,
            category_id=category,
            sort_orders=sort_orders,
            pages=pages,
            description=f"Searching {scope}",
        )

    cur = LOCALE_CURRENCY.get(ctx.obj["locale"], "$")
    if query:
        search_title = f'Search: "{query}"'
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
        currency=cur, interactive=interactive,
    )


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
@click.option("--exclude-genre", multiple=True, help="Genre(s) to exclude (repeatable, fuzzy match)")
@click.option("--keywords", default="", help="Optional keyword filter within the category")
@click.option("--max-price", type=click.FloatRange(min=0), default=5.00, help="Max price threshold (default: $5.00)")
@click.option("--sort", type=click.Choice(["price", "-price", "discount", "price-per-hour"] + list(SORT_OPTIONS.keys())), default="price-per-hour", help="Sort order (price/discount/price-per-hour are client-side)")
@click.option("--min-rating", type=float, default=0.0, help="Minimum rating (e.g. 4.0)")
@click.option("--min-ratings", type=int, default=1, help="Minimum number of ratings (default: 1, filters unreviewed)")
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours (filters out shorts)")
@click.option("--narrator", default="", help="Filter by narrator name (substring match)")
@click.option("--author", default="", help="Filter by author name (substring match)")
@click.option("--exclude-author", "exclude_authors", multiple=True, help="Exclude author (substring match, repeatable)")
@click.option("--exclude-narrator", "exclude_narrators", multiple=True, help="Exclude narrator (substring match, repeatable)")
@click.option("--on-sale", is_flag=True, default=False, help="Only show discounted items")
@click.option("--deep", is_flag=True, default=False, help="Scan with 3 sort orders for better coverage (3x API calls)")
@click.option("--pages", type=click.IntRange(min=1), default=10, help="Pages to scan per sort order (50 items/page, default: 10)")
@click.option("--language", default="", help="Language filter (e.g. english)")
@click.option("--all-languages", is_flag=True, default=False, help="Include all languages (default: locale language only)")
@click.option("--first-in-series", is_flag=True, default=False, help="Show only the first book per series")
@click.option("--skip-owned", is_flag=True, default=False, help="Exclude books already in your library")
@click.option("--limit", "-n", type=int, default=25, help="Show only the top N results (0 for unlimited, default: 25)")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None, help="Export results to file (.json or .csv)")
@click.option("--json", "json_flag", is_flag=True, default=False, help="Output results as JSON to stdout")
@click.option("--quiet", "-q", is_flag=True, default=False, help="Suppress table output (useful with --output)")
@click.option("--profile", "profile_name", default=None, help="Load a saved search profile (overrides defaults, CLI flags take precedence)")
@click.option("--interactive", "-i", is_flag=True, default=False, help="Browse results interactively")
@click.pass_context
def find(ctx, category, genre, exclude_genre, keywords, max_price, sort, min_rating, min_ratings, min_hours, narrator, author, exclude_authors, exclude_narrators, on_sale, deep, pages, language, all_languages, first_in_series, skip_owned, limit, output, json_flag, quiet, profile_name, interactive):
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
    ns = dict(
        max_price=max_price, sort=sort, min_rating=min_rating, min_ratings=min_ratings,
        min_hours=min_hours, language=language, narrator=narrator, author=author,
        pages=pages, limit=limit,
        on_sale=on_sale, deep=deep, first_in_series=first_in_series,
        all_languages=all_languages, skip_owned=skip_owned, interactive=interactive,
        genre=genre, exclude_genre=exclude_genre, exclude_authors=exclude_authors,
        exclude_narrators=exclude_narrators, keywords=keywords,
    )
    _apply_config_defaults(ctx, ns, ctx.obj.get("config", {}))
    if profile_name:
        profiles = _load_profiles()
        if profile_name not in profiles:
            raise click.ClickException(f"Profile '{profile_name}' not found. Use 'deals profile list' to see available profiles.")
        _apply_profile_defaults(ctx, ns, profiles[profile_name])
    (max_price, sort, min_rating, min_ratings, min_hours, language, narrator, author,
     pages, limit, on_sale, deep, first_in_series, all_languages, skip_owned,
     interactive, genre, exclude_genre, exclude_authors, exclude_narrators, keywords) = (
        ns["max_price"], ns["sort"], ns["min_rating"], ns["min_ratings"], ns["min_hours"],
        ns["language"], ns["narrator"], ns["author"], ns["pages"], ns["limit"],
        ns["on_sale"], ns["deep"],
        ns["first_in_series"], ns["all_languages"], ns["skip_owned"], ns["interactive"],
        ns["genre"], ns["exclude_genre"], ns["exclude_authors"], ns["exclude_narrators"], ns["keywords"],
    )
    if output and ctx.get_parameter_source("quiet") != _CL:
        quiet = True
    if genre and category:
        raise click.UsageError("Use --genre or --category, not both.")
    if json_flag:
        console.file = sys.stderr
    if not language and not all_languages:
        language = LOCALE_LANGUAGES.get(ctx.obj["locale"], "")

    dc = _get_client(ctx.obj["locale"])
    server_sort = SORT_OPTIONS.get(sort, "BestSellers")
    category_name = ""
    skip_asins: set[str] | None = None
    exclude_category_ids: set[str] = set()

    sort_orders = DEEP_SORT_ORDERS if deep else [server_sort]

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
        currency=cur, interactive=interactive,
    )


@cli.command()
@click.option("--sort", type=click.Choice(["title", "rating", "length", "date", "price", "-price", "price-per-hour"]), default="date", help="Sort order (default: date — newest first)")
@click.option("-n", "--limit", type=int, default=None, help="Show only the top N results")
@click.option("-o", "--output", type=click.Path(path_type=Path), default=None, help="Export to file (.json or .csv)")
@click.option("--json", "json_flag", is_flag=True, default=False, help="Output as JSON to stdout")
@click.option("-q", "--quiet", is_flag=True, default=False, help="Suppress table output")
@click.pass_context
def library(ctx, sort, limit, output, json_flag, quiet):
    """List all audiobooks in your Audible library.

    Fetches your full library with metadata — useful for exporting to
    a file for analysis or feeding to other tools.

    \b
    Examples:
        deals library
        deals library --json > my-books.json
        deals library -o library.csv
        deals library --sort rating -n 20
    """
    if output and ctx.get_parameter_source("quiet") != _CL:
        quiet = True
    if json_flag:
        console.file = sys.stderr

    dc = _get_client(ctx.obj["locale"])
    with dc:
        with create_scan_progress() as progress:
            task = progress.add_task("Fetching library", total=None, items=0)
            products = dc.get_library()
            progress.update(task, completed=1, total=1, items=len(products))

    products = _sort_local(products, sort)
    total_before_limit = len(products)
    if limit is not None and limit > 0:
        products = products[:limit]

    cur = LOCALE_CURRENCY.get(ctx.obj["locale"], "$")

    if output:
        _export_products(products, output)
        console.print(f"[green]Exported {len(products)} items to {output}[/green]")
    if json_flag:
        serialized = [_serialize_product(p) for p in products]
        click.echo(json_mod.dumps(serialized, indent=2, ensure_ascii=False))
    if not json_flag and not quiet:
        console.print()
        title = "Your Library"
        display_products(products, title=title, currency=cur)
        if total_before_limit > len(products):
            console.print(f"  [bold]{len(products)}[/bold] of {total_before_limit} books shown")
        else:
            console.print(f"  [bold]{len(products)}[/bold] books in library")


@cli.command("last")
@click.option("--sort", type=click.Choice(["price", "-price", "discount", "price-per-hour", "rating", "length", "date", "relevance"]), default=None, help="Re-sort results")
@click.option("--max-price", type=click.FloatRange(min=0), default=None, help="Max price filter")
@click.option("--min-rating", type=float, default=0.0, help="Minimum rating")
@click.option("--min-ratings", type=int, default=0, help="Minimum number of ratings")
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours")
@click.option("--narrator", default="", help="Filter by narrator name (substring match)")
@click.option("--author", default="", help="Filter by author name (substring match)")
@click.option("--exclude-author", "exclude_authors", multiple=True, help="Exclude author (substring match, repeatable)")
@click.option("--exclude-narrator", "exclude_narrators", multiple=True, help="Exclude narrator (substring match, repeatable)")
@click.option("--language", default="", help="Language filter")
@click.option("--on-sale", is_flag=True, default=False, help="Only show discounted items")
@click.option("--first-in-series", is_flag=True, default=False, help="Show only first book per series")
@click.option("--limit", "-n", type=int, default=None, help="Show only the top N results")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None, help="Export results to file (.json or .csv)")
@click.option("--json", "json_flag", is_flag=True, default=False, help="Output results as JSON to stdout")
@click.option("--quiet", "-q", is_flag=True, default=False, help="Suppress table output")
@click.option("--interactive", "-i", is_flag=True, default=False, help="Browse results interactively")
@click.option("--clear", is_flag=True, default=False, help="Delete the cached results and exit")
@click.pass_context
def last_cmd(ctx, sort, max_price, min_rating, min_ratings, min_hours, narrator, author, exclude_authors, exclude_narrators, language, on_sale, first_in_series, limit, output, json_flag, quiet, interactive, clear):
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
    """
    if clear:
        try:
            LAST_RESULTS_FILE.unlink()
            console.print("[green]Last results cache cleared.[/green]")
        except FileNotFoundError:
            console.print("[dim]No cached results to clear.[/dim]")
        return
    if output and ctx.get_parameter_source("quiet") != _CL:
        quiet = True
    cached_title, data = _load_last_results()
    products = [_deserialize_product(d) for d in data]
    if json_flag:
        console.file = sys.stderr

    effective_sort = sort or "price"  # default re-sort by price
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
    )


@cli.command()
@click.argument("asin", required=False, default=None)
@click.option("--last", "last_ref", type=int, default=None, help="Use result #N from last search/find")
@click.pass_context
def detail(ctx, asin, last_ref):
    """Show detailed info for a product by ASIN."""
    if last_ref is not None:
        resolved = _resolve_last_references((last_ref,))
        asin = resolved[0]
    if not asin:
        raise click.UsageError("Provide an ASIN or use --last N.")
    _validate_asin(asin)
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
        resolved = _resolve_last_references((last_ref,))
        asin = resolved[0]
    if not asin:
        raise click.UsageError("Provide an ASIN or use --last N.")
    _validate_asin(asin)
    from audible_deals.client import LOCALE_DOMAIN
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
        all_asins.extend(_resolve_last_references(last_refs))

    if len(all_asins) < 2:
        raise click.UsageError("Provide at least 2 ASINs to compare.")

    for asin in all_asins:
        _validate_asin(asin)

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


def _wishlist_entry(product: Product, max_price: float | None) -> dict:
    """Build a wishlist dict from a Product."""
    return {
        "asin": product.asin,
        "title": product.title,
        "max_price": max_price,
        "added": product.release_date or "",
    }


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
        all_asins.extend(_resolve_last_references(last_refs))
    if not all_asins:
        raise click.UsageError("Provide at least one ASIN or use --last N.")

    items = _load_wishlist()
    existing = {item["asin"] for item in items}

    for asin in all_asins:
        _validate_asin(asin)

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
            items.append(_wishlist_entry(p, max_price))
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


@wishlist.command("sync")
@click.option("--max-price", type=float, default=None, help="Set target price for all synced items")
@click.pass_context
def wishlist_sync(ctx, max_price):
    """Sync your Audible account wishlist into the local watchlist.

    Fetches all items from your Audible account wishlist and adds any that
    are not already tracked locally. Existing local items are never removed.

    \b
    Examples:
        deals wishlist sync
        deals wishlist sync --max-price 5
    """
    dc = _get_client(ctx.obj["locale"])
    with dc:
        audible_items = dc.get_wishlist()

    local_items = _load_wishlist()
    existing_asins = {item["asin"] for item in local_items}

    added = 0
    skipped = 0
    for product in audible_items:
        if product.asin in existing_asins:
            skipped += 1
            continue
        local_items.append(_wishlist_entry(product, max_price))
        added += 1
        console.print(f"[green]+[/green] {product.title} ({product.asin})")

    _save_wishlist(local_items)
    console.print(
        f"\n[bold]{added}[/bold] synced, "
        f"{skipped} already tracked, "
        f"{len(local_items)} total on wishlist"
    )


def _parse_interval(value: str) -> int:
    """Parse an interval string into seconds. Accepts '30m', '2h', '1h30m', '90s', or a plain number (minutes)."""
    raw = value
    value = value.strip().lower()
    if value.isdigit():
        total = int(value) * 60
    else:
        total = 0
        for match in re.finditer(r"(\d+)\s*(h|m|s)", value):
            n, unit = int(match.group(1)), match.group(2)
            if unit == "h":
                total += n * 3600
            elif unit == "m":
                total += n * 60
            else:
                total += n
        # Reject input with unrecognized characters
        remainder = re.sub(r"\d+\s*(h|m|s)", "", value).strip()
        if remainder:
            raise click.BadParameter(f"Cannot parse interval '{raw}'. Use e.g. '30m', '2h', '1h30m'.")
    if total <= 0:
        raise click.BadParameter(f"Interval must be positive. Use e.g. '30m', '2h', '1h30m'.")
    return total


def _watch_once(ctx: click.Context) -> int:
    """Run a single wishlist price check. Returns the number of BUY hits."""
    items = _load_wishlist()
    if not items:
        console.print("[dim]Wishlist is empty. Use 'deals wishlist add ASIN' to add items.[/dim]")
        return 0

    dc = _get_client(ctx.obj["locale"])
    targets: dict[str, float | None] = {item["asin"]: item.get("max_price") for item in items}

    with dc:
        products = dc.get_products_batch([item["asin"] for item in items])

    found_asins = {p.asin for p in products}
    for item in items:
        if item["asin"] not in found_asins:
            console.print(f"[red]Not found: {item['asin']} ({item['title']})[/red]")

    if not products:
        return 0

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
    return hits


@cli.command()
@click.option("--every", default=None, help="Re-check on an interval (e.g. '30m', '2h', '1h30m'). Runs until interrupted.")
@click.pass_context
def watch(ctx, every):
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
    """
    if not every:
        _watch_once(ctx)
        return

    interval = _parse_interval(every)
    console.print(f"[dim]Watching every {every} (Ctrl+C to stop)...[/dim]\n")
    try:
        while True:
            _watch_once(ctx)
            console.print(f"\n  [dim]Next check in {every}... (Ctrl+C to stop)[/dim]\n")
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")


# ---------------------------------------------------------------------------
# Saved search profiles
# ---------------------------------------------------------------------------
PROFILES_FILE = CONFIG_DIR / "profiles.json"
LAST_RESULTS_FILE = CONFIG_DIR / "last_results.json"
CONFIG_FILE = CONFIG_DIR / "config.json"


def _load_profiles() -> dict[str, dict]:
    if PROFILES_FILE.exists():
        try:
            return json_mod.loads(PROFILES_FILE.read_text())
        except (json_mod.JSONDecodeError, KeyError):
            pass
    return {}


def _save_profiles(profiles: dict[str, dict]) -> None:
    _atomic_write(PROFILES_FILE, json_mod.dumps(profiles, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Global defaults config
# ---------------------------------------------------------------------------

_CONFIG_SCHEMA: dict[str, type] = {
    "skip_owned": bool, "max_price": float, "min_rating": float,
    "min_ratings": int, "min_hours": float, "language": str,
    "locale": str, "sort": str, "pages": int, "on_sale": bool,
    "deep": bool, "first_in_series": bool, "all_languages": bool,
    "interactive": bool, "limit": int, "narrator": str, "author": str,
}


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json_mod.loads(CONFIG_FILE.read_text())
        except (json_mod.JSONDecodeError, KeyError, OSError):
            pass
    return {}


def _save_config(cfg: dict) -> None:
    _atomic_write(CONFIG_FILE, json_mod.dumps(cfg, indent=2, ensure_ascii=False))


def _coerce_config_value(key: str, raw: str):
    """Coerce a raw string value to the type declared in _CONFIG_SCHEMA."""
    typ = _CONFIG_SCHEMA[key]
    if typ is bool:
        if raw.lower() in ("true", "1", "yes"):
            return True
        elif raw.lower() in ("false", "0", "no"):
            return False
        raise click.ClickException(f"Invalid boolean value for '{key}': {raw!r}. Use true/false.")
    try:
        return typ(raw)
    except (ValueError, TypeError) as e:
        raise click.ClickException(f"Invalid value for '{key}' (expected {typ.__name__}): {e}")


@cli.group("config")
def config_cmd():
    """Manage global defaults for deals commands."""


def _validate_config_key(key: str) -> str:
    """Normalize and validate a config key. Returns the snake_case key or raises."""
    norm = key.replace("-", "_")
    if norm not in _CONFIG_SCHEMA:
        valid = ", ".join(sorted(k.replace("_", "-") for k in _CONFIG_SCHEMA))
        raise click.ClickException(f"Unknown config key '{key}'. Valid keys: {valid}")
    return norm


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
    norm_key = _validate_config_key(key)
    coerced = _coerce_config_value(norm_key, value)
    cfg = _load_config()
    cfg[norm_key] = coerced
    _save_config(cfg)
    console.print(f"[green]Config set:[/green] {norm_key} = {coerced!r}")


@config_cmd.command("get")
@click.argument("key")
def config_get(key):
    """Get a global default value."""
    norm_key = _validate_config_key(key)
    cfg = _load_config()
    if norm_key not in cfg:
        console.print(f"[dim]{norm_key} is not set[/dim]")
    else:
        console.print(f"{norm_key} = {cfg[norm_key]!r}")


@config_cmd.command("list")
def config_list():
    """List all set global defaults."""
    cfg = _load_config()
    if not cfg:
        console.print("[dim]No global defaults set. Use 'deals config set KEY VALUE' to set one.[/dim]")
        return
    for k, v in sorted(cfg.items()):
        console.print(f"  {k} = {v!r}")


@config_cmd.command("reset")
@click.argument("key", required=False, default=None)
def config_reset(key):
    """Remove a key from global defaults, or clear all if no key given."""
    cfg = _load_config()
    if key is None:
        _save_config({})
        console.print("[green]All global defaults cleared.[/green]")
        return
    norm_key = _validate_config_key(key)
    if norm_key in cfg:
        del cfg[norm_key]
        _save_config(cfg)
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
@click.option("--exclude-author", "exclude_authors", multiple=True)
@click.option("--exclude-narrator", "exclude_narrators", multiple=True)
@click.option("--on-sale", is_flag=True, default=False)
@click.option("--deep", is_flag=True, default=False)
@click.option("--pages", type=int, default=None)
@click.option("--first-in-series", is_flag=True, default=False)
@click.option("--all-languages", is_flag=True, default=False)
@click.option("--limit", "-n", type=int, default=None)
@click.option("--skip-owned", is_flag=True, default=False)
@click.option("--language", default="")
@click.option("--interactive", "-i", is_flag=True, default=False)
def profile_save(name, **kwargs):
    """Save a search profile.

    \b
    Example:
        deals profile save my-scifi --genre sci-fi --max-price 5 --min-rating 4 --first-in-series
        deals profile save work --skip-owned --language english --interactive
        deals find --profile my-scifi
        deals search "Brandon Sanderson" --profile my-scifi
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


@profile.command("show")
@click.argument("name")
def profile_show(name):
    """Show the saved flags for a named profile."""
    profiles = _load_profiles()
    if name not in profiles:
        raise click.ClickException(f"Profile '{name}' not found.")
    opts = profiles[name]
    console.print(f"\n[bold]Profile: {name}[/bold]\n")
    for key, value in sorted(opts.items()):
        display_key = key.replace("_", "-")
        if isinstance(value, bool) and value:
            console.print(f"  --{display_key}")
        elif isinstance(value, (list, tuple)):
            for v in value:
                console.print(f"  --{display_key} {v}")
        elif not isinstance(value, bool):
            console.print(f"  --{display_key} {value}")
    console.print()


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
        entries.append({"date": today, "price": round(p.price, 2), "title": p.title})
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
    drops: list[tuple[str, str, float, float]] = []  # (asin, title, old_price, new_price)
    new_items: list[tuple[str, str, float]] = []  # (asin, title, price)

    for hist_file in HISTORY_DIR.glob("*.json"):
        asin = hist_file.stem
        try:
            entries = json_mod.loads(hist_file.read_text())
        except json_mod.JSONDecodeError:
            continue
        if not entries:
            continue

        # Extract title from the most recent entry that has one
        title = ""
        for e in reversed(entries):
            if e.get("title"):
                title = e["title"]
                break

        recent = [e for e in entries if e["date"] >= cutoff]
        if not recent:
            continue

        # New item: first entry is within the window
        if entries[0]["date"] >= cutoff and len(entries) == len(recent):
            new_items.append((asin, title, entries[-1]["price"]))
            continue

        # Price drop: compare earliest in-window to latest
        before = [e for e in entries if e["date"] < cutoff]
        if before and recent:
            old_price = before[-1]["price"]
            new_price = recent[-1]["price"]
            if new_price < old_price:
                drops.append((asin, title, old_price, new_price))

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

    def _label(asin: str, title: str) -> str:
        if not title:
            return asin
        t = title if len(title) <= 40 else title[:37] + "..."
        return f"{t}  {asin}"

    if drops:
        console.print(f"  [green]Price drops: {len(drops)}[/green]")
        for asin, title, old, new in sorted(drops, key=lambda x: x[2] - x[3], reverse=True)[:10]:
            console.print(f"    {_label(asin, title)}  ${old:.2f} -> [green]${new:.2f}[/green]  ([green]-${old - new:.2f}[/green])")
    else:
        console.print("  [dim]No price drops[/dim]")

    if new_items:
        console.print(f"\n  [cyan]Newly tracked: {len(new_items)}[/cyan]")
        for asin, title, price in new_items[:10]:
            console.print(f"    [dim]{_label(asin, title)}  ${price:.2f}[/dim]")
    if wishlist_hits:
        console.print(f"\n  [bold green]Wishlist items at target: {len(wishlist_hits)}[/bold green]")
        for item in wishlist_hits:
            console.print(f"    {item['asin']}  {item['title']}")

    if not drops and not new_items and not wishlist_hits:
        console.print("  [dim]Nothing to report.[/dim]")
    console.print()


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
