"""Wishlist management commands and the watch command."""

from __future__ import annotations

import csv
import datetime
import json as json_mod
import logging
import sys
import time
from pathlib import Path

import click

from audible_deals.cli.helpers import (
    _collect_asins,
    _credit_price,
    _currency,
    _get_client,
    _safe_record_prices,
)
from audible_deals.display import (
    console,
    display_watch_table,
    display_wishlist,
    price_str,
)
from audible_deals.filtering import sort_local
from audible_deals.parsing import parse_interval
from audible_deals.validation import validate_asin
from audible_deals.wishlist import (
    load_wishlist,
    partition_wishlist,
    save_wishlist,
    wishlist_entry,
)

logger = logging.getLogger(__name__)


@click.group()
def wishlist():
    """Manage your audiobook wishlist."""


def _add_author_watch(ctx, author: str, max_price: float) -> None:
    """Add an author watch to the wishlist unless one already exists."""
    items = load_wishlist()
    author_lower = author.lower()
    if any(
        i.get("type") == "author" and i.get("author", "").lower() == author_lower
        for i in items
    ):
        console.print(f"[dim]Already watching author: {author}[/dim]")
        return
    items.append(
        {
            "type": "author",
            "author": author,
            "max_price": max_price,
            "added": datetime.date.today().isoformat(),
        }
    )
    save_wishlist(items)
    console.print(
        f"[green]+[/green] Author watch: {author} "
        f"(target {price_str(max_price, _currency(ctx))})"
    )


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
@click.option(
    "--author",
    default=None,
    help="Watch all titles by this author (use with --max-price)",
)
@click.pass_context
def wishlist_add(ctx, asins, max_price, last_refs, author):
    """Add ASINs (or an author watch) to your wishlist.

    \b
    Example:
        deals wishlist add B00R6S1RCY B00I2VWW5U --max-price 5
        deals wishlist add --last 1 --last 2 --max-price 5
        deals wishlist add --author "Brandon Sanderson" --max-price 5
    """
    if author:
        if asins or last_refs:
            raise click.UsageError(
                "--author cannot be combined with ASIN arguments or --last."
            )
        if max_price is None:
            raise click.UsageError("--max-price is required when using --author.")
        _add_author_watch(ctx, author, max_price)
        return

    all_asins = _collect_asins(asins, last_refs)
    if not all_asins:
        raise click.UsageError("Provide at least one ASIN or --author or use --last N.")

    items = load_wishlist()
    existing = {item["asin"] for item in items if item.get("asin")}

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
@click.option(
    "--author",
    default=None,
    help="Remove an author watch by name (case-insensitive)",
)
def wishlist_remove(asins, last_refs, author):
    """Remove ASINs or an author watch from your wishlist."""
    all_asins = _collect_asins(asins, last_refs)
    if not all_asins and not author:
        raise click.UsageError("Provide at least one ASIN, --author, or use --last N.")
    for asin in all_asins:
        validate_asin(asin)
    items = load_wishlist()
    before = len(items)
    if all_asins:
        remove_set = set(all_asins)
        items = [i for i in items if i.get("asin") not in remove_set]
    if author:
        author_lower = author.lower()
        items = [
            i
            for i in items
            if not (
                i.get("type") == "author"
                and i.get("author", "").lower() == author_lower
            )
        ]
    save_wishlist(items)
    removed = before - len(items)
    console.print(f"[bold]{removed}[/bold] removed, {len(items)} remaining")


@wishlist.command("update")
@click.argument("asins", nargs=-1, required=False)
@click.option(
    "--last",
    "last_refs",
    type=str,
    multiple=True,
    help="Use result #N from last search/find (repeatable)",
)
@click.option(
    "--max-price",
    type=click.FloatRange(min=0),
    default=None,
    help="Set the target price for the matching wishlist entries",
)
@click.option(
    "--clear-target",
    is_flag=True,
    default=False,
    help="Clear the target price (set to no target)",
)
@click.pass_context
def wishlist_update(ctx, asins, last_refs, max_price, clear_target):
    """Update the target price for wishlist items.

    \b
    Examples:
        deals wishlist update B00R6S1RCY --max-price 5
        deals wishlist update B00R6S1RCY B00I2VWW5U --max-price 3.99
        deals wishlist update B00R6S1RCY --clear-target
        deals wishlist update --last 1 --max-price 5
    """
    all_asins = _collect_asins(asins, last_refs)
    if not all_asins:
        raise click.UsageError("Provide at least one ASIN or use --last N.")

    if max_price is not None and clear_target:
        raise click.UsageError("Use either --max-price or --clear-target, not both.")
    if max_price is None and not clear_target:
        raise click.UsageError("Provide --max-price or --clear-target.")

    for asin in all_asins:
        validate_asin(asin)

    items = load_wishlist()
    by_asin = {item["asin"]: item for item in items if item.get("asin")}
    cur = _currency(ctx)

    updated = 0
    not_found = 0
    for asin in all_asins:
        if asin not in by_asin:
            console.print(f"[red]Not on wishlist: {asin}[/red]")
            not_found += 1
            continue
        entry = by_asin[asin]
        if clear_target:
            entry["max_price"] = None
            console.print(
                f"[yellow]~[/yellow] {entry['title']} ({asin}) → target cleared"
            )
        else:
            entry["max_price"] = max_price
            console.print(
                f"[yellow]~[/yellow] {entry['title']} ({asin}) → target {price_str(max_price, cur)}"
            )
        updated += 1

    save_wishlist(items)
    console.print(f"\n[bold]{updated}[/bold] updated, {not_found} not found")


