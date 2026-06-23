"""Background price tracking: scheduled refresh of wishlist and tracked ASINs."""

from __future__ import annotations

import datetime
import logging
import time

import click

from audible_deals import constants, scheduler
from audible_deals.cli.helpers import (
    _credit_price,
    _currency,
    _get_client,
    _safe_record_prices,
)
from audible_deals.cli.notify import (
    _apply_cooldown,
    _collect_target_hits,
    _parse_webhook_headers,
    _persist_notify_state,
    _post_webhook,
)
from audible_deals.constants import LockHeldError, run_lock
from audible_deals.display import console
from audible_deals.parsing import parse_interval
from audible_deals.price_history import load_all_price_histories
from audible_deals.storage import load_json_file, save_json_file
from audible_deals.webhooks import (
    format_webhook_message,
    format_webhook_payload,
    parse_webhook_headers as _parse_wh_headers,
)
from audible_deals.wishlist import load_wishlist, partition_wishlist

logger = logging.getLogger(__name__)

# Refresh history for ASINs seen within this window, beyond the wishlist
_RECENT_HISTORY_DAYS = 30
# Bound the per-run refresh so a large history dir can't blow up API usage
_MAX_EXTRA_ASINS = 200
_MIN_INTERVAL_SECONDS = 600
_RUN_HISTORY_MAX = 10


def _append_run(state: dict, entry: dict) -> None:
    """Insert entry newest-first into state["run_history"], capped at _RUN_HISTORY_MAX."""
    history = state.get("run_history", [])
    history.insert(0, entry)
    state["run_history"] = history[:_RUN_HISTORY_MAX]
    state.pop("last_run", None)


def _run_history(state: dict) -> list[dict]:
    """Return run history entries, newest-first. Falls back to legacy last_run key."""
    if "run_history" in state:
        return state["run_history"]
    last = state.get("last_run")
    if last:
        return [last]
    return []


def _parse_cfg_webhook_headers(raw_list: list) -> dict[str, str]:
    """Parse stored webhook_headers config, skipping bad entries with a warning."""
    str_items = []
    for item in raw_list:
        if not isinstance(item, str):
            logger.warning("Skipping invalid webhook_headers entry: %r", item)
            continue
        str_items.append(item)
    return _parse_wh_headers(str_items, strict=False)


def _load_track_state() -> dict:
    return load_json_file(constants.TRACK_STATE_FILE, dict, "track state")


def _save_track_state(state: dict) -> None:
    save_json_file(constants.TRACK_STATE_FILE, state, "track state")


def _recent_history_asins(exclude: set[str]) -> list[str]:
    """ASINs with a history entry in the last _RECENT_HISTORY_DAYS, oldest-checked first."""
    cutoff = (
        datetime.date.today() - datetime.timedelta(days=_RECENT_HISTORY_DAYS)
    ).isoformat()
    candidates: list[tuple[str, str]] = []
    for asin, entries in load_all_price_histories().items():
        if asin in exclude or not entries:
            continue
        last_date = entries[-1].get("date", "")
        if last_date >= cutoff:
            candidates.append((last_date, asin))
    candidates.sort()  # stalest first, so they get refreshed before the cap hits
    return [asin for _, asin in candidates[:_MAX_EXTRA_ASINS]]


def _send_hits_webhook(
    hits: list[dict],
    extras: dict,
    url: str,
    fmt: str,
    currency: str,
    extra_headers: dict[str, str] | None = None,
) -> None:
    body, headers = format_webhook_payload(hits, fmt, currency=currency, extras=extras)
    if extra_headers:
        headers = {**headers, **extra_headers}
    _post_webhook(url, body, headers)


def _is_auth_error(exc: Exception) -> bool:
    """True when an exception signals the user must re-run 'deals login'."""
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if status in (401, 403):
        return True
    msg = str(exc).lower()
    return "not authenticated" in msg or "deals login" in msg


def _record_failure(
    exc: Exception,
    started: float,
    webhook: str | None,
    webhook_format: str,
    webhook_headers: dict[str, str] | None,
) -> None:
    """Append a failed-run entry and ping only on genuine auth failures."""
    state = _load_track_state()
    _append_run(
        state,
        {
            "at": datetime.datetime.now().isoformat(timespec="seconds"),
            "duration_s": round(time.monotonic() - started, 1),
            "error": f"{type(exc).__name__}: {exc}",
        },
    )
    if _is_auth_error(exc):
        _notify_auth_error(state, str(exc), webhook, webhook_format, webhook_headers)
    _save_track_state(state)


