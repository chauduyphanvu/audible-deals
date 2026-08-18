"""Notify command wiring."""

from __future__ import annotations

import json as json_mod
from pathlib import Path

import click

from audible_deals.cli.helpers import (
    _credit_price,
    _currency,
    _get_client,
    _safe_record_prices,
)
from audible_deals.constants import LockHeldError, run_lock
from audible_deals.notification_workflow import parse_webhook_headers, run_notify


@click.command()
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
@click.option(
    "--webhook-header",
    "webhook_headers",
    multiple=True,
    metavar="'NAME: VALUE'",
    help="Extra header for webhook POST (repeatable; requires --webhook)",
)
@click.pass_context
def notify(
    ctx, webhook, webhook_format, webhook_template, exit_code, cooldown, webhook_headers
):
    """Check wishlist and send notifications for items at target price.

    \b
    Examples:
        deals notify --webhook https://hooks.slack.com/services/...
        deals notify --webhook https://hooks.slack.com/... --webhook-format slack
        deals notify  (prints to stdout as JSON, useful for cron + mail)
    """
    if webhook_headers and not webhook:
        raise click.UsageError("--webhook-header requires --webhook")
    extra_headers = parse_webhook_headers(webhook_headers) if webhook_headers else {}
    try:
        with run_lock():
            had_hits = run_notify(
                webhook,
                webhook_format,
                webhook_template,
                cooldown,
                extra_headers,
                locale=ctx.obj["locale"],
                currency=_currency(ctx),
                credit_price=_credit_price(ctx),
                get_client=_get_client,
                record_products=_safe_record_prices,
            )
            if exit_code and had_hits is not None:
                ctx.exit(0 if had_hits else 1)
    except LockHeldError:
        click.echo("Another deals notify/recap run is in progress — exiting.", err=True)
        if not webhook:
            click.echo(json_mod.dumps({"deals": [], "count": 0}, indent=2))
        if exit_code:
            ctx.exit(1)
