"""CLI for finding Audible audiobook deals.

Usage:
    deals login                    Authenticate with Audible
    deals import-auth PATH         Import auth from audible-cli or Libation
    deals categories [--parent ID] List categories
    deals search QUERY [options]   Search catalog with filters
    deals find [options]           Browse & filter deals (main command)
    deals detail ASIN              Show detailed product info
    deals compare ASIN ASIN ...    Side-by-side comparison
    deals wishlist add/remove/list Manage your watchlist
    deals watch                    Check wishlist for price drops
    deals history ASIN             View price history
    deals completions SHELL        Generate shell completions
"""

from __future__ import annotations

import csv
import datetime
import json as json_mod
import math
import readline  # noqa: F401 — required on macOS for input() with long strings
import sys
from dataclasses import asdict
from pathlib import Path

import click
from rich.table import Table

from audible_deals.client import AUTH_FILE, CONFIG_DIR, DealsClient, Product
from audible_deals.display import (
    console,
    create_scan_progress,
    display_categories,
    display_comparison,
    display_product_detail,
    display_products,
    display_summary,
)

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
    min_hours: float = 0.0,
    language: str = "",
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

    if min_hours > 0:
        filtered = [p for p in filtered if p.hours >= min_hours]

    if language:
        lang_lower = language.lower()
        filtered = [p for p in filtered if p.language.lower() == lang_lower]

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
    min_hours: float,
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
) -> None:
    """Shared post-processing pipeline for search and find commands."""
    filtered, excluded = _filter_products(
        all_products,
        max_price=max_price,
        min_rating=min_rating,
        min_hours=min_hours,
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
                        editions_removed=editions_removed, series_collapsed=series_collapsed)


@click.group()
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
        cats = dc.get_categories(root=parent)

    title = "Subcategories" if parent else "Top-Level Categories"
    display_categories(cats, title=title)
    console.print(
        "\n  [dim]Tip: use --parent ID to see subcategories, "
        "or pass the ID to 'deals find --category ID'[/dim]"
    )


@cli.command()
@click.argument("query")
@click.option("--max-price", type=float, default=None, help="Max price filter (e.g. 5.00)")
@click.option("--category", default="", help="Category ID to search within")
@click.option("--genre", default="", help="Genre name to search within (fuzzy match, e.g. 'sci-fi')")
@click.option("--exclude-genre", multiple=True, help="Genre(s) to exclude (repeatable, fuzzy match)")
@click.option("--sort", type=click.Choice(list(SORT_OPTIONS.keys()) + ["price", "-price", "discount", "price-per-hour"]), default="relevance", help="Sort order (price/discount/price-per-hour are client-side)")
@click.option("--min-rating", type=float, default=0.0, help="Minimum rating (e.g. 4.0)")
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours")
@click.option("--on-sale", is_flag=True, default=False, help="Only show discounted items")
@click.option("--pages", type=int, default=3, help="Number of pages to scan (50 items/page)")
@click.option("--language", default="", help="Language filter (e.g. english)")
@click.option("--all-languages", is_flag=True, default=False, help="Include all languages (default: locale language only)")
@click.option("--first-in-series", is_flag=True, default=False, help="Show only the first book per series")
@click.option("--skip-owned", is_flag=True, default=False, help="Exclude books already in your library")
@click.option("--limit", "-n", type=int, default=None, help="Show only the top N results")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None, help="Export results to file (.json or .csv)")
@click.option("--json", "json_flag", is_flag=True, default=False, help="Output results as JSON to stdout")
@click.option("--quiet", "-q", is_flag=True, default=False, help="Suppress table output (useful with --output)")
@click.pass_context
def search(ctx, query, max_price, category, genre, exclude_genre, sort, min_rating, min_hours, on_sale, pages, language, all_languages, first_in_series, skip_owned, limit, output, json_flag, quiet):
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
            category_name = dc.get_category_name(category)
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

    _postprocess_and_output(
        all_products,
        title=f'Search: "{query}"',
        max_price=max_price, min_rating=min_rating, min_hours=min_hours,
        language=language, on_sale=on_sale, skip_asins=skip_asins,
        exclude_category_ids=exclude_category_ids,
        first_in_series=first_in_series, sort=sort, limit=limit,
        output=output, json_flag=json_flag, quiet=quiet,
    )


