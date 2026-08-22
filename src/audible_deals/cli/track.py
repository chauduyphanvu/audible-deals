"""Background price tracking: scheduled refresh of wishlist and tracked ASINs."""

from __future__ import annotations

import datetime
import logging

import click

from audible_deals import constants, scheduler
from audible_deals.automation_models import TrackRunRequest
from audible_deals.cli.helpers import (
    _credit_price,
    _currency,
    _get_client,
)
from audible_deals.cli.helpers import _resolve_categories, _resolve_skip_asins
from audible_deals.config_store import (
    config_numeric_errors,
    load_monitors,
    load_notify_state,
    load_track_state,
    save_notify_state,
    save_track_state,
)
from audible_deals.locking import LockHeldError, run_lock
from audible_deals.monitor_service import MonitorRuntime
from audible_deals.notification_workflow import parse_webhook_headers
from audible_deals.parsing import parse_interval
from audible_deals.presentation.terminal import catalog_scan_progress, console
from audible_deals.result_publication import record_prices_safely as _safe_record_prices
from audible_deals.track_service import (
    TrackRuntime,
    run_history,
    run_track,
)
from audible_deals.webhook_client import WebhookClient, WebhookDeliveryError
from audible_deals.webhooks import (
    parse_webhook_headers as _parse_wh_headers,
)
from audible_deals.wishlist import warn_wishlist_issues

logger = logging.getLogger(__name__)

# Refresh history for ASINs seen within this window, beyond the wishlist
_MIN_INTERVAL_SECONDS = 600


def _run_history(state: dict) -> list[dict]:
    return run_history(state)


def _parse_cfg_webhook_headers(raw_list: list) -> dict[str, str]:
    """Parse stored webhook_headers config, skipping bad entries with a warning."""
    str_items = []
    for item in raw_list:
        if not isinstance(item, str):
            logger.warning("Skipping invalid webhook_headers entry: %r", item)
            continue
        str_items.append(item)
    return _parse_wh_headers(str_items, strict=False)


