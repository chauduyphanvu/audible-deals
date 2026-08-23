"""Wishlist management commands and the watch command."""

from __future__ import annotations

import csv
import json as json_mod
import logging
import math
import sys
import time
from pathlib import Path

import click

from audible_deals.cli.helpers import (
    _credit_price,
    _currency,
    _get_client,
    _resolve_cli_selectors,
)
from audible_deals.validation import NONNEGATIVE_FLOAT
from audible_deals.filtering import sort_local
from audible_deals.parsing import parse_interval
from audible_deals.presentation.common import price_str
from audible_deals.presentation.reports import display_watch_table, display_wishlist
from audible_deals.presentation.terminal import console, safe_markup, safe_text
from audible_deals.serialization import sanitize_csv_cell, validate_export_path
from audible_deals.result_publication import record_prices_safely as _safe_record_prices
from audible_deals.wishlist import (
    load_wishlist,
    partition_wishlist,
    WishlistMutationError,
    warn_wishlist_issues,
)
from audible_deals.wishlist_service import (
    add_author_watch,
    add_products,
    plan_owned_purge,
    plan_product_add,
    plan_repair,
    purge_confirmed_asins,
    remove_entries,
    repair_wishlist,
    sync_products,
    update_targets,
)

logger = logging.getLogger(__name__)


