"""Recap command wiring."""

from __future__ import annotations

import json as json_mod

import click

from audible_deals.cli.helpers import _currency
from audible_deals.constants import LockHeldError, run_lock
from audible_deals.notification_workflow import (
    empty_recap_payload,
    parse_webhook_headers,
    run_recap,
)
from audible_deals.validation import validate_webhook_url


@click.command()
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
    "--atl-all",
    "atl_all",
    is_flag=True,
    default=False,
    help="Include ALL tracked items at all-time low, not just wishlist",
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
@click.option(
    "--webhook-header",
    "webhook_headers",
    multiple=True,
    metavar="'NAME: VALUE'",
    help="Extra header for webhook POST (repeatable; requires --webhook)",
)
@click.pass_context
def recap(
    ctx,
    days,
    show_new,
    atl,
    atl_all,
    json_flag,
    webhook,
    webhook_format,
    webhook_headers,
):
    """Show a recap of price changes across tracked items.

    Scans price history files and reports items that dropped in price,
    new items tracked, and wishlist items at target.
    """
    if webhook_headers and not webhook:
        raise click.UsageError("--webhook-header requires --webhook")
    if json_flag and webhook:
        raise click.UsageError("--json and --webhook are mutually exclusive")
    if webhook:
        validate_webhook_url(webhook)
    extra_headers = parse_webhook_headers(webhook_headers) if webhook_headers else {}
    try:
        with run_lock():
            run_recap(
                days,
                show_new,
                atl,
                atl_all,
                json_flag,
                webhook,
                webhook_format,
                _currency(ctx),
                extra_headers,
            )
    except LockHeldError:
        click.echo("Another deals notify/recap run is in progress — exiting.", err=True)
        if json_flag:
            empty = empty_recap_payload(days, atl or atl_all)
            click.echo(json_mod.dumps(empty, indent=2))
