"""Notification service behavior at external boundaries."""

import datetime
import importlib

import pytest
from click.testing import CliRunner

from audible_deals.automation_models import (
    MonitorDefinition,
    MonitorEvent,
    NotificationHit,
    NotificationRequest,
    NotificationRunResult,
)
from audible_deals.cli import cli
from audible_deals.notification_service import (
    NotificationRuntime,
    build_notify_state,
    commit_notification_state,
    deliver_monitor_events,
    run_notification,
)
from tests.conftest import make_product


class Client:
    def __init__(self, products=(), author_products=()):
        self.products = list(products)
        self.author_products = list(author_products)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_products_batch(self, asins):
        requested = set(asins)
        return [product for product in self.products if product.asin in requested]

    def search_pages(self, **kwargs):
        yield self.author_products, 1, len(self.author_products)


class Webhook:
    def __init__(self, events=None, error=None):
        self.events = events if events is not None else []
        self.error = error

    def post(self, url, body, headers):
        self.events.append(("post", url, dict(headers)))
        if self.error:
            raise self.error


def test_multi_locale_and_author_hits_are_collected_and_deduped():
    calls = []
    us_item = make_product(asin="US1", price=3, locale="us")
    uk_item = make_product(asin="UK1", price=2, locale="uk")
    author_item = make_product(
        asin="AUTHOR1", price=1, locale="us", authors=["Wanted Writer"]
    )
    clients = {
        "us": Client([us_item], [us_item, author_item]),
        "uk": Client([uk_item]),
    }
    wishlist = [
        {"asin": "US1", "max_price": 5, "locale": "us"},
        {"asin": "UK1", "max_price": 5, "locale": "uk"},
        {
            "type": "author",
            "author": "Wanted Writer",
            "max_price": 5,
        },
    ]

    result = run_notification(
        NotificationRequest("us", "$", None),
        NotificationRuntime(
            get_client=lambda locale: calls.append(locale) or clients[locale],
            record_products=lambda products: None,
            webhook_client=Webhook(),
            load_wishlist=lambda: wishlist,
        ),
    )

    assert calls == ["us", "uk", "us"]
    assert [hit.asin for hit in result.hits] == ["US1", "UK1", "AUTHOR1"]
    assert result.had_hits


def test_delivery_precedes_cooldown_save_and_failure_leaves_state_unchanged():
    today = datetime.date(2026, 8, 21)
    product = make_product(asin="HIT1", price=3)
    wishlist = [{"asin": "HIT1", "max_price": 5}]
    events = []
    saved = []
    runtime = NotificationRuntime(
        get_client=lambda locale: Client([product]),
        record_products=lambda products: None,
        webhook_client=Webhook(events),
        load_wishlist=lambda: wishlist,
        load_notify_state=lambda: {"BROKEN": {"price": "bad", "date": None}},
        save_notify_state=lambda state: (events.append(("save",)), saved.append(state)),
        today=lambda: today,
    )
    request = NotificationRequest(
        "us", "$", None, webhook="https://example.test", cooldown=3
    )

    result = run_notification(request, runtime)

    assert [event[0] for event in events] == ["post"]
    assert saved == []
    assert result.pending_notify_state == {"HIT1": {"price": 3, "date": "2026-08-21"}}

    events.append(("success",))
    commit_notification_state(result, runtime)
    assert [event[0] for event in events] == ["post", "success", "save"]
    assert saved == [{"HIT1": {"price": 3, "date": "2026-08-21"}}]

    failing_runtime = NotificationRuntime(
        get_client=lambda locale: Client([product]),
        record_products=lambda products: None,
        webhook_client=Webhook(error=RuntimeError("down")),
        load_wishlist=lambda: wishlist,
        load_notify_state=lambda: {},
        save_notify_state=lambda state: pytest.fail("state saved"),
        today=lambda: today,
    )
    with pytest.raises(RuntimeError, match="down"):
        run_notification(request, failing_runtime)