@cli.command()
@click.option("--category", default="", help="Category ID (use 'deals categories' to find IDs)")
@click.option("--genre", default="", help="Genre name (fuzzy match, e.g. 'sci-fi', 'mystery', 'romance')")
@click.option("--exclude-genre", multiple=True, help="Genre(s) to exclude (repeatable, fuzzy match)")
@click.option("--keywords", default="", help="Optional keyword filter within the category")
@click.option("--max-price", type=float, default=5.00, help="Max price threshold (default: $5.00)")
@click.option("--sort", type=click.Choice(["price", "-price", "discount", "price-per-hour"] + list(SORT_OPTIONS.keys())), default="price", help="Sort order (price/discount/price-per-hour are client-side)")
@click.option("--min-rating", type=float, default=0.0, help="Minimum rating (e.g. 4.0)")
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours (filters out shorts)")
@click.option("--on-sale", is_flag=True, default=False, help="Only show discounted items")
@click.option("--deep", is_flag=True, default=False, help="Scan with 3 sort orders for better coverage (3x API calls)")
@click.option("--pages", type=int, default=10, help="Pages to scan per sort order (50 items/page, default: 10)")
@click.option("--language", default="", help="Language filter (e.g. english)")
@click.option("--all-languages", is_flag=True, default=False, help="Include all languages (default: locale language only)")
@click.option("--first-in-series", is_flag=True, default=False, help="Show only the first book per series")
@click.option("--skip-owned", is_flag=True, default=False, help="Exclude books already in your library")
@click.option("--limit", "-n", type=int, default=None, help="Show only the top N results")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None, help="Export results to file (.json or .csv)")
@click.option("--json", "json_flag", is_flag=True, default=False, help="Output results as JSON to stdout")
@click.option("--quiet", "-q", is_flag=True, default=False, help="Suppress table output (useful with --output)")
@click.pass_context
def find(ctx, category, genre, exclude_genre, keywords, max_price, sort, min_rating, min_hours, on_sale, deep, pages, language, all_languages, first_in_series, skip_owned, limit, output, json_flag, quiet):
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
        deals find --keywords "space opera" --max-price 3 --min-rating 4
    """
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
            category_name = dc.get_category_name(category)
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

    find_title = f"Deals under ${max_price:.2f}"
    if keywords:
        find_title += f' matching "{keywords}"'
    _postprocess_and_output(
        all_products,
        title=find_title,
        max_price=max_price, min_rating=min_rating, min_hours=min_hours,
        language=language, on_sale=on_sale, skip_asins=skip_asins,
        exclude_category_ids=exclude_category_ids,
        first_in_series=first_in_series, sort=sort, limit=limit,
        output=output, json_flag=json_flag, quiet=quiet,
    )


@cli.command()
@click.argument("asin")
@click.pass_context
def detail(ctx, asin):
    """Show detailed info for a product by ASIN."""
    dc = _get_client(ctx.obj["locale"])
    with dc:
        try:
            product = dc.get_product(asin)
        except ValueError as e:
            raise click.ClickException(str(e))

    display_product_detail(product)


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
    WISHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    WISHLIST_FILE.write_text(json_mod.dumps(items, indent=2, ensure_ascii=False))


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

    hits = 0
    for p in products:
        target = targets.get(p.asin)
        target_str = f"${target:.2f}" if target else "-"
        p_str = f"${p.price:.2f}" if p.price is not None else "-"
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
        path.write_text(json_mod.dumps(entries))


@cli.command()
@click.argument("asin")
def history(asin):
    """Show price history for an ASIN.

    History is recorded automatically each time an ASIN appears in
    search/find results. Use 'deals history ASIN' to view past prices.
    """
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

    table = Table(title=f"Price History: {asin}", show_lines=False, padding=(0, 1), title_style="bold")
    table.add_column("Date", width=12)
    table.add_column("Price", justify="right", width=10)
    table.add_column("Change", justify="right", width=10)

    prev_price = None
    for entry in entries:
        price = entry["price"]
        p_str = f"${price:.2f}"
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
        table.add_row(entry["date"], p_str, change)
        prev_price = price

    console.print(table)

    low = min(e["price"] for e in entries)
    high = max(e["price"] for e in entries)
    current = entries[-1]["price"]
    console.print(f"\n  Low: [green]${low:.2f}[/green]  High: [red]${high:.2f}[/red]  Current: ${current:.2f}")


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

    # Find the deals entry point script
    deals_bin = shutil.which("deals")
    if not deals_bin:
        # Fall back to running via python -m
        deals_bin = f"{sys.executable} -m audible_deals"

    env_var = f"_DEALS_COMPLETE={shell}_source"
    result = subprocess.run(
        ["/bin/sh", "-c", f"{env_var} {deals_bin}"],
        capture_output=True,
        text=True,
    )
    if result.stdout:
        click.echo(result.stdout)
    else:
        click.echo(result.stderr, err=True)
