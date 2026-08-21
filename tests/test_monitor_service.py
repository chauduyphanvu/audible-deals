"""Monitor service diff, persistence, and scheduling tests."""

import datetime
import json

from audible_deals import constants
from audible_deals.automation_models import MonitorDefinition
from audible_deals.config_store import load_monitor_state, save_monitor_state
from audible_deals.monitor_service import (
    MonitorRuntime,
    monitor_events,
    record_monitor_error,
    run_monitor,
    select_monitors_for_run,
)


def definition(name, pages):
    return {
        "version": 1,
        "name": name,
        "enabled": True,
        "locale": "us",
        "mode": "find",
        "query": "",
        "settings": {"pages": pages, "sort": "price", "deep": False},
    }


def product(asin, price):
    return {
        "asin": asin,
        "title": asin,
        "full_title": asin,
        "price": price,
        "url": f"https://example.test/{asin}",
    }


def test_diff_preserves_new_drop_threshold_disappear_and_reentry_rules():
    previous = {"A1": product("A1", 4), "GONE": product("GONE", 2)}

    events = monitor_events(
        previous,
        (
            product("A1", 3.98),
            product("A2", 1),
            product("UNPRICED", None),
        ),
        "cheap",
    )

    assert [(event.event, event.asin) for event in events] == [
        ("price_drop", "A1"),
        ("new", "A2"),
    ]
    assert monitor_events({"A1": product("A1", 3.99)}, (), "cheap") == ()
    assert [
        event.event for event in monitor_events({}, (product("A1", 3.99),), "cheap")
    ] == ["new"]


def test_budget_selection_repairs_cursor_and_skips_malformed_or_oversized():
    monitors = {
        "bad": {"enabled": True, "settings": []},
        "huge": definition("huge", 61),
        "one": definition("one", 40),
        "two": definition("two", 20),
    }

    selection = select_monitors_for_run(monitors, 99)

    assert [monitor.name for monitor in selection.monitors] == ["two", "one"]
    assert selection.cursor == 3


def test_record_error_preserves_sparse_snapshot_and_unknown_fields(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(constants, "MONITOR_STATE_FILE", tmp_path / "state.json")
    constants.MONITOR_STATE_FILE.write_text(
        json.dumps(
            {
                "version": 1,
                "monitors": {"cheap": {"future": {"keep": True}}},
            }
        )
    )

    record_monitor_error("cheap", RuntimeError("down"))

    assert load_monitor_state()["monitors"]["cheap"] == {
        "future": {"keep": True},
        "last_error": "RuntimeError: down",
    }


def test_loaded_definition_unknown_fields_survive_model_boundary():
    raw = definition("one", 1) | {"future": [1, 2]}
    assert MonitorDefinition.from_dict(raw).to_dict() == raw


def test_success_preserves_unknown_slot_fields_and_valid_siblings(
    tmp_config, monkeypatch
):
    monkeypatch.setattr(constants, "MONITOR_STATE_FILE", tmp_config / "monitors.json")
    save_monitor_state(
        {
            "version": 1,
            "monitors": {
                "one": {
                    "initialized": False,
                    "products": {},
                    "future": {"keep": True},
                },
                "sibling": {
                    "initialized": True,
                    "products": {"S1": product("S1", 4)},
                },
            },
        }
    )
    monkeypatch.setattr(
        "audible_deals.monitor_service.scan_monitor",
        lambda definition, runtime: (product("A1", 2),),
    )
    runtime = MonitorRuntime(
        get_client=lambda locale: None,
        resolve_categories=lambda *args: ("", "", set()),
        resolve_skip_asins=lambda *args: set(),
        progress=lambda *args: None,
        now=lambda: datetime.datetime(2026, 8, 21, 12, 0),
    )

    run_monitor(MonitorDefinition.from_dict(definition("one", 1)), runtime)

    state = load_monitor_state()["monitors"]
    assert state["one"] == {
        "future": {"keep": True},
        "initialized": True,
        "products": {"A1": product("A1", 2)},
        "last_success": "2026-08-21T12:00:00",
        "last_error": None,
    }
    assert state["sibling"] == {
        "initialized": True,
        "products": {"S1": product("S1", 4)},
    }
