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
from audible_deals.cli.options import json_option
from audible_deals.config_store import webhook_allow_private
from audible_deals.constants import WEBHOOK_FORMATS
from audible_deals.exit_codes import ExitCode
from audible_deals.locking import LockHeldError, run_lock
from audible_deals.notification_service import (
    NotificationRuntime,
    commit_notification_state,
    run_notification,
)
from audible_deals.notification_workflow import parse_webhook_headers
from audible_deals.presentation.terminal import console, route_console_to_stderr
from audible_deals.result_publication import record_prices_safely as _safe_record_prices
from audible_deals.validation import validate_webhook_url
from audible_deals.webhook_client import WebhookClient, WebhookDeliveryError
from audible_deals.wishlist import warn_wishlist_issues


@click.command()
@click.option("--webhook", default=None, help="Webhook URL to POST results to")
@click.option(
    "--webhook-format",
    type=click.Choice(WEBHOOK_FORMATS),
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
    help="Exit 0 if any items hit target, 10 if none",
)
@click.option(
    "--cooldown",
    type=click.IntRange(min=0),
    default=None,
    help=(
        "Suppress repeat notifications for N days unless the price drops further "
        "(default: off, and 0 means off)"
    ),
)
@click.option(
    "--webhook-header",
    "webhook_headers",
    multiple=True,
    metavar="'NAME: VALUE'",
    help="Extra header for webhook POST (repeatable; requires --webhook)",
)
@json_option(
    "Print the deals as JSON on stdout even when sending to a webhook "
    "(without --webhook this is always the case)"
)
@click.pass_context
def notify(
    ctx,
    webhook,
    webhook_format,
    webhook_template,
    exit_code,
    cooldown,
    webhook_headers,
    json_flag,
):
    """Check wishlist and send notifications for items at target price.

    Without --webhook the deals are printed to stdout as JSON
    ({"deals": [...], "count": N}), and always valid JSON, even when the
    wishlist is empty or nothing is at target. Messages go to stderr then.

    \b
    Examples:
        deals notify --webhook https://hooks.slack.com/services/...
        deals notify --webhook https://hooks.slack.com/... --webhook-format slack
        deals notify  (prints to stdout as JSON, useful for cron + mail)
        deals notify --webhook https://ntfy.sh/topic --json  (send and print)
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
    print_json = json_flag or not webhook
    if print_json:
        route_console_to_stderr(ctx)
    runtime = NotificationRuntime(
        get_client=_get_client,
        record_products=_safe_record_prices,
        webhook_client=WebhookClient(
            allow_private=webhook_allow_private(ctx.obj.get("config"))
        ),
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
                    cooldown=cooldown or None,
                    webhook_headers=extra_headers,
                ),
                runtime,
            )
            warn_wishlist_issues(result.wishlist_issues)
            if result.empty_wishlist:
                console.print(
                    "[dim]Wishlist is empty. Use 'deals wishlist add' first.[/dim]"
                )
            elif not result.hits and webhook:
                if result.suppressed:
                    console.print(
                        f"[dim]{result.suppressed} deal(s) suppressed by cooldown. Nothing sent.[/dim]"
                    )
                else:
                    console.print(
                        "[dim]No items at target price. Nothing sent to webhook.[/dim]"
                    )
            elif result.hits and webhook:
                console.print(
                    f"[green]Sent {len(result.hits)} deal(s) to webhook[/green]"
                )
            if print_json:
                _print_deals([hit.to_dict() for hit in result.hits])
            commit_notification_state(result, runtime)
            if exit_code:
                ctx.exit(ExitCode.OK if result.had_hits else ExitCode.NO_MATCH)
    except LockHeldError:
        # Like `track run`: a busy lock is not a failure and not "no deals",
        # so a cron job exits 0 and the next run picks up.
        click.echo(
            "Note: another deals notify/recap run is in progress; exiting.", err=True
        )
        if print_json:
            _print_deals([])
    except (ValueError, WebhookDeliveryError) as exc:
        raise click.ClickException(str(exc)) from exc


def _print_deals(deals: list[dict]) -> None:
    """The documented ``deals notify`` output (schema version 1)."""
    click.echo(
        json_mod.dumps({"deals": deals, "count": len(deals)}, indent=2, allow_nan=False)
    )
