"""Click-free saved-search monitor workflows."""

from __future__ import annotations

import dataclasses
import datetime
import logging
from dataclasses import dataclass, replace
from typing import Any, Callable

from audible_deals.automation_models import (
    MonitorDefinition,
    MonitorEvent,
    MonitorRunResult,
    MonitorSelection,
    MonitorSnapshot,
)
from audible_deals.catalog_workflow import (
    CatalogQueryError,
    bind_catalog_categories,
    build_monitor_scan_plan,
    execute_catalog_scan,
)
from audible_deals.config_store import (
    load_monitor_state,
    load_monitors,
    save_monitor_state,
    save_monitors,
)
from audible_deals.locking import run_lock
from audible_deals.result_processing import (
    SettingsFilterRequest,
    process_settings_discovery,
)
from audible_deals.serialization import serialize_product
from audible_deals.settings import Settings

logger = logging.getLogger(__name__)

MAX_MONITOR_API_CALLS_PER_RUN = 60


class MonitorServiceError(ValueError):
    pass


class MonitorExistsError(MonitorServiceError):
    pass


class MonitorNotFoundError(MonitorServiceError):
    pass


@dataclass(frozen=True)
class MonitorRuntime:
    get_client: Callable[[str], Any]
    resolve_categories: Callable[..., tuple]
    resolve_skip_asins: Callable[..., set[str]]
    progress: Callable[..., Any]
    now: Callable[[], datetime.datetime] = datetime.datetime.now


def settings_to_dict(settings: Settings) -> dict[str, Any]:
    return dataclasses.asdict(settings)


def settings_from_dict(data: dict) -> Settings:
    fields = {field.name for field in dataclasses.fields(Settings)}
    try:
        return Settings(**{key: value for key, value in data.items() if key in fields})
    except (TypeError, ValueError) as exc:
        raise MonitorServiceError(f"Invalid monitor settings: {exc}") from exc


def build_scan_plan(definition: MonitorDefinition):
    settings = settings_from_dict(definition.settings)
    try:
        return build_monitor_scan_plan(
            mode=definition.mode,
            query=definition.query,
            keywords=settings.keywords,
            sort=settings.sort,
            deep=settings.deep,
            pages=settings.pages,
        )
    except CatalogQueryError as exc:
        raise MonitorServiceError(str(exc)) from exc


def estimate_monitor_calls(definition: MonitorDefinition) -> int:
    plan = build_scan_plan(definition)
    assert plan.total_calls is not None
    return plan.total_calls


def monitor_snapshot(state: dict, name: str) -> MonitorSnapshot:
    monitors = state.setdefault("monitors", {})
    raw = monitors.get(name)
    if raw is not None and not isinstance(raw, dict):
        logger.warning(
            "monitor state for %s is malformed; resetting its baseline", name
        )
    elif isinstance(raw, dict) and not isinstance(raw.get("products", {}), dict):
        logger.warning(
            "monitor state products for %s is malformed; resetting its baseline", name
        )
    snapshot = MonitorSnapshot.from_dict(raw)
    if isinstance(raw, dict) and isinstance(raw.get("products", {}), dict):
        if len(snapshot.products) != len(raw.get("products", {})):
            logger.warning(
                "monitor state products for %s contains malformed entries", name
            )
    return snapshot


def _save_snapshot(state: dict, name: str, snapshot: MonitorSnapshot) -> None:
    state.setdefault("monitors", {})[name] = snapshot.to_dict(initialized=True)


def record_monitor_error(name: str, error: Exception) -> None:
    state = load_monitor_state()
    snapshot = monitor_snapshot(state, name)
    snapshot = replace(
        snapshot,
        last_error=f"{type(error).__name__}: {error}",
        present_fields=(snapshot.present_fields or frozenset()) | {"last_error"},
    )
    state.setdefault("monitors", {})[name] = snapshot.to_dict()
    save_monitor_state(state)


def scan_monitor(
    definition: MonitorDefinition, runtime: MonitorRuntime
) -> tuple[dict, ...]:
    settings = settings_from_dict(definition.settings)
    plan = build_scan_plan(definition)
    client = runtime.get_client(definition.locale)
    with client:
        category, _category_name, excluded = runtime.resolve_categories(
            client, settings.genre, "", settings.exclude_genre
        )
        skip_asins = runtime.resolve_skip_asins(client, settings.skip_owned, False)
        plan = bind_catalog_categories(plan, [category])
        with runtime.progress(plan, f"Checking monitor {definition.name}") as progress:
            products = execute_catalog_scan(client, plan, progress)
    result = process_settings_discovery(
        SettingsFilterRequest(
            products=tuple(products),
            settings=settings,
            skip_asins=skip_asins,
            exclude_category_ids=excluded,
        )
    )
    return tuple(
        serialize_product(product)
        for product in result.products
        if product.price is not None
    )


