"""Monitor service diff, persistence, and scheduling tests."""

import datetime
from contextlib import contextmanager

import pytest

from audible_deals import db
from audible_deals.automation_models import MonitorDefinition, MonitorSnapshot
from audible_deals.cli.helpers import _resolve_skip_asins
from audible_deals.monitor_service import (
    MonitorRuntime,
    MonitorServiceError,
    monitor_events,
    record_monitor_error,
    run_monitor,
    scan_monitor,
    select_monitors_for_run,
    settings_from_dict,
)
from audible_deals.product import Product
from audible_deals.results_cache import save_dismissed_asins
from audible_deals.state_store import (
    load_monitor_snapshot,
    monitor_summaries,
    save_monitor_snapshot,
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


def test_settings_from_dict_rejects_plus_conflict():
    with pytest.raises(MonitorServiceError, match="mutually exclusive"):
        settings_from_dict({"skip_plus": True, "only_plus": True})


def test_settings_from_dict_rejects_non_boolean_plus_value():
    with pytest.raises(MonitorServiceError, match="only_plus must be boolean"):
        settings_from_dict({"only_plus": 1})


def test_monitor_excludes_dismissed_when_exclude_seen_is_false(tmp_config, monkeypatch):
    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    @contextmanager
    def progress(*args, **kwargs):
        yield None

    dismissed = Product(
        asin="MONDISMISS", title="Dismissed", price=2, length_minutes=60
    )
    visible = Product(asin="MONKEEP", title="Visible", price=3, length_minutes=60)
    save_dismissed_asins({dismissed.asin})
    monkeypatch.setattr(
        "audible_deals.monitor_service.execute_catalog_scan",
        lambda *args: [dismissed, visible],
    )
    runtime = MonitorRuntime(
        get_client=lambda locale: Client(),
        resolve_categories=lambda *args: ("", "", set()),
        resolve_skip_asins=_resolve_skip_asins,
        progress=progress,
    )

    results = scan_monitor(
        MonitorDefinition.from_dict(definition("dismissed", 1)), runtime
    )

    assert [item["asin"] for item in results] == [visible.asin]


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


def test_record_error_keeps_the_baseline_it_did_not_change(tmp_config):
    save_monitor_snapshot(
        "cheap",
        MonitorSnapshot(
            initialized=True,
            products={"A1": product("A1", 3)},
            last_success="2026-08-21T12:00:00",
        ),
    )

    record_monitor_error("cheap", RuntimeError("down"))

    snapshot = load_monitor_snapshot("cheap")
    assert snapshot.last_error == "RuntimeError: down"
    assert snapshot.initialized is True
    assert snapshot.products == {"A1": product("A1", 3)}
    assert snapshot.last_success == "2026-08-21T12:00:00"


def test_record_error_for_a_new_monitor_leaves_it_uninitialized(tmp_config):
    record_monitor_error("fresh", RuntimeError("down"))

    snapshot = load_monitor_snapshot("fresh")
    assert snapshot.initialized is False
    assert snapshot.products == {}
    assert snapshot.last_error == "RuntimeError: down"


def test_loaded_definition_unknown_fields_survive_model_boundary():
    raw = definition("one", 1) | {"future": [1, 2]}
    assert MonitorDefinition.from_dict(raw).to_dict() == raw


def test_success_saves_the_baseline_and_leaves_sibling_monitors_alone(
    tmp_config, monkeypatch
):
    save_monitor_snapshot("one", MonitorSnapshot(last_error="RuntimeError: old"))
    save_monitor_snapshot(
        "sibling",
        MonitorSnapshot(initialized=True, products={"S1": product("S1", 4)}),
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

    result = run_monitor(MonitorDefinition.from_dict(definition("one", 1)), runtime)

    assert result.baseline is True
    assert load_monitor_snapshot("one") == MonitorSnapshot(
        initialized=True,
        products={"A1": product("A1", 2)},
        last_success="2026-08-21T12:00:00",
        last_error=None,
    )
    assert load_monitor_snapshot("sibling") == MonitorSnapshot(
        initialized=True, products={"S1": product("S1", 4)}
    )
    assert set(monitor_summaries()) == {"one", "sibling"}


def test_a_monitor_with_a_baseline_reports_changes_on_the_next_run(
    tmp_config, monkeypatch
):
    runtime = MonitorRuntime(
        get_client=lambda locale: None,
        resolve_categories=lambda *args: ("", "", set()),
        resolve_skip_asins=lambda *args: set(),
        progress=lambda *args: None,
        now=lambda: datetime.datetime(2026, 8, 21, 12, 0),
    )
    scans = iter([(product("A1", 5),), (product("A1", 3), product("A2", 4))])
    monkeypatch.setattr(
        "audible_deals.monitor_service.scan_monitor",
        lambda definition, runtime: next(scans),
    )
    delivered = []
    monitor = MonitorDefinition.from_dict(definition("one", 1))

    first = run_monitor(monitor, runtime, deliver=lambda e, m: delivered.append(e))
    second = run_monitor(monitor, runtime, deliver=lambda e, m: delivered.append(e))

    assert (first.baseline, first.events) == (True, ())
    assert second.baseline is False
    assert [(e.event, e.asin) for e in second.events] == [
        ("price_drop", "A1"),
        ("new", "A2"),
    ]
    assert len(delivered) == 1


def test_product_prices_are_stored_for_diffing(tmp_config):
    save_monitor_snapshot(
        "one",
        MonitorSnapshot(
            initialized=True,
            products={"A1": product("A1", 3), "A2": product("A2", None)},
        ),
    )
    rows = dict(
        db.state().execute(
            "SELECT asin, price FROM monitor_products WHERE monitor = 'one'"
        )
    )
    assert rows == {"A1": 3.0, "A2": None}