def _wishlist_operation(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except WishlistMutationError as exc:
        raise click.ClickException(str(exc)) from exc


def _finite_max_price(ctx, param, value):
    if value is not None and not math.isfinite(value):
        raise click.BadParameter("must be a finite number", param=param)
    return value


@click.group(invoke_without_command=True)
@click.pass_context
def wishlist(ctx):
    """Manage your audiobook wishlist."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(wishlist_list)


def _add_author_watch(ctx, author: str, max_price: float) -> None:
    """Add an author watch to the wishlist unless one already exists."""
    result = _wishlist_operation(add_author_watch, author, max_price)
    warn_wishlist_issues(result.issues)
    if not result.added:
        console.print(f"[dim]Already watching author: {safe_markup(author)}[/dim]")
        return
    console.print(
        f"[green]+[/green] Author watch: {safe_markup(author)} "
        f"(target {price_str(max_price, _currency(ctx))})"
    )


@wishlist.command("add")
@click.argument("asins", nargs=-1, required=False, metavar="SELECTOR...")
@click.option(
    "--max-price",
    type=NONNEGATIVE_FLOAT,
    default=None,
    callback=_finite_max_price,
    help="Alert when price drops below this",
)
@click.option(
    "--last",
    "last_refs",
    type=str,
    multiple=True,
    help="Use result #N from the last result session (repeatable)",
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
        deals wishlist add @1-3,5 --max-price 5
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

    resolved, locale = _resolve_cli_selectors(ctx, asins, last_refs)
    all_asins = [item.asin for item in resolved]
    if not all_asins:
        raise click.UsageError("Provide at least one ASIN or --author or use --last N.")

    plan = _wishlist_operation(plan_product_add, all_asins)
    warn_wishlist_issues(plan.issues)
    for asin in plan.already_present:
        console.print(f"[dim]{safe_markup(asin)} already on wishlist[/dim]")

    fetched = []
    if plan.pending_asins:
        dc = _get_client(locale)
        with dc:
            products = dc.get_products_batch(list(plan.pending_asins))
            by_asin = {product.asin: product for product in products}
            for asin in plan.pending_asins:
                product = by_asin.get(asin)
                if product is None:
                    console.print(f"[red]Not found: {safe_markup(asin)}[/red]")
                else:
                    fetched.append(product)

    if fetched:
        result = _wishlist_operation(add_products, fetched, max_price, locale=locale)
        for event in result.events:
            product = event.product
            if event.action == "raced":
                console.print(
                    f"[dim]{safe_markup(product.asin)} already on wishlist[/dim]"
                )
            else:
                console.print(
                    f"[green]+[/green] {safe_markup(product.title)} "
                    f"({safe_markup(product.asin)})"
                )
        added = len(result.added_products)
        valid_total = result.valid_total
    else:
        added = 0
        valid_total = plan.valid_total
    console.print(f"\n[bold]{added}[/bold] added, {valid_total} total on wishlist")


@wishlist.command("remove")
@click.argument("asins", nargs=-1, required=False, metavar="SELECTOR...")
@click.option(
    "--last",
    "last_refs",
    type=str,
    multiple=True,
    help="Use result #N from the last result session (repeatable)",
)
@click.option(
    "--author",
    default=None,
    help="Remove an author watch by name (case-insensitive)",
)
@click.pass_context
def wishlist_remove(ctx, asins, last_refs, author):
    """Remove ASINs or an author watch from your wishlist."""
    resolved, _ = _resolve_cli_selectors(ctx, asins, last_refs)
    all_asins = [item.asin for item in resolved]
    if not all_asins and not author:
        raise click.UsageError("Provide at least one ASIN, --author, or use --last N.")
    result = _wishlist_operation(remove_entries, all_asins, author=author)
    warn_wishlist_issues(result.issues)
    console.print(
        f"[bold]{result.removed}[/bold] removed, {result.remaining} remaining"
    )


@wishlist.command("update")
@click.argument("asins", nargs=-1, required=False, metavar="SELECTOR...")
@click.option(
    "--last",
    "last_refs",
    type=str,
    multiple=True,
    help="Use result #N from the last result session (repeatable)",
)
@click.option(
    "--max-price",
    type=NONNEGATIVE_FLOAT,
    default=None,
    callback=_finite_max_price,
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
    resolved, _ = _resolve_cli_selectors(ctx, asins, last_refs)
    all_asins = [item.asin for item in resolved]
    if not all_asins:
        raise click.UsageError("Provide at least one ASIN or use --last N.")

    if max_price is not None and clear_target:
        raise click.UsageError("Use either --max-price or --clear-target, not both.")
    if max_price is None and not clear_target:
        raise click.UsageError("Provide --max-price or --clear-target.")

    cur = _currency(ctx)

    target = None if clear_target else max_price
    result = _wishlist_operation(update_targets, all_asins, target)
    warn_wishlist_issues(result.issues)
    for event in result.events:
        change = event.change
        if change is None:
            console.print(f"[red]Not on wishlist: {safe_markup(event.asin)}[/red]")
            continue
        if clear_target:
            console.print(
                f"[yellow]~[/yellow] {safe_markup(change.title)} "
                f"({safe_markup(change.asin)}) → target cleared"
            )
        else:
            console.print(
                f"[yellow]~[/yellow] {safe_markup(change.title)} "
                f"({safe_markup(change.asin)}) → target "
                f"{price_str(change.max_price, cur)}"
            )
    console.print(
        f"\n[bold]{len(result.changes)}[/bold] updated, "
        f"{len(result.not_found_asins)} not found"
    )


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
            json_mod.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
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
            writer.writerows(
                {key: sanitize_csv_cell(value) for key, value in row.items()}
                for row in rows
            )
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
    validate_export_path(output)
    cur = _currency(ctx)
    items = load_wishlist()
    asin_items, author_items = partition_wishlist(items)

    total = None
    if output:
        total = _export_wishlist(asin_items, author_items, output)

    if json_flag:
        console.file = sys.stderr
        payload = {"items": asin_items, "author_watches": author_items}
        click.echo(
            json_mod.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
        )
        if output:
            console.print(
                f"[green]Exported {total} entries to {safe_markup(output)}[/green]"
            )
        return

    if output:
        console.print(
            f"[green]Exported {total} entries to {safe_markup(output)}[/green]"
        )
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
    type=NONNEGATIVE_FLOAT,
    default=None,
    callback=_finite_max_price,
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

    cur = _currency(ctx)

    result = _wishlist_operation(sync_products, audible_items, max_price, update=update)
    warn_wishlist_issues(result.issues)
    for change in result.changes:
        product = change.product
        if change.action == "updated":
            console.print(
                f"[yellow]~[/yellow] {safe_markup(product.title)} "
                f"({safe_markup(product.asin)}) → target {price_str(max_price, cur)}"
            )
        else:
            console.print(
                f"[green]+[/green] {safe_markup(product.title)} "
                f"({safe_markup(product.asin)})"
            )
    console.print(
        f"\n[bold]{result.added}[/bold] synced, "
        f"{result.updated} updated, "
        f"{result.skipped} already tracked, "
        f"{result.valid_total} total on wishlist"
    )


@wishlist.command("repair")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show invalid entries without changing the wishlist",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt",
)
def wishlist_repair(dry_run, yes):
    """Remove invalid entries after creating a backup.

    Malformed JSON and non-list wishlist files are refused. Valid entries keep
    their original data and order.

    \b
    Examples:
        deals wishlist repair --dry-run
        deals wishlist repair
        deals wishlist repair --yes
    """
    plan = _wishlist_operation(plan_repair)
    if not plan.issues:
        console.print("[dim]Wishlist has no invalid entries.[/dim]")
        return

    console.print(f"[bold]{len(plan.issues)} invalid wishlist entries:[/bold]")
    for issue in plan.issues:
        click.echo(f"  [{issue.index}] {safe_text(issue.reason)}")

    if dry_run:
        console.print(
            f"\n[dim]Dry run: would remove {len(plan.issues)} "
            "invalid entries. No files changed.[/dim]"
        )
        return

    if not yes:
        click.confirm(
            f"Remove {len(plan.issues)} invalid wishlist entries?",
            abort=True,
        )

    result = _wishlist_operation(repair_wishlist, plan)
    console.print(
        f"\n[green]Removed {result.removed} invalid entries.[/green] "
        f"Backup: {safe_markup(result.backup)}"
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

    plan = _wishlist_operation(plan_owned_purge)
    warn_wishlist_issues(plan.issues)
    if not plan.asin_items:
        console.print("[dim]Nothing to purge.[/dim]")
        return

    dc = _get_client(ctx.obj["locale"])
    with dc:
        owned_asins = dc.get_library_asins()

    to_remove = plan.owned_items(owned_asins)

    if not to_remove:
        console.print("[dim]Nothing to purge.[/dim]")
        return

    if dry_run:
        for item in to_remove:
            console.print(
                f"[dim]Would remove: {safe_markup(item.get('title', ''))} "
                f"({safe_markup(item.get('asin', ''))})[/dim]"
            )
        return

    if not yes:
        click.confirm(
            f"Remove {len(to_remove)} owned item(s) from wishlist?",
            abort=True,
        )

    result = _wishlist_operation(
        purge_confirmed_asins, (item["asin"] for item in to_remove)
    )
    console.print(
        f"\n[bold]{result.removed}[/bold] removed, "
        f"{result.remaining} remaining on wishlist"
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

    targets: dict[str, float | None] = {
        item["asin"]: item.get("max_price") for item in asin_items
    }
    by_locale: dict[str, list[dict]] = {}
    for item in asin_items:
        item_locale = item.get("locale", ctx.obj["locale"])
        by_locale.setdefault(item_locale, []).append(item)

    products = []
    for item_locale, locale_items in by_locale.items():
        dc = _get_client(item_locale)
        with dc:
            products.extend(
                dc.get_products_batch([item["asin"] for item in locale_items])
            )

    _safe_record_prices(products)
    found_asins = {p.asin for p in products}
    for item in asin_items:
        if item["asin"] not in found_asins:
            console.print(
                f"[red]Not found: {safe_markup(item['asin'])} "
                f"({safe_markup(item.get('title', ''))})[/red]"
            )

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
    console.print(
        f"[dim]Watching every {safe_markup(every)} (Ctrl+C to stop)...[/dim]\n"
    )
    try:
        while True:
            _watch_once(ctx, buy_only=buy_only, sort_by=sort_by, show_url=show_url)
            console.print(
                f"\n  [dim]Next check in {safe_markup(every)}... "
                "(Ctrl+C to stop)[/dim]\n"
            )
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