@click.group(invoke_without_command=True)
@click.pass_context
def track(ctx):
    """Background price tracking on an OS schedule.

    'deals track install' registers a launchd agent (macOS), systemd user
    timer or cron entry (Linux), or scheduled task (Windows) that runs
    'deals track run' on an interval. Each run refreshes prices for your
    wishlist, author watches, and recently tracked ASINs, records history,
    and sends webhook alerts for items at target.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(track_status)


@track.command("run")
@click.option(
    "--cooldown",
    type=click.IntRange(min=0),
    default=1,
    help="Suppress repeat webhook alerts for N days unless the price drops further (default: 1)",
)
@click.pass_context
def track_run(ctx, cooldown):
    """Refresh tracked prices once (the command the scheduler runs)."""
    cfg = ctx.obj.get("config", {})
    if errors := config_numeric_errors(cfg):
        raise click.ClickException(errors[0])
    webhook = cfg.get("webhook")
    webhook_format = cfg.get("webhook_format") or "generic"
    webhook_headers = _parse_cfg_webhook_headers(cfg.get("webhook_headers") or [])
    webhook_client = WebhookClient()
    request = TrackRunRequest(
        locale=ctx.obj["locale"],
        currency=_currency(ctx),
        credit_price=_credit_price(ctx),
        cooldown=cooldown,
        webhook=webhook,
        webhook_format=webhook_format,
        webhook_headers=webhook_headers,
    )
    runtime = TrackRuntime(
        get_client=_get_client,
        record_products=_safe_record_prices,
        webhook_client=webhook_client,
        monitor_runtime=MonitorRuntime(
            get_client=_get_client,
            resolve_categories=_resolve_categories,
            resolve_skip_asins=_resolve_skip_asins,
            progress=catalog_scan_progress,
        ),
        load_track_state=load_track_state,
        save_track_state=save_track_state,
        load_monitors=load_monitors,
        load_notify_state=load_notify_state,
        save_notify_state=save_notify_state,
        lock=run_lock,
    )
    try:
        result = run_track(request, runtime)
    except LockHeldError as e:
        console.print(f"[dim]Another run is in progress, skipping: {e}[/dim]")
        return
    except WebhookDeliveryError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as e:
        raise click.ClickException(f"track run failed: {e}")
    warn_wishlist_issues(result.wishlist_issues)
    console.print(f"[dim]{result.summary()}[/dim]")


@track.command("install")
@click.option(
    "--every",
    default="6h",
    help="Refresh interval (e.g. '6h', '30m', '1h30m'; default: 6h, minimum: 10m)",
)
@click.option("--webhook", default=None, help="Webhook URL to alert (saved to config)")
@click.option(
    "--webhook-format",
    type=click.Choice(constants.WEBHOOK_FORMATS),
    default=None,
    help="Webhook payload format (saved to config)",
)
@click.option(
    "--webhook-header",
    "webhook_headers",
    multiple=True,
    metavar="'NAME: VALUE'",
    help="Extra header for webhook POST (repeatable; requires --webhook)",
)
@click.pass_context
def track_install(ctx, every, webhook, webhook_format, webhook_headers):
    """Install the OS schedule for background tracking.

    \b
    Examples:
        deals track install
        deals track install --every 3h
        deals track install --webhook https://ntfy.sh/mytopic --webhook-format ntfy
    """
    interval_s = parse_interval(every)
    if interval_s < _MIN_INTERVAL_SECONDS:
        raise click.UsageError(
            f"Minimum interval is 10m — '{every}' would hammer the API."
        )

    if webhook_headers and not webhook:
        raise click.UsageError("--webhook-header requires --webhook")
    if webhook_headers:
        parse_webhook_headers(webhook_headers)

    if webhook:
        from audible_deals.config_store import load_config, save_config
        from audible_deals.validation import validate_webhook_url

        validate_webhook_url(webhook)
        cfg = load_config()
        cfg["webhook"] = webhook
        if webhook_format:
            cfg["webhook_format"] = webhook_format
        if webhook_headers:
            cfg["webhook_headers"] = list(webhook_headers)
        save_config(cfg)
        console.print("[green]Webhook saved to config.[/green]")
    elif webhook_format:
        raise click.UsageError("--webhook-format requires --webhook")

    try:
        description = scheduler.install(interval_s, constants.TRACK_LOG_FILE)
    except scheduler.SchedulerError as e:
        raise click.ClickException(str(e))

    state = load_track_state()
    state["install"] = {
        "every": every,
        "interval_s": interval_s,
        "method": description,
        "installed_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    save_track_state(state)

    console.print(f"[green]Installed:[/green] {description}")
    console.print(f"  Runs 'deals track run' every {every}")
    console.print(f"  Log: {constants.TRACK_LOG_FILE}")
    console.print("  Check with: deals track status")


@track.command("uninstall")
def track_uninstall():
    """Remove the OS schedule for background tracking."""
    try:
        removed = scheduler.uninstall()
    except scheduler.SchedulerError as e:
        raise click.ClickException(str(e))
    state = load_track_state()
    state.pop("install", None)
    save_track_state(state)
    if removed:
        console.print("[green]Background tracking schedule removed.[/green]")
    else:
        console.print("[dim]No schedule was installed.[/dim]")


@track.command("status")
@click.option(
    "--history",
    "show_history",
    is_flag=True,
    default=False,
    help="Print full run history table",
)
def track_status(show_history):
    """Show schedule and last-run status for background tracking."""
    from audible_deals.presentation.reports import display_track_history

    state = load_track_state()
    install_info = state.get("install")
    present, where = scheduler.installed()

    if install_info:
        console.print(
            f"  [dim]Schedule:[/dim]  every {install_info.get('every', '?')} "
            f"via {install_info.get('method', '?')}"
        )
        if not present:
            console.print(
                f"  [yellow]Warning: schedule not found at {where} — "
                "re-run 'deals track install'[/yellow]"
            )
    elif present:
        console.print(f"  [dim]Schedule:[/dim]  found at {where} (no install record)")
    else:
        console.print(
            "  [dim]Not installed. Run 'deals track install' to enable "
            "background tracking.[/dim]"
        )

    runs = _run_history(state)
    last = runs[0] if runs else None
    from audible_deals.config_store import load_monitor_state, load_monitors

    monitors = load_monitors()
    monitor_state = load_monitor_state().get("monitors", {})
    enabled = [
        name
        for name, definition in monitors.items()
        if isinstance(definition, dict) and definition.get("enabled", True)
    ]
    failed = [
        name
        for name in enabled
        if isinstance(monitor_state.get(name), dict)
        and monitor_state[name].get("last_error")
    ]
    if enabled:
        detail = f"  [dim]Monitors:[/dim]  {len(enabled)} enabled"
        if failed:
            detail += (
                f", [yellow]{len(failed)} with errors: {', '.join(failed)}[/yellow]"
            )
        console.print(detail)
    if not last:
        console.print("  [dim]Last run:[/dim]  never")
        return
    if last.get("error"):
        console.print(f"  [red]Last run:[/red]  {last.get('at')} — {last['error']}")
    else:
        console.print(
            f"  [dim]Last run:[/dim]  {last.get('at')} "
            f"({last.get('duration_s', '?')}s, "
            f"{last.get('wishlist_checked', 0)} wishlist + "
            f"{last.get('extra_tracked_checked', 0)} tracked checked, "
            f"{last.get('hits', 0)} at target, "
            f"{last.get('monitors_checked', 0)} monitor(s), "
            f"{last.get('monitor_events', 0)} event(s))"
        )

    if show_history:
        display_track_history(runs)


@track.command("log")
@click.option("--lines", "-n", type=click.IntRange(min=1), default=50)
def track_log(lines):
    """Show the tail of the background tracking log."""
    log_file = constants.TRACK_LOG_FILE
    if not log_file.exists():
        console.print(f"[dim]No log yet at {log_file}[/dim]")
        return
    content = log_file.read_text(errors="replace").splitlines()
    for line in content[-lines:]:
        click.echo(line)
