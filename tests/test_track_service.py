"""Track service orchestration and failure-state tests."""

import contextlib
import datetime
import importlib
from dataclasses import replace

import pytest
from click.testing import CliRunner

import audible_deals.track_service as track_service
from audible_deals import constants
from audible_deals.automation_models import (
    MonitorEvent,
    MonitorRunResult,
    TrackRunRequest,
)
from audible_deals.cli import cli
from audible_deals.config_store import load_track_state
from audible_deals.locking import run_lock
from audible_deals.monitor_service import MonitorRuntime
from audible_deals.track_service import (
    TrackRuntime,
    refresh_eligible_asins,
    run_track,
)
from audible_deals.webhook_client import WebhookDeliveryError
from tests.conftest import make_product


class Client:
    def __init__(self, error=None, products=()):
        self.error = error
        self.products = list(products)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_products_batch(self, asins):
        if self.error:
            raise self.error
        requested = set(asins)
        return [product for product in self.products if product.asin in requested]


class Webhook:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def post(self, url, body, headers):
        self.calls.append((url, body, dict(headers)))
        if self.error:
            raise self.error


def monitor_runtime():
    return MonitorRuntime(
        get_client=lambda locale: None,
        resolve_categories=lambda *args: ("", "", set()),
        resolve_skip_asins=lambda *args: set(),
        progress=lambda *args: None,
    )


def request(webhook=None):
    return TrackRunRequest("us", "$", None, 1, webhook, "generic", {})


def runtime(client, webhook, *, wishlist, monitors=lambda: {}):
    return TrackRuntime(
        get_client=lambda locale: client,
        record_products=lambda products: None,
        webhook_client=webhook,
        monitor_runtime=monitor_runtime(),
        load_wishlist=lambda: wishlist,
        load_refresh_eligibility=lambda: {},
        load_dismissed_asins=lambda: set(),
        load_monitors=monitors,
        load_notify_state=lambda: {},
        save_notify_state=lambda state: None,
        lock=run_lock,
    )


def test_refresh_eligibility_is_stalest_first_capped_and_excludes_wishlist():
    today = datetime.date(2026, 8, 21)
    eligibility = {
        "us": {
            f"A{index:03d}": (today - datetime.timedelta(days=index % 30)).isoformat()
            for index in range(250)
        }
    }

    selected, cursor = refresh_eligible_asins({"A029"}, eligibility, "us", today, 0)

    assert len(selected) == 200
    assert "A029" not in selected
    assert selected[0] == "A059"
    assert cursor == {
        "surfaced_on": eligibility["us"][selected[-1]],
        "asin": selected[-1],
    }


def test_refresh_eligibility_uses_current_locale_exact_window_and_exclusions():
    today = datetime.date(2026, 8, 21)
    eligibility = {
        "us": {
            "DAY30": (today - datetime.timedelta(days=30)).isoformat(),
            "DAY31": (today - datetime.timedelta(days=31)).isoformat(),
            "WISHLIST": today.isoformat(),
            "DISMISSED": today.isoformat(),
        },
        "uk": {"UKONLY": today.isoformat()},
    }

    selected, cursor = refresh_eligible_asins(
        {"WISHLIST", "DISMISSED"}, eligibility, "us", today, 0
    )

    assert selected == ["DAY30"]
    assert cursor == {
        "surfaced_on": eligibility["us"]["DAY30"],
        "asin": "DAY30",
    }


def test_refresh_cursor_is_stable_across_membership_churn():
    today = datetime.date(2026, 8, 21)
    old_date = (today - datetime.timedelta(days=10)).isoformat()
    target = "A199Z"
    eligibility = {
        "us": {
            **{f"A{index:03d}": old_date for index in range(201)},
            target: old_date,
        }
    }

    first, cursor = refresh_eligible_asins(set(), eligibility, "us", today, None)
    assert len(first) == 200
    assert target not in first

    del eligibility["us"]["A000"]
    eligibility["us"]["NEWER"] = today.isoformat()
    second, _cursor = refresh_eligible_asins(set(), eligibility, "us", today, cursor)

    assert second[0] == target


@pytest.mark.parametrize(
    "cursor",
    [None, 200, {}, {"surfaced_on": "bad", "asin": "A001"}],
)
def test_missing_or_invalid_refresh_cursor_restarts_oldest(cursor):
    today = datetime.date(2026, 8, 21)
    eligibility = {
        "us": {
            "OLDER": (today - datetime.timedelta(days=2)).isoformat(),
            "NEWER": today.isoformat(),
        }
    }

    selected, _cursor = refresh_eligible_asins(set(), eligibility, "us", today, cursor)

    assert selected[0] == "OLDER"


def test_removed_cursor_member_resumes_by_stable_sort_key():
    today = datetime.date(2026, 8, 21)
    date = today.isoformat()
    eligibility = {"us": {"A001": date, "A003": date}}
    cursor = {"surfaced_on": date, "asin": "A002"}

    selected, _cursor = refresh_eligible_asins(set(), eligibility, "us", today, cursor)

    assert selected == ["A003", "A001"]