_WISHLIST_CSV_FIELDS = ["type", "asin", "title", "author", "max_price", "added"]


def _export_wishlist(
    asin_items: list[dict],
    author_items: list[dict],
    path: Path,
) -> int:
    """Write wishlist to path as .json or .csv. Returns total entry count."""
    suffix = path.suffix.lower()
    total = len(asin_items) + len(author_items)

    if suffix == ".json":
        payload = {"items": asin_items, "author_watches": author_items}
        path.write_text(
            json_mod.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    elif suffix == ".csv":
        rows: list[dict] = []
        for item in asin_items:
            rows.append(
                {
                    "type": "item",
                    "asin": item.get("asin", ""),
                    "title": item.get("title", ""),
                    "author": "",
                    "max_price": item.get("max_price", ""),
                    "added": item.get("added", ""),
                }
            )
        for item in author_items:
            rows.append(
                {
                    "type": "author_watch",
                    "asin": "",
                    "title": "",
                    "author": item.get("author", ""),
                    "max_price": item.get("max_price", ""),
                    "added": item.get("added", ""),
                }
            )
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_WISHLIST_CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    else:
        raise click.BadParameter(
            f"Unsupported extension '{suffix}'. Use .json or .csv.",
            param_hint="--output",
        )
    return total


@wishlist.command("list")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Export wishlist to file (.json or .csv)",
)
@click.option(
    "--json",
    "json_flag",
    is_flag=True,
    default=False,
    help="Output wishlist as JSON to stdout",
)
@click.pass_context
def wishlist_list(ctx, output, json_flag):
    """Show your wishlist."""
    cur = _currency(ctx)
    items = load_wishlist()
    asin_items, author_items = partition_wishlist(items)

    if json_flag:
        console.file = sys.stderr
        payload = {"items": asin_items, "author_watches": author_items}
        click.echo(json_mod.dumps(payload, indent=2, ensure_ascii=False))
        return

    if output:
        total = _export_wishlist(asin_items, author_items, output)
        console.print(f"[green]Exported {total} entries to {output}[/green]")
        return

    if not asin_items and not author_items:
        console.print(
            "[dim]Wishlist is empty. Use 'deals wishlist add ASIN' to add items.[/dim]"
        )
        return

    display_wishlist(asin_items, author_items, cur)


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
    local_by_asin = {item["asin"]: item for item in local_items if item.get("asin")}
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
                    f"[yellow]~[/yellow] {product.title} ({product.asin}) → target {price_str(max_price, cur)}"
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


@wishlist.command("purge")
@click.option(
    "--owned",
    is_flag=True,
    default=False,
    help="Remove wishlist entries already owned in your Audible library",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be removed without saving",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt",
)
@click.pass_context
def wishlist_purge(ctx, owned, dry_run, yes):
    """Remove wishlist entries already owned in your Audible library.

    \b
    Examples:
        deals wishlist purge --owned
        deals wishlist purge --owned --dry-run
        deals wishlist purge --owned --yes
    """
    if not owned:
        raise click.UsageError("Specify --owned to purge owned items")

    items = load_wishlist()
    if not any(i.get("asin") for i in items):
        console.print("[dim]Nothing to purge.[/dim]")
        return

    dc = _get_client(ctx.obj["locale"])
    with dc:
        owned_asins = dc.get_library_asins()

    to_remove = [i for i in items if i.get("asin") in owned_asins]
    kept = [i for i in items if i.get("asin") not in owned_asins]

    if not to_remove:
        console.print("[dim]Nothing to purge.[/dim]")
        return

    if dry_run:
        for item in to_remove:
            console.print(
                f"[dim]Would remove: {item.get('title', '')} ({item.get('asin', '')})[/dim]"
            )
        return

    if not yes:
        click.confirm(
            f"Remove {len(to_remove)} owned item(s) from wishlist?",
            abort=True,
        )

    save_wishlist(kept)
    console.print(
        f"\n[bold]{len(to_remove)}[/bold] removed, {len(kept)} remaining on wishlist"
    )


def _watch_once(
    ctx: click.Context,
    buy_only: bool = False,
    sort_by: str | None = None,
    show_url: bool = False,
) -> int:
    """Run a single wishlist price check. Returns the number of BUY hits."""
    items = load_wishlist()
    asin_items, author_items = partition_wishlist(items)
    if not asin_items and not author_items:
        console.print(
            "[dim]Wishlist is empty. Use 'deals wishlist add ASIN' to add items.[/dim]"
        )
        return 0

    if author_items and not asin_items:
        console.print(
            "[dim]Author watches are checked by 'deals notify'. Use 'deals notify' to see author hits.[/dim]"
        )
    elif author_items:
        console.print("[dim]Author watches are checked by 'deals notify' only.[/dim]")

    if not asin_items:
        return 0

    dc = _get_client(ctx.obj["locale"])
    targets: dict[str, float | None] = {
        item["asin"]: item.get("max_price") for item in asin_items
    }

    with dc:
        products = dc.get_products_batch([item["asin"] for item in asin_items])

    _safe_record_prices(products)
    found_asins = {p.asin for p in products}
    for item in asin_items:
        if item["asin"] not in found_asins:
            console.print(f"[red]Not found: {item['asin']} ({item['title']})[/red]")

    if not products:
        return 0

    if sort_by:
        products = sort_local(products, sort_by)

    cur = _currency(ctx)
    return display_watch_table(
        products, targets, cur, buy_only, show_url, credit_price=_credit_price(ctx)
    )


@click.command()
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