def monitor_events(
    previous: dict[str, dict], current: tuple[dict, ...], name: str
) -> tuple[MonitorEvent, ...]:
    events: list[MonitorEvent] = []
    for product in current:
        old = previous.get(product["asin"])
        price = product.get("price")
        if price is None:
            continue
        if old is None:
            kind = "new"
        elif (
            old.get("price") is not None and float(old["price"]) - float(price) >= 0.01
        ):
            kind = "price_drop"
        else:
            continue
        events.append(
            MonitorEvent(
                event=kind,
                monitor=name,
                asin=product["asin"],
                title=product.get("full_title") or product.get("title", ""),
                price=price,
                target=price,
                url=product.get("url", ""),
                previous_price=old.get("price") if old else None,
            )
        )
    return tuple(events)


def run_monitor(
    definition: MonitorDefinition,
    runtime: MonitorRuntime,
    *,
    deliver: Callable[[tuple[MonitorEvent, ...], MonitorDefinition], None]
    | None = None,
) -> MonitorRunResult:
    state = load_monitor_state()
    snapshot = monitor_snapshot(state, definition.name)
    current = tuple(
        product
        for product in scan_monitor(definition, runtime)
        if product.get("price") is not None
    )
    events = (
        monitor_events(snapshot.products, current, definition.name)
        if snapshot.initialized
        else ()
    )
    if events and deliver:
        deliver(events, definition)
    _save_snapshot(
        state,
        definition.name,
        MonitorSnapshot(
            initialized=True,
            products={product["asin"]: product for product in current},
            last_success=runtime.now().isoformat(timespec="seconds"),
            last_error=None,
            unknown_fields=snapshot.unknown_fields,
        ),
    )
    save_monitor_state(state)
    return MonitorRunResult(events, not snapshot.initialized)


def select_monitors_for_run(monitors: dict[str, dict], cursor: int) -> MonitorSelection:
    enabled = [
        (name, definition)
        for name, definition in sorted(monitors.items())
        if isinstance(definition, dict) and definition.get("enabled", True)
    ]
    if not enabled:
        return MonitorSelection((), cursor)
    start = int(cursor) % len(enabled)
    ordered = enabled[start:] + enabled[:start]
    selected: list[MonitorDefinition] = []
    used = 0
    last_name: str | None = None
    for name, raw_definition in ordered:
        if (
            not isinstance(raw_definition.get("settings"), dict)
            or "mode" not in raw_definition
        ):
            logger.warning("Skipping malformed monitor %s", name)
            continue
        definition = MonitorDefinition.from_dict(raw_definition)
        try:
            estimate = estimate_monitor_calls(definition)
        except (KeyError, TypeError, MonitorServiceError) as exc:
            logger.warning("Skipping malformed monitor %s: %s", name, exc)
            continue
        if estimate > MAX_MONITOR_API_CALLS_PER_RUN:
            logger.warning(
                "Skipping monitor %s: estimate %s exceeds per-run budget",
                name,
                estimate,
            )
            continue
        if used + estimate > MAX_MONITOR_API_CALLS_PER_RUN:
            if selected:
                break
            continue
        if not definition.name:
            definition = replace(
                definition,
                name=name,
                present_fields=(definition.present_fields or frozenset()) | {"name"},
            )
        selected.append(definition)
        used += estimate
        last_name = name
    next_cursor = cursor
    if last_name is not None:
        next_cursor = (
            next(index for index, item in enumerate(enabled) if item[0] == last_name)
            + 1
        ) % len(enabled)
    return MonitorSelection(tuple(selected), next_cursor)


def add_monitor(definition: MonitorDefinition) -> None:
    with run_lock():
        monitors = load_monitors()
        if definition.name in monitors:
            raise MonitorExistsError(f"Monitor '{definition.name}' already exists.")
        monitors[definition.name] = definition.to_dict()
        save_monitors(monitors)
        state = load_monitor_state()
        state.setdefault("monitors", {}).pop(definition.name, None)
        save_monitor_state(state)


def remove_monitor(name: str) -> None:
    with run_lock():
        monitors = load_monitors()
        if name not in monitors:
            raise MonitorNotFoundError(f"Monitor '{name}' not found.")
        del monitors[name]
        save_monitors(monitors)
        state = load_monitor_state()
        state.setdefault("monitors", {}).pop(name, None)
        save_monitor_state(state)


def set_monitor_enabled(name: str, enabled: bool) -> bool:
    with run_lock():
        monitors = load_monitors()
        raw = monitors.get(name)
        if not isinstance(raw, dict):
            raise MonitorNotFoundError(f"Monitor '{name}' not found.")
        if raw.get("enabled", True) == enabled:
            return False
        raw["enabled"] = enabled
        save_monitors(monitors)
    return True
