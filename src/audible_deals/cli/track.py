"""Background price tracking: scheduled refresh of wishlist and tracked ASINs."""

from __future__ import annotations

import dataclasses
import datetime
import logging

import click

from audible_deals import constants, json_output, paths, scheduler, secret_store
from audible_deals.automation_models import TrackRunRequest
from audible_deals.cli.errors import describe
from audible_deals.cli.helpers import (
    _credit_price,
    _currency,
    _get_client,
    _resolve_categories,
    _resolve_skip_asins,
)
from audible_deals.cli.options import json_option
from audible_deals.config_store import (
    config_numeric_errors,
    load_config,
    load_monitors,
    webhook_allow_private,
)
from audible_deals.exit_codes import exit_code_for
from audible_deals.locking import LockHeldError, run_lock
from audible_deals.monitor_service import MonitorRuntime
from audible_deals.notices import note, warn
from audible_deals.notification_workflow import parse_webhook_headers
from audible_deals.parsing import parse_interval
from audible_deals.presentation.terminal import (
    catalog_scan_progress,
    console,
    safe_markup,
    safe_text,
)
from audible_deals.result_publication import record_prices_safely as _safe_record_prices
from audible_deals.state_store import (
    load_install_record,
    load_notify_state,
    load_track_state,
    monitor_summaries,
    save_install_record,
    save_notify_state,
    save_track_state,
)
from audible_deals.track_log import rotate_log, tail_lines
from audible_deals.track_loop import human_seconds, run_every, stop_on_sigterm
from audible_deals.track_service import TrackRuntime, run_track
from audible_deals.webhook_client import WebhookClient, WebhookDeliveryError
from audible_deals.webhooks import (
    parse_webhook_headers as _parse_wh_headers,
)
from audible_deals.wishlist import warn_wishlist_issues

logger = logging.getLogger(__name__)

# Refresh history for ASINs seen within this window, beyond the wishlist
_MIN_INTERVAL_SECONDS = 600


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


def _run_track_once(ctx, cooldown, *, reload_config: bool = False):
    """One ``track run``: build the request from the config and run it.

    Returns the :class:`TrackRunResult`; failures propagate (``LockHeldError``
    when another run holds the lock). ``reload_config`` re-reads the config file,
    so a loop notices edits between runs.
    """
    if reload_config:
        ctx.obj["config"] = load_config()
    cfg = ctx.obj.get("config", {})
    if errors := config_numeric_errors(cfg):
        raise click.ClickException(errors[0])
    webhook_client = WebhookClient(allow_private=webhook_allow_private(cfg))
    request = TrackRunRequest(
        locale=ctx.obj["locale"],
        currency=_currency(ctx),
        credit_price=_credit_price(ctx),
        cooldown=cooldown,
        webhook=None,
        webhook_format=cfg.get("webhook_format") or "generic",
        webhook_headers={},
    )

    def open_secrets(base: TrackRunRequest) -> TrackRunRequest:
        # In keyring mode the config holds references; this is where they are
        # opened, inside run_track's recorded section: a keychain that cannot be
        # read becomes a failed run in `deals track status`, not a silent skip.
        return dataclasses.replace(
            base,
            webhook=secret_store.resolve(cfg.get("webhook")),
            webhook_headers=_parse_cfg_webhook_headers(
                secret_store.resolve(cfg.get("webhook_headers") or [])
            ),
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
            credit_price=request.credit_price,
        ),
        load_track_state=load_track_state,
        save_track_state=save_track_state,
        load_monitors=load_monitors,
        load_notify_state=load_notify_state,
        save_notify_state=save_notify_state,
        lock=run_lock,
        on_start=lambda: rotate_log(paths.track_log_file()),
        prepare_request=open_secrets,
    )
    try:
        return run_track(request, runtime)
    except WebhookDeliveryError as exc:
        raise click.ClickException(str(exc)) from exc
    except LockHeldError:
        raise
    except secret_store.SecretStoreError:
        raise  # its message says what to do; the run is already in the history
    except Exception as e:
        if exit_code_for(e) is not None:
            raise  # authentication / request failures keep their own exit status
        raise click.ClickException(f"track run failed: {e}") from e