def test_track_rotates_capped_eligibility_and_persists_cursor(tmp_config):
    today = datetime.date(2026, 8, 21)
    eligibility = {"us": {f"A{index:03d}": today.isoformat() for index in range(250)}}
    calls = []

    class RecordingClient(Client):
        def get_products_batch(self, asins):
            calls.append(list(asins))
            return []

    state = {}

    track_runtime = replace(
        runtime(RecordingClient(), Webhook(), wishlist=[]),
        load_refresh_eligibility=lambda: eligibility,
        load_track_state=lambda: state,
        save_track_state=lambda updated: None,
        today=lambda: today,
    )

    run_track(request(), track_runtime)
    first = calls[1]
    run_track(request(), track_runtime)
    second = calls[3]

    assert first == [f"A{index:03d}" for index in range(200)]
    assert second[:50] == [f"A{index:03d}" for index in range(200, 250)]
    assert second[50:] == [f"A{index:03d}" for index in range(150)]
    assert state["refresh_cursors"]["us"] == {
        "surfaced_on": today.isoformat(),
        "asin": "A149",
    }


def test_partial_monitor_failure_isolated_and_success_saved(tmp_config, monkeypatch):
    definitions = {
        name: {
            "name": name,
            "enabled": True,
            "locale": "us",
            "mode": "find",
            "settings": {"pages": 1, "sort": "price"},
        }
        for name in ("bad", "good")
    }
    errors = []

    def fake_run(definition, runtime, deliver=None):
        if definition.name == "bad":
            raise RuntimeError("broken")
        event = MonitorEvent("new", "good", "A1", "Book", 3, 3, "url")
        return MonitorRunResult((event,), False)

    monkeypatch.setattr(track_service, "run_monitor", fake_run)
    monkeypatch.setattr(
        track_service,
        "record_monitor_error",
        lambda name, error: errors.append((name, str(error))),
    )

    result = run_track(
        request(),
        runtime(Client(), Webhook(), wishlist=[], monitors=lambda: definitions),
    )

    assert result.monitors_checked == 2
    assert result.monitor_events == 1
    assert result.monitor_failures == ("bad: RuntimeError: broken",)
    assert errors == [("bad", "broken")]
    assert load_track_state()["run_history"][0] == result.to_dict()


def test_auth_failure_reacquires_lock_writes_sparse_entry_and_latches_once(
    tmp_config,
):
    webhook = Webhook()
    real_lock = run_lock
    acquisitions = []

    @contextlib.contextmanager
    def counted_lock():
        with real_lock():
            acquisitions.append(1)
            yield

    auth_error = RuntimeError("Not authenticated. Run 'deals login' first.")
    track_runtime = runtime(
        Client(auth_error),
        webhook,
        wishlist=[{"asin": "A1", "max_price": 5}],
    )
    track_runtime = replace(track_runtime, lock=counted_lock)

    with pytest.raises(RuntimeError, match="Not authenticated"):
        run_track(request("https://example.test"), track_runtime)

    state = load_track_state()
    entry = state["run_history"][0]
    assert set(entry) == {"at", "duration_s", "error"}
    assert state["auth_error_notified"] is True
    assert len(webhook.calls) == 1
    assert len(acquisitions) == 2

    with pytest.raises(RuntimeError):
        run_track(request("https://example.test"), track_runtime)
    assert len(webhook.calls) == 1

    successful_runtime = replace(
        track_runtime,
        get_client=lambda locale: Client(),
        load_wishlist=lambda: [],
    )
    run_track(request("https://example.test"), successful_runtime)
    assert load_track_state().get("auth_error_notified") is not True


def test_auth_webhook_failure_does_not_latch_and_is_retried(tmp_config):
    webhook = Webhook(error=RuntimeError("webhook down"))
    auth_error = RuntimeError("Not authenticated")
    track_runtime = runtime(
        Client(auth_error),
        webhook,
        wishlist=[{"asin": "A1", "max_price": 5}],
    )

    for _ in range(2):
        with pytest.raises(RuntimeError, match="Not authenticated"):
            run_track(request("https://example.test"), track_runtime)

    assert len(webhook.calls) == 2
    assert load_track_state().get("auth_error_notified") is not True


def test_wishlist_webhook_failure_does_not_record_run_or_notify_state(tmp_config):
    product = make_product(asin="A1", price=3)
    webhook = Webhook(WebhookDeliveryError("Webhook failed: down"))
    notify_saves = []
    track_runtime = runtime(
        Client(products=[product]),
        webhook,
        wishlist=[{"asin": "A1", "max_price": 5}],
    )
    track_runtime = replace(
        track_runtime,
        save_notify_state=notify_saves.append,
    )

    with pytest.raises(WebhookDeliveryError, match="Webhook failed: down"):
        run_track(request("https://example.test"), track_runtime)

    assert notify_saves == []
    assert not constants.TRACK_STATE_FILE.exists()


def test_track_cli_preserves_webhook_error_prefix(tmp_config, monkeypatch):
    track_cli = importlib.import_module("audible_deals.cli.track")
    monkeypatch.setattr(
        track_cli,
        "run_track",
        lambda *args: (_ for _ in ()).throw(
            WebhookDeliveryError("Webhook failed: down")
        ),
    )

    result = CliRunner().invoke(cli, ["track", "run"])

    assert result.exit_code != 0
    assert "Webhook failed: down" in result.output
    assert "track run failed:" not in result.output