def test_no_cooldown_never_writes_notify_state():
    product = make_product(asin="HIT1", price=3)

    result = run_notification(
        NotificationRequest("us", "$", None),
        NotificationRuntime(
            get_client=lambda locale: Client([product]),
            record_products=lambda products: None,
            webhook_client=Webhook(),
            load_wishlist=lambda: [{"asin": "HIT1", "max_price": 5}],
            save_notify_state=lambda state: pytest.fail("state saved"),
        ),
    )

    assert result.pending_notify_state is None


def test_pending_notify_state_is_detached_from_loaded_and_committed_values():
    loaded = {
        "OLD": {
            "price": 4,
            "date": "2026-08-20",
            "future": {"nested": [1]},
        }
    }
    pending = build_notify_state(
        loaded,
        (),
        [{"asin": "OLD"}],
        frozenset(),
        datetime.date(2026, 8, 21),
    )
    loaded["OLD"]["future"]["nested"].append(2)
    result = NotificationRunResult(
        hits=(),
        had_hits=True,
        pending_notify_state=pending,
    )
    saved = []
    runtime = NotificationRuntime(
        get_client=lambda locale: Client(),
        record_products=lambda products: None,
        webhook_client=Webhook(),
        save_notify_state=saved.append,
    )

    commit_notification_state(result, runtime)
    saved[0]["OLD"]["future"]["nested"].append(3)

    assert result.pending_notify_state["OLD"]["future"]["nested"] == [1]


def test_monitor_override_never_receives_global_headers_even_for_same_url():
    sent = []
    event = MonitorEvent("new", "m", "A1", "Book", 3, 3, "url")
    monitor = MonitorDefinition.from_dict(
        {"name": "m", "locale": "us", "webhook": "https://same.test"}
    )

    deliver_monitor_events(
        (event,),
        monitor,
        "https://same.test",
        "generic",
        {"Authorization": "secret"},
        Webhook(sent),
    )

    assert sent[0][2].get("Authorization") is None


def _pending_result() -> NotificationRunResult:
    hit = NotificationHit("A1", "Book", 3, 5, "https://example.test/A1")
    return NotificationRunResult(
        hits=(hit,),
        had_hits=True,
        pending_notify_state={"A1": {"price": 3, "date": "2026-08-21"}},
    )


def test_notify_stdout_failure_prevents_pending_state_commit(tmp_config, monkeypatch):
    notify_cli = importlib.import_module("audible_deals.cli.notify")
    commits = []
    monkeypatch.setattr(notify_cli, "run_notification", lambda *args: _pending_result())
    monkeypatch.setattr(
        notify_cli, "commit_notification_state", lambda *args: commits.append(1)
    )
    monkeypatch.setattr(
        notify_cli.click,
        "echo",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("writer failed")),
    )

    result = CliRunner().invoke(cli, ["notify"])

    assert isinstance(result.exception, RuntimeError)
    assert commits == []


def test_notify_save_failure_occurs_after_deals_json_output(tmp_config, monkeypatch):
    notify_cli = importlib.import_module("audible_deals.cli.notify")
    monkeypatch.setattr(notify_cli, "run_notification", lambda *args: _pending_result())
    monkeypatch.setattr(
        notify_cli,
        "commit_notification_state",
        lambda *args: (_ for _ in ()).throw(RuntimeError("save failed")),
    )

    result = CliRunner().invoke(cli, ["notify"])

    assert isinstance(result.exception, RuntimeError)
    assert '"deals"' in result.output
    assert '"asin": "A1"' in result.output


def test_notify_webhook_orders_post_success_output_then_state_save(
    tmp_config, monkeypatch
):
    notify_cli = importlib.import_module("audible_deals.cli.notify")
    events = []

    def run(*args):
        events.append("post")
        return _pending_result()

    monkeypatch.setattr(notify_cli, "run_notification", run)
    monkeypatch.setattr(notify_cli, "validate_webhook_url", lambda url: None)
    monkeypatch.setattr(
        notify_cli.console, "print", lambda message: events.append("success")
    )
    monkeypatch.setattr(
        notify_cli, "commit_notification_state", lambda *args: events.append("save")
    )

    result = CliRunner().invoke(
        cli, ["notify", "--webhook", "https://example.test/hook"]
    )

    assert result.exit_code == 0, result.output
    assert events == ["post", "success", "save"]