def _print_track_result(result) -> None:
    warn_wishlist_issues(result.wishlist_issues)
    console.print(f"[dim]{safe_markup(result.summary())}[/dim]")


@track.command("run")
@click.option(
    "--cooldown",
    type=click.IntRange(min=0),
    default=1,
    help="Suppress repeat webhook alerts for N days unless the price drops further (default: 1, 0 turns it off)",
)
@click.option(
    "--every",
    default=None,
    metavar="INTERVAL",
    help=(
        "Keep running in the foreground, one run per INTERVAL (e.g. 30m, 2h; "
        "at least 10m), for use without a system scheduler. Failed runs are "
        "retried with backoff; Ctrl+C stops."
    ),
)
@click.pass_context
def track_run(ctx, cooldown, every):
    """Refresh tracked prices once (the command the scheduler runs).

    With --every it keeps going in the foreground instead. It uses the same run
    lock as scheduled runs, so a scheduler and a loop never run at once: a run
    that finds the lock held is skipped. After a failed run it waits twice as
    long each time (with some jitter, at most an hour) before trying again.

    \b
    Examples:
        deals track run
        deals track run --every 30m
    """
    if every is None:
        try:
            result = _run_track_once(ctx, cooldown)
        except LockHeldError as e:
            # Not a failure: the scheduler's next run picks up (exit 0, like notify).
            click.echo(
                f"Note: another run is in progress, skipping: {safe_text(e)}",
                err=True,
            )
            return
        _print_track_result(result)
        return

    interval = parse_interval(every)
    if interval < _MIN_INTERVAL_SECONDS:
        raise click.UsageError(
            f"Minimum interval is 10m — '{every}' would hammer the API."
        )
    console.print(f"[dim]Running every {safe_markup(every)} (Ctrl+C to stop).[/dim]")

    def stamp() -> str:
        return datetime.datetime.now().strftime("%H:%M:%S")

    def busy(error: Exception) -> None:
        note(f"{stamp()} another run is in progress, skipped this one")

    def failed(error: Exception, in_a_row: int, wait: float) -> None:
        warn(
            f"{stamp()} run failed ({in_a_row} in a row): {describe(error)}; "
            f"trying again in {human_seconds(wait)}"
        )

    def waiting(seconds: float, backing_off: bool) -> None:
        if not backing_off:
            console.print(
                f"[dim]Next run in {human_seconds(seconds)} (Ctrl+C to stop).[/dim]"
            )

    def succeeded(result) -> None:
        console.print(f"[dim]{stamp()}[/dim]", end=" ")
        _print_track_result(result)

    with stop_on_sigterm():
        run_every(
            lambda: _run_track_once(ctx, cooldown, reload_config=True),
            interval_s=interval,
            on_result=succeeded,
            on_busy=busy,
            on_failure=failed,
            on_wait=waiting,
        )
    note("Stopped.")


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
        from audible_deals.config_store import config_transaction
        from audible_deals.validation import validate_webhook_url

        validate_webhook_url(webhook)
        with config_transaction() as cfg:
            mode = secret_store.mode_of(cfg)
            cfg["webhook"] = secret_store.protect(
                webhook, secret_store.WEBHOOK_ACCOUNT, mode=mode
            )
            if webhook_format:
                cfg["webhook_format"] = webhook_format
            if webhook_headers:
                cfg["webhook_headers"] = secret_store.protect(
                    list(webhook_headers), secret_store.HEADERS_ACCOUNT, mode=mode
                )
        console.print("[green]Webhook saved to config.[/green]")
    elif webhook_format:
        raise click.UsageError("--webhook-format requires --webhook")

    try:
        description = scheduler.install(interval_s, paths.track_log_file())
    except scheduler.SchedulerError as e:
        raise click.ClickException(str(e)) from e

    save_install_record(
        {
            "every": every,
            "interval_s": interval_s,
            "method": description,
            "installed_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
    )

    console.print(f"[green]Installed:[/green] {safe_markup(description)}")
    console.print(f"  Runs 'deals track run' every {safe_markup(every)}")
    console.print(f"  Data: {safe_markup(paths.data_dir())}")
    console.print(f"  Log: {safe_markup(paths.track_log_file())}")
    console.print("  Check with: deals track status")


@track.command("uninstall")
def track_uninstall():
    """Remove the OS schedule for background tracking."""
    try:
        removed = scheduler.uninstall()
    except scheduler.SchedulerError as e:
        raise click.ClickException(str(e)) from e
    save_install_record(None)
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
@json_option("Output the schedule and last run as JSON")
def track_status(show_history, json_flag):
    """Show schedule and last-run status for background tracking."""
    from audible_deals.presentation.reports import display_track_history

    install_info = load_install_record()
    present, where = scheduler.installed()
    runs = load_track_state().runs
    monitor_state = monitor_summaries()
    enabled = [
        name
        for name, definition in load_monitors().items()
        if isinstance(definition, dict) and definition.get("enabled", True)
    ]
    failed = [
        name
        for name in enabled
        if name in monitor_state and monitor_state[name].last_error
    ]

    if json_flag:
        payload = json_output.track_status_payload(
            install=install_info or None,
            present=present,
            where=where,
            enabled_monitors=enabled,
            failed_monitors=failed,
            runs=runs,
            include_history=show_history,
        )
        click.echo(json_output.dumps(json_output.plain(payload)))
        return

    if install_info:
        console.print(
            f"  [dim]Schedule:[/dim]  every "
            f"{safe_markup(install_info.get('every', '?'))} "
            f"via {safe_markup(install_info.get('method', '?'))}"
        )
        if not present:
            console.print(
                f"  [yellow]Warning: schedule not found at {safe_markup(where)} — "
                "re-run 'deals track install'[/yellow]"
            )
    elif present:
        console.print(
            f"  [dim]Schedule:[/dim]  found at {safe_markup(where)} (no install record)"
        )
    else:
        console.print(
            "  [dim]Not installed. Run 'deals track install' to enable "
            "background tracking.[/dim]"
        )

    last = runs[0] if runs else None

    if enabled:
        detail = f"  [dim]Monitors:[/dim]  {len(enabled)} enabled"
        if failed:
            detail += f", [yellow]{len(failed)} with errors: "
            detail += f"{safe_markup(', '.join(failed))}[/yellow]"
        console.print(detail)
    if not last:
        console.print("  [dim]Last run:[/dim]  never")
        return
    if last.get("error"):
        console.print(
            f"  [red]Last run:[/red]  {safe_markup(last.get('at'))} — "
            f"{safe_markup(last['error'])}"
        )
    else:
        console.print(
            f"  [dim]Last run:[/dim]  {safe_markup(last.get('at'))} "
            f"({safe_markup(last.get('duration_s', '?'))}s, "
            f"{safe_markup(last.get('wishlist_checked', 0))} wishlist + "
            f"{safe_markup(last.get('extra_tracked_checked', 0))} tracked checked, "
            f"{safe_markup(last.get('hits', 0))} at target, "
            f"{safe_markup(last.get('monitors_checked', 0))} monitor(s), "
            f"{safe_markup(last.get('monitor_events', 0))} event(s))"
        )

    if show_history:
        display_track_history(runs)


@track.command("log")
@click.option("--lines", "-n", type=click.IntRange(min=1), default=50)
def track_log(lines):
    """Show the tail of the background tracking log."""
    log_file = paths.track_log_file()
    if not log_file.exists():
        console.print(f"[dim]No log yet at {safe_markup(log_file)}[/dim]")
        return
    for line in tail_lines(log_file, lines):
        click.echo(safe_text(line))
