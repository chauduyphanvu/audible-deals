"""Click-free background tracking orchestration."""

from __future__ import annotations

import bisect
import datetime
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from audible_deals import constants
from audible_deals.automation_models import TrackRunRequest, TrackRunResult
from audible_deals.config_store import (
    load_monitors,
    load_notify_state,
    load_track_state,
    save_notify_state,
    save_track_state,
)
from audible_deals.locking import LockHeldError, run_lock
from audible_deals.monitor_service import (
    MonitorRuntime,
    record_monitor_error,
    run_monitor,
    select_monitors_for_run,
)
from audible_deals.notification_service import (
    apply_cooldown,
    build_notify_state,
    collect_target_hits,
    deliver_auth_error,
    deliver_hits,
    deliver_monitor_events,
)
from audible_deals.product import Product
from audible_deals.refresh_eligibility import load_refresh_eligibility
from audible_deals.results_cache import load_dismissed_asins
from audible_deals.webhook_client import WebhookClient, WebhookDeliveryError
from audible_deals.wishlist import inspect_wishlist, load_wishlist

logger = logging.getLogger(__name__)

RECENT_HISTORY_DAYS = 30
MAX_EXTRA_ASINS = 200
RUN_HISTORY_MAX = 10


@dataclass(frozen=True)
class TrackRuntime:
    get_client: Callable[[str], Any]
    record_products: Callable[[list[Product]], None]
    webhook_client: WebhookClient
    monitor_runtime: MonitorRuntime
    load_wishlist: Callable[[], list[dict]] = load_wishlist
    load_refresh_eligibility: Callable[[], dict[str, dict[str, str]]] = (
        load_refresh_eligibility
    )
    load_dismissed_asins: Callable[[], set[str]] = load_dismissed_asins
    load_track_state: Callable[[], dict] = load_track_state
    save_track_state: Callable[[dict], None] = save_track_state
    load_monitors: Callable[[], dict[str, dict]] = load_monitors
    load_notify_state: Callable[[], dict] = load_notify_state
    save_notify_state: Callable[[dict], None] = save_notify_state
    lock: Callable = run_lock
    today: Callable[[], datetime.date] = datetime.date.today
    now: Callable[[], datetime.datetime] = datetime.datetime.now
    monotonic: Callable[[], float] = time.monotonic


def append_run(state: dict, entry: dict) -> None:
    history = state.get("run_history", [])
    history.insert(0, entry)
    state["run_history"] = history[:RUN_HISTORY_MAX]
    state.pop("last_run", None)


def run_history(state: dict) -> list[dict]:
    if "run_history" in state:
        return state["run_history"]
    last = state.get("last_run")
    return [last] if last else []


def refresh_eligible_asins(
    exclude: set[str],
    eligibility: dict[str, dict[str, str]],
    locale: str,
    today: datetime.date,
    cursor: object,
) -> tuple[list[str], dict[str, str] | None]:
    cutoff = (today - datetime.timedelta(days=RECENT_HISTORY_DAYS)).isoformat()
    candidates: list[tuple[str, str]] = []
    today_iso = today.isoformat()
    for asin, surfaced_on in eligibility.get(locale, {}).items():
        if asin in exclude or not isinstance(surfaced_on, str):
            continue
        if cutoff <= surfaced_on <= today_iso:
            candidates.append((surfaced_on, asin))
    candidates.sort()
    if not candidates:
        return [], None
    cursor_key = None
    if isinstance(cursor, dict):
        surfaced_on = cursor.get("surfaced_on")
        asin = cursor.get("asin")
        try:
            if isinstance(surfaced_on, str):
                datetime.date.fromisoformat(surfaced_on)
            else:
                raise ValueError
        except ValueError:
            pass
        else:
            if isinstance(asin, str) and constants._ASIN_RE.fullmatch(asin):
                cursor_key = (surfaced_on, asin)
    start = bisect.bisect_right(candidates, cursor_key) if cursor_key else 0
    if start == len(candidates):
        start = 0
    count = min(MAX_EXTRA_ASINS, len(candidates))
    selected_keys = [
        candidates[(start + offset) % len(candidates)] for offset in range(count)
    ]
    continuation = selected_keys[-1]
    return [asin for _, asin in selected_keys], {
        "surfaced_on": continuation[0],
        "asin": continuation[1],
    }


def is_auth_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if status in (401, 403):
        return True
    message = str(exc).lower()
    return "not authenticated" in message or "deals login" in message


def _notify_auth_failure(
    state: dict,
    error: str,
    request: TrackRunRequest,
    runtime: TrackRuntime,
) -> None:
    if not request.webhook or state.get("auth_error_notified"):
        return
    try:
        deliver_auth_error(
            error,
            request.webhook,
            request.webhook_format,
            request.webhook_headers,
            runtime.webhook_client,
        )
    except Exception:
        logger.exception("auth-error webhook ping failed")
    else:
        state["auth_error_notified"] = True