def _notify_auth_error(
    state: dict,
    error: str,
    url: str | None,
    fmt: str,
    extra_headers: dict[str, str] | None = None,
) -> None:
    """One-time webhook ping when a background run hits an auth failure."""
    if not url or state.get("auth_error_notified"):
        return
    try:
        body, headers = format_webhook_message(
            f"Background tracking failed: {error}\nRun 'deals login' to re-authenticate.",
            fmt,
            title="audible-deals needs attention",
        )
        if extra_headers:
            headers = {**headers, **extra_headers}
        _post_webhook(url, body, headers)
        state["auth_error_notified"] = True
    except Exception:
        logger.exception("auth-error webhook ping failed")


@click.group()
def track():
    """Background price tracking on an OS schedule.

    'deals track install' registers a launchd agent (macOS), systemd user
    timer or cron entry (Linux), or scheduled task (Windows) that runs
    'deals track run' on an interval. Each run refreshes prices for your
    wishlist, author watches, and recently tracked ASINs, records history,
    and sends webhook alerts for items at target.
    """


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
    started = time.monotonic()
    cfg = ctx.obj.get("config", {})
    webhook = cfg.get("webhook")
    webhook_format = cfg.get("webhook_format") or "generic"
    webhook_headers = _parse_cfg_webhook_headers(cfg.get("webhook_headers") or [])

    try:
        with run_lock():
            state = _load_track_state()
            _track_run_locked(
                ctx, state, cooldown, webhook, webhook_format, started, webhook_headers
            )
    except LockHeldError as e:
        console.print(f"[dim]Another run is in progress, skipping: {e}[/dim]")
    except click.ClickException:
        raise
    except Exception as e:
        logger.exception("track run failed")
        try:
            with run_lock():
                _record_failure(e, started, webhook, webhook_format, webhook_headers)
        except LockHeldError:
            # Best-effort save when a concurrent run holds the lock.
            _record_failure(e, started, webhook, webhook_format, webhook_headers)
        raise click.ClickException(f"track run failed: {e}")


def _track_run_locked(
    ctx, state, cooldown, webhook, webhook_format, started, webhook_headers=None
):
    items = load_wishlist()
    asin_items, author_items = partition_wishlist(items)
    wishlist_asins = {i["asin"] for i in asin_items}

    dc = _get_client(ctx.obj["locale"])
    hits, extras, hit_asins = _collect_target_hits(
        dc, asin_items, author_items, credit_price=_credit_price(ctx)
    )

    extra_asins = _recent_history_asins(exclude=wishlist_asins)
    if extra_asins:
        with dc:
            extra_products = dc.get_products_batch(extra_asins)
        _safe_record_prices(extra_products)

    suppressed = 0
    webhook_sent = False
    today = datetime.date.today()
    if webhook and hits:
        kept, suppressed, notify_state = _apply_cooldown(hits, cooldown, today)
        if kept:
            _send_hits_webhook(
                kept, extras, webhook, webhook_format, _currency(ctx), webhook_headers
            )
            webhook_sent = True
            _persist_notify_state(notify_state, kept, asin_items, hit_asins, today)

    _append_run(
        state,
        {
            "at": datetime.datetime.now().isoformat(timespec="seconds"),
            "duration_s": round(time.monotonic() - started, 1),
            "wishlist_checked": len(asin_items),
            "author_watches_checked": len(author_items),
            "extra_tracked_checked": len(extra_asins),
            "hits": len(hits),
            "suppressed": suppressed,
            "webhook_sent": webhook_sent,
            "error": None,
        },
    )
    state.pop("auth_error_notified", None)
    _save_track_state(state)

    summary = (
        f"Refreshed {len(asin_items)} wishlist + {len(extra_asins)} tracked item(s); "
        f"{len(hits)} at target"
    )
    if webhook_sent:
        summary += " (webhook sent)"
    elif suppressed:
        summary += f" ({suppressed} suppressed by cooldown)"
    console.print(f"[dim]{summary}[/dim]")


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
        _parse_webhook_headers(webhook_headers)

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

    state = _load_track_state()
    state["install"] = {
        "every": every,
        "interval_s": interval_s,
        "method": description,
        "installed_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    _save_track_state(state)

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
    state = _load_track_state()
    state.pop("install", None)
    _save_track_state(state)
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
    from audible_deals.display import display_track_history

    state = _load_track_state()
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
            f"{last.get('hits', 0)} at target)"
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
