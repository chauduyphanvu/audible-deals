"""Acceptance coverage for saved-search monitors."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from audible_deals import constants
from audible_deals.cli import cli
from audible_deals.cli import monitor as monitor_module
from audible_deals.cli import track as track_module
from audible_deals.config_store import (
    load_monitor_state,
    load_monitors,
    save_monitor_state,
    save_monitors,
    save_profiles,
)
from audible_deals.display import console, display_track_history
from audible_deals.product import Product
from audible_deals.serialization import serialize_product
from audible_deals.webhooks import (
    format_monitor_webhook_payload,
    format_webhook_payload,
)


@pytest.fixture
def monitor_files(monkeypatch, tmp_path):
    for name, filename in (
        ("MONITORS_FILE", "monitors.json"),
        ("MONITOR_STATE_FILE", "monitor-state.json"),
        ("PROFILES_FILE", "profiles.json"),
        ("CONFIG_FILE", "config.json"),
        ("LOCK_FILE", "monitor.lock"),
    ):
        monkeypatch.setattr(constants, name, tmp_path / filename)
    return tmp_path


def _definition(name="cheap", *, locale="us", mode="find", **settings):
    return {
        "version": 1,
        "name": name,
        "enabled": True,
        "locale": locale,
        "mode": mode,
        "query": "author" if mode == "search" else "",
        "settings": {"pages": 1, "limit": 0, "sort": "price", **settings},
    }


def _product(asin="A1", price=4.0):
    return serialize_product(
        Product(asin=asin, title=asin, price=price, length_minutes=60)
    )


def test_add_contract_frozen_precedence_show_and_remove(monitor_files):
    constants.CONFIG_FILE.write_text(
        json.dumps({"pages": 2, "max_price": 9.0, "limit": 1})
    )
    save_profiles({"scifi": {"pages": 4, "max_price": 6.0, "limit": 2}})
    runner = CliRunner()

    assert runner.invoke(cli, ["monitor", "add", "bad"]).exit_code == 2
    assert (
        runner.invoke(
            cli, ["monitor", "add", "bad", "--profile", "scifi", "--query", "x"]
        ).exit_code
        == 2
    )
    result = runner.invoke(
        cli,
        [
            "--locale",
            "uk",
            "monitor",
            "add",
            "books",
            "--profile",
            "scifi",
            "--pages",
            "5",
            "--max-price",
            "4",
        ],
    )
    assert result.exit_code == 0, result.output
    saved = load_monitors()["books"]
    assert saved["locale"] == "uk"
    assert saved["mode"] == "find"
    assert saved["settings"]["max_price"] == 4.0
    assert saved["settings"]["pages"] == 5
    assert saved["settings"]["sort"] == "price-per-hour"
    assert saved["settings"]["limit"] is None
    shown = runner.invoke(cli, ["monitor", "show", "books"])
    assert shown.exit_code == 0
    assert "Source: profile scifi" in shown.output
    assert "Webhook" not in shown.output
    assert "max-price: 4.0" in shown.output
    assert "pages: 5" in shown.output
    assert "sort: price-per-hour" in shown.output
    assert runner.invoke(
        cli, ["monitor", "remove", "books"], input="n\n"
    ).output.endswith("Cancelled.\n")
    assert "books" in load_monitors()
    assert runner.invoke(cli, ["monitor", "remove", "books", "--yes"]).exit_code == 0
    assert not load_monitors()


def test_direct_query_has_search_defaults_filters_and_completion(monitor_files):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "monitor",
            "add",
            "sanderson",
            "--query",
            "Sanderson | Wells",
            "--max-price",
            "7",
            "--pages",
            "2",
        ],
    )
    assert result.exit_code == 0, result.output
    saved = load_monitors()["sanderson"]
    assert saved["mode"] == "search"
    assert saved["settings"]["sort"] == "relevance"
    assert saved["settings"]["min_ratings"] == 0
    assert saved["settings"]["max_price"] == 7.0
    assert saved["settings"]["pages"] == 2
    assert [
        item.value
        for item in monitor_module._complete_monitor_names(None, None, "sand")
    ] == ["sanderson"]
    assert [
        item.value for item in monitor_module._complete_monitor_names(None, None, "no")
    ] == []


def test_baseline_events_disappear_reentry_and_monitor_isolation(
    monkeypatch, monitor_files
):
    scans = [
        [_product("A1", 4.0)],
        [_product("A1", 4.0), _product("A2", 3.0)],
        [_product("A1", 3.98)],
        [],
        [_product("A1", 3.98)],
        [_product("A1", 2.0)],
    ]
    monkeypatch.setattr(monitor_module, "scan_monitor", lambda definition: scans.pop(0))
    first = _definition("first")
    second = _definition("second")
    assert monitor_module.run_monitor(first)[0] == []
    assert [event["event"] for event in monitor_module.run_monitor(first)[0]] == ["new"]
    assert [event["event"] for event in monitor_module.run_monitor(first)[0]] == [
        "price_drop"
    ]
    assert monitor_module.run_monitor(first)[0] == []
    assert [event["event"] for event in monitor_module.run_monitor(first)[0]] == ["new"]
    assert monitor_module.run_monitor(second)[0] == []
    state = load_monitor_state()["monitors"]
    assert state["first"]["products"]["A1"]["price"] == 3.98
    assert state["second"]["products"]["A1"]["price"] == 2.0


def test_unpriced_products_are_not_snapshotted_or_delivered(monkeypatch, monitor_files):
    product = _product(price=None)
    monkeypatch.setattr(monitor_module, "scan_monitor", lambda definition: [product])
    assert monitor_module.run_monitor(_definition())[0] == []
    assert load_monitor_state()["monitors"]["cheap"]["products"] == {}
    # Production scan filters unpriced records before they reach this low-level helper.
    assert monitor_module._events({}, [product], "cheap") == []


def test_monitor_webhooks_all_formats_and_header_routing(monkeypatch, monitor_files):
    event = {
        "event": "new",
        "monitor": "cheap",
        "asin": "A1",
        "title": "Book",
        "price": 3.0,
        "target": 3.0,
        "url": "https://example.test/A1",
    }
    for fmt in constants.WEBHOOK_FORMATS:
        body, _headers = format_webhook_payload([event], fmt)
        assert body
        monitor_body, _monitor_headers = format_monitor_webhook_payload(
            [event], fmt, locale="us"
        )
        assert monitor_body
    body, _headers = format_monitor_webhook_payload([event], "generic", locale="us")
    monitor_payload = json.loads(body)
    assert monitor_payload == {
        "monitor": "cheap",
        "locale": "us",
        "events": [event],
        "count": 1,
    }
    assert json.loads(format_webhook_payload([event], "generic")[0]) == {
        "deals": [event],
        "count": 1,
    }
    sent = []
    monkeypatch.setattr(track_module, "post_webhook", lambda *args: sent.append(args))
    headers = {"Authorization": "secret"}
    track_module._send_monitor_events(
        [event],
        {
            "locale": "us",
            "webhook": "https://override.test",
            "webhook_format": "generic",
        },
        "https://global.test",
        "slack",
        headers,
    )
    track_module._send_monitor_events(
        [event], {"locale": "us"}, "https://global.test", "slack", headers
    )
    assert json.loads(sent[0][1])["monitor"] == "cheap"
    assert sent[0][-1].get("Authorization") is None
    assert sent[1][-1]["Authorization"] == "secret"


def test_delivery_failure_keeps_snapshot_for_retry_and_persists_error(
    monkeypatch, monitor_files
):
    scans = [
        [_product("A1", 4.0)],
        [_product("A1", 4.0), _product("A2", 3.0)],
        [_product("A1", 4.0), _product("A2", 3.0)],
    ]
    monkeypatch.setattr(monitor_module, "scan_monitor", lambda definition: scans.pop(0))
    definition = _definition()
    monitor_module.run_monitor(definition)
    with pytest.raises(RuntimeError):
        monitor_module.run_monitor(
            definition,
            deliver=lambda events, definition: (_ for _ in ()).throw(
                RuntimeError("down")
            ),
        )
    monitor_module.record_monitor_error("cheap", RuntimeError("down"))
    slot = load_monitor_state()["monitors"]["cheap"]
    assert set(slot["products"]) == {"A1"}
    assert "down" in slot["last_error"]
    events, _ = monitor_module.run_monitor(definition)
    assert [event["asin"] for event in events] == ["A2"]
    assert load_monitor_state()["monitors"]["cheap"]["last_error"] is None


def test_malformed_state_self_heals_without_losing_valid_sibling(
    monkeypatch, monitor_files
):
    constants.MONITOR_STATE_FILE.write_text(
        json.dumps(
            {
                "version": 1,
                "monitors": {
                    "bad": [],
                    "good": {"initialized": True, "products": {"A1": _product()}},
                },
            }
        )
    )
    monkeypatch.setattr(
        monitor_module, "scan_monitor", lambda definition: [_product("A2", 2.0)]
    )
    events, baseline = monitor_module.run_monitor(_definition("bad"))
    assert events == [] and baseline is True
    state = load_monitor_state()["monitors"]
    assert state["good"]["products"]["A1"]["price"] == 4.0
    assert state["bad"]["products"]["A2"]["price"] == 2.0


def test_missing_monitor_state_mapping_is_repaired(monkeypatch, monitor_files):
    constants.MONITOR_STATE_FILE.write_text(json.dumps({"version": 1}))
    monkeypatch.setattr(monitor_module, "scan_monitor", lambda definition: [])
    monitor_module.run_monitor(_definition())
    slot = load_monitor_state()["monitors"]["cheap"]
    assert slot["initialized"] is True
    assert slot["products"] == {}
    assert slot["last_success"]
    assert slot["last_error"] is None


def test_scan_plan_client_sort_and_multi_query():
    settings = monitor_module._settings_from_dict(
        {"pages": 2, "sort": "price", "deep": False}
    )
    queries, sorts = monitor_module.monitor_scan_plan(settings, "search", "a | b")
    assert queries == ["a", "b"]
    assert sorts == ["Relevance"]
    queries, sorts = monitor_module.monitor_scan_plan(settings, "find", "")
    assert queries == [""]
    assert sorts == ["BestSellers"]


def test_monitor_budget_round_robin_is_bounded():
    monitors = {name: _definition(name, pages=30) for name in ("a", "b", "c")}
    state = {"monitor_cursor": 0}
    first = monitor_module.select_monitors_for_run(monitors, state)
    second = monitor_module.select_monitors_for_run(monitors, state)
    assert [name for name, _ in first] == ["a", "b"]
    assert [name for name, _ in second] == ["c", "a"]


def test_monitor_budget_round_robin_does_not_starve_expensive_monitor():
    monitors = {
        "a": _definition("a", pages=40),
        "b": _definition("b", pages=40),
        "c": _definition("c", pages=20),
    }
    state = {"monitor_cursor": 0}
    first = monitor_module.select_monitors_for_run(monitors, state)
    second = monitor_module.select_monitors_for_run(monitors, state)
    assert [name for name, _ in first] == ["a"]
    assert [name for name, _ in second] == ["b", "c"]


def test_pause_resume_are_idempotent_and_test_does_not_persist(
    monkeypatch, monitor_files
):
    save_monitors({"cheap": _definition()})
    save_monitor_state(
        {
            "version": 1,
            "monitors": {
                "cheap": {"initialized": True, "products": {"A1": _product()}}
            },
        }
    )
    runner = CliRunner()
    assert runner.invoke(cli, ["monitor", "pause", "cheap"]).exit_code == 0
    assert "already paused" in runner.invoke(cli, ["monitor", "pause", "cheap"]).output
    assert runner.invoke(cli, ["monitor", "resume", "cheap"]).exit_code == 0
    before = constants.MONITOR_STATE_FILE.read_text()
    monkeypatch.setattr(
        monitor_module, "scan_monitor", lambda definition: [_product("A2", 2.0)]
    )
    result = runner.invoke(cli, ["monitor", "test", "cheap"])
    assert result.exit_code == 0
    assert "Current matches" in result.output
    assert "A2 — $2.00 — A2" in result.output
    assert constants.MONITOR_STATE_FILE.read_text() == before


def test_add_clears_orphaned_state(monitor_files):
    save_profiles({"scifi": {}})
    save_monitor_state(
        {
            "version": 1,
            "monitors": {
                "cheap": {"initialized": True, "products": {"A1": _product()}}
            },
        }
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["monitor", "add", "cheap", "--profile", "scifi"])
    assert result.exit_code == 0, result.output
    assert "cheap" not in load_monitor_state()["monitors"]


def test_scan_monitor_ignores_legacy_limit(monkeypatch, monitor_files):
    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    products = [
        Product(
            asin=f"A{number}", title=f"Book {number}", price=number, length_minutes=60
        )
        for number in range(1, 4)
    ]
    monkeypatch.setattr(monitor_module, "_get_client", lambda locale: Client())
    monkeypatch.setattr(
        monitor_module, "_resolve_categories", lambda *args: ("", "", set())
    )
    monkeypatch.setattr(monitor_module, "_resolve_skip_asins", lambda *args: set())
    monkeypatch.setattr(
        monitor_module, "_fetch_with_progress", lambda *args, **kwargs: products
    )
    monkeypatch.setattr(
        monitor_module,
        "_apply_settings_filters",
        lambda *args, **kwargs: (products, {}, 0, 0, None),
    )
    assert len(monitor_module.scan_monitor(_definition(limit=1))) == 3


def test_track_history_shows_monitor_counts_for_new_and_legacy_runs():
    runs = [
        {
            "at": "2026-01-01T00:00:00",
            "duration_s": 1,
            "wishlist_checked": 1,
            "hits": 0,
            "error": None,
        },
        {
            "at": "2026-01-02T00:00:00",
            "duration_s": 2,
            "wishlist_checked": 2,
            "hits": 1,
            "monitors_checked": 3,
            "monitor_events": 4,
            "monitor_failures": ["broken"],
            "error": None,
        },
    ]
    with console.capture() as capture:
        display_track_history(runs)
    output = capture.get()
    assert "Monitors" in output
    assert "-" in output
    assert "3/4/1" in output