def _record_failure(
    exc: Exception,
    started: float,
    request: TrackRunRequest,
    runtime: TrackRuntime,
) -> None:
    state = runtime.load_track_state()
    result = TrackRunResult(
        at=runtime.now().isoformat(timespec="seconds"),
        duration_s=round(runtime.monotonic() - started, 1),
        error=f"{type(exc).__name__}: {exc}",
        present_fields=frozenset({"at", "duration_s", "error"}),
    )
    append_run(state, result.to_dict())
    if is_auth_error(exc):
        _notify_auth_failure(state, str(exc), request, runtime)
    runtime.save_track_state(state)


def _run_locked(
    request: TrackRunRequest,
    runtime: TrackRuntime,
    state: dict,
    started: float,
) -> TrackRunResult:
    inspection = inspect_wishlist(runtime.load_wishlist())
    asin_items = inspection.asin_items
    author_items = inspection.author_items
    wishlist_asins = {item["asin"] for item in asin_items}
    today = runtime.today()

    client = runtime.get_client(request.locale)
    hits, extras, hit_asins = collect_target_hits(
        client,
        asin_items,
        author_items,
        runtime.record_products,
        credit_price=request.credit_price,
    )

    excluded_asins = wishlist_asins | runtime.load_dismissed_asins()
    eligibility = runtime.load_refresh_eligibility()
    cursors = state.get("refresh_cursors")
    if not isinstance(cursors, dict):
        cursors = {}
    cursor = cursors.get(request.locale)
    extra_asins, next_cursor = refresh_eligible_asins(
        excluded_asins,
        eligibility,
        request.locale,
        today,
        cursor,
    )
    if extra_asins:
        cursors[request.locale] = next_cursor
        state["refresh_cursors"] = cursors
        with client:
            products = client.get_products_batch(extra_asins)
        runtime.record_products(products)

    suppressed = 0
    webhook_sent = False
    if request.webhook and hits:
        notify_state = runtime.load_notify_state()
        if not isinstance(notify_state, dict):
            notify_state = {}
        kept, suppressed = apply_cooldown(hits, request.cooldown, today, notify_state)
        if kept:
            deliver_hits(
                kept,
                extras,
                request.webhook,
                request.webhook_format,
                request.currency,
                request.webhook_headers,
                runtime.webhook_client,
            )
            webhook_sent = True
            runtime.save_notify_state(
                build_notify_state(
                    notify_state,
                    kept,
                    asin_items,
                    hit_asins,
                    today,
                )
            )

    monitor_checked = 0
    monitor_events_count = 0
    monitor_failures: list[str] = []

    def deliver(events, monitor):
        deliver_monitor_events(
            events,
            monitor,
            request.webhook,
            request.webhook_format,
            request.webhook_headers,
            runtime.webhook_client,
        )

    selection = select_monitors_for_run(
        runtime.load_monitors(), int(state.get("monitor_cursor", 0))
    )
    if selection.monitors:
        state["monitor_cursor"] = selection.cursor
    for definition in selection.monitors:
        monitor_checked += 1
        try:
            result = run_monitor(
                definition,
                runtime.monitor_runtime,
                deliver=deliver,
            )
            monitor_events_count += len(result.events)
        except Exception as exc:
            logger.exception("monitor %s failed", definition.name)
            monitor_failures.append(f"{definition.name}: {type(exc).__name__}: {exc}")
            record_monitor_error(definition.name, exc)

    result = TrackRunResult(
        at=runtime.now().isoformat(timespec="seconds"),
        duration_s=round(runtime.monotonic() - started, 1),
        wishlist_checked=len(asin_items),
        author_watches_checked=len(author_items),
        extra_tracked_checked=len(extra_asins),
        hits=len(hits),
        suppressed=suppressed,
        webhook_sent=webhook_sent,
        monitors_checked=monitor_checked,
        monitors_scheduled=len(selection.monitors),
        monitor_events=monitor_events_count,
        monitor_failures=tuple(monitor_failures),
        error=None,
        wishlist_issues=tuple(inspection.issues),
    )
    append_run(state, result.to_dict())
    state.pop("auth_error_notified", None)
    runtime.save_track_state(state)
    return result


def run_track(request: TrackRunRequest, runtime: TrackRuntime) -> TrackRunResult:
    started = runtime.monotonic()
    try:
        with runtime.lock():
            state = runtime.load_track_state()
            return _run_locked(request, runtime, state, started)
    except LockHeldError:
        raise
    except WebhookDeliveryError:
        raise
    except Exception as exc:
        logger.exception("track run failed")
        try:
            with runtime.lock():
                _record_failure(exc, started, request, runtime)
        except LockHeldError:
            logger.warning(
                "Could not record failed run because another run holds the lock"
            )
        raise
