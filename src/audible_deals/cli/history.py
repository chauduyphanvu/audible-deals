"""Price history command."""

from __future__ import annotations

import json as json_mod
import logging

import click

from audible_deals.cli.helpers import (
    _currency,
    _resolve_cli_selectors,
)
from audible_deals.constants import LOCALE_CURRENCY
from audible_deals.display import console, display_price_history
from audible_deals.price_history import (
    load_all_price_histories,
    load_price_history,
    purge_stale_history,
)

logger = logging.getLogger(__name__)


@click.command()
@click.argument("asin", required=False, default=None, metavar="SELECTOR")
@click.option(
    "--last",
    "last_ref",
    type=str,
    default=None,
    help="Use result #N from the last result session",
)
@click.option(
    "--json", "json_flag", is_flag=True, default=False, help="Emit raw entries as JSON"
)
@click.option(
    "--all",
    "all_flag",
    is_flag=True,
    default=False,
    help="Emit all ASIN histories as JSON (requires --json)",
)
@click.option(
    "--purge-older-than",
    "purge_days",
    type=click.IntRange(min=1),
    default=None,
    help="Delete history files whose last entry is older than DAYS days",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be purged without deleting",
)
@click.option(
    "--yes", is_flag=True, default=False, help="Skip confirmation prompt for purge"
)
@click.pass_context
def history(ctx, asin, last_ref, json_flag, all_flag, purge_days, dry_run, yes):
    """Show price history for an ASIN, Audible URL, or @N selector.

    History is recorded automatically each time an ASIN appears in
    search/find results. Use 'deals history SELECTOR' to view past prices.
    """
    if dry_run and purge_days is None:
        raise click.UsageError("--dry-run requires --purge-older-than")
    if yes and purge_days is None:
        raise click.UsageError("--yes requires --purge-older-than")

    if purge_days is not None:
        if json_flag or all_flag or asin or last_ref:
            raise click.UsageError(
                "--purge-older-than cannot be combined with --json, --all, SELECTOR, or --last."
            )
        locale = ctx.obj["locale"]
        count, affected = purge_stale_history(purge_days, dry_run=True, locale=locale)
        if count == 0:
            console.print(
                f"[dim]No history files older than {purge_days} days found.[/dim]"
            )
            return
        if dry_run:
            examples = ", ".join(affected[:5])
            suffix = f" (e.g. {examples})" if affected else ""
            console.print(
                f"[dim]Would remove {count} stale history file(s) (>{purge_days} days since last entry){suffix}.[/dim]"
            )
            return
        if not yes:
            click.confirm(
                f"Remove {count} history file(s) older than {purge_days} days?",
                abort=True,
            )
        actual_count, _ = purge_stale_history(purge_days, locale=locale, asins=affected)
        console.print(
            f"[green]Removed {actual_count} stale history files (>{purge_days} days since last entry).[/green]"
        )
        return

    if all_flag:
        if not json_flag:
            raise click.UsageError("--all requires --json.")
        if asin or last_ref:
            raise click.UsageError("--all cannot be combined with an ASIN or --last.")
        click.echo(
            json_mod.dumps(
                load_all_price_histories(ctx.obj["locale"]),
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    if not asin and last_ref is None:
        raise click.UsageError("Provide an ASIN, Audible URL, @N, or use --last N.")
    resolved, locale = _resolve_cli_selectors(
        ctx,
        (asin,) if asin else (),
        (last_ref,) if last_ref is not None else (),
        single=True,
        announce=not json_flag,
    )
    asin = resolved[0].asin
    entries = load_price_history(asin, locale)
    if json_flag:
        click.echo(json_mod.dumps(entries, indent=2, ensure_ascii=False))
        return
    if not entries:
        console.print(
            f"[dim]No price history for {asin}. "
            "History is recorded when items appear in search/find results.[/dim]"
        )
        return

    cur = LOCALE_CURRENCY.get(locale, _currency(ctx))
    display_price_history(entries, asin, cur)
