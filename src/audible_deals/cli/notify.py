"""Notify command wiring."""

from __future__ import annotations

import json as json_mod
from pathlib import Path

import click

from audible_deals.automation_models import NotificationRequest
from audible_deals.cli.helpers import (
    _credit_price,
    _currency,
    _get_client,
)
from audible_deals.locking import LockHeldError, run_lock
from audible_deals.notification_service import (
    NotificationRuntime,
    commit_notification_state,
    run_notification,
)
from audible_deals.notification_workflow import parse_webhook_headers
from audible_deals.presentation.terminal import console
from audible_deals.result_publication import record_prices_safely as _safe_record_prices
from audible_deals.validation import validate_webhook_url
from audible_deals.webhook_client import WebhookClient, WebhookDeliveryError
from audible_deals.wishlist import warn_wishlist_issues


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
    if webhook_template is not None and webhook_format != "generic":
        raise click.UsageError(
            "--webhook-template and --webhook-format are mutually exclusive"
        )
    if webhook_template is not None and not webhook:
        raise click.UsageError("--webhook-template requires --webhook")
    if webhook:
        validate_webhook_url(webhook)
    extra_headers = parse_webhook_headers(webhook_headers) if webhook_headers else {}
    runtime = NotificationRuntime(
        get_client=_get_client,
        record_products=_safe_record_prices,
        webhook_client=WebhookClient(),
    )
    try:
        with run_lock():
            result = run_notification(
                NotificationRequest(
                    locale=ctx.obj["locale"],
                    currency=_currency(ctx),
                    credit_price=_credit_price(ctx),
                    webhook=webhook,
                    webhook_format=webhook_format,
                    webhook_template=webhook_template,
                    cooldown=cooldown,
                    webhook_headers=extra_headers,
                ),
                runtime,
            )
            warn_wishlist_issues(result.wishlist_issues)
            if result.empty_wishlist:
                console.print(
                    "[dim]Wishlist is empty. Use 'deals wishlist add' first.[/dim]"
                )
            elif not result.hits:
                if not webhook:
                    click.echo(
                        json_mod.dumps(
                            {"deals": [], "count": 0}, indent=2, allow_nan=False
                        )
                    )
                elif result.suppressed:
                    console.print(
                        f"[dim]{result.suppressed} deal(s) suppressed by cooldown. Nothing sent.[/dim]"
                    )
                else:
                    console.print(
                        "[dim]No items at target price. Nothing sent to webhook.[/dim]"
                    )
            elif webhook:
                console.print(
                    f"[green]Sent {len(result.hits)} deal(s) to webhook[/green]"
                )
            else:
                deals = [hit.to_dict() for hit in result.hits]
                click.echo(
                    json_mod.dumps(
                        {"deals": deals, "count": len(deals)},
                        indent=2,
                        allow_nan=False,
                    )
                )
            commit_notification_state(result, runtime)
            if exit_code:
                ctx.exit(0 if result.had_hits else 1)
    except LockHeldError:
        click.echo("Another deals notify/recap run is in progress — exiting.", err=True)
        if not webhook:
            click.echo(
                json_mod.dumps({"deals": [], "count": 0}, indent=2, allow_nan=False)
            )
        if exit_code:
            ctx.exit(1)
    except (ValueError, WebhookDeliveryError) as exc:
        raise click.ClickException(str(exc)) from exc
