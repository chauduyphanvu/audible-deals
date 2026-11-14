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


class AccountClient(Client):
    """One marketplace's catalog plus the account's Audible wishlist."""

    def __init__(self, locale="us", products=(), wishlist=(), wishlist_error=None):
        super().__init__(products)
        self.locale = locale
        self.wishlist = list(wishlist)
        self.wishlist_error = wishlist_error
        self.calls = []

    def get_products_batch(self, asins):
        self.calls.append(("batch", list(asins)))
        return super().get_products_batch(asins)

    def get_wishlist(self):
        self.calls.append(("wishlist",))
        if self.wishlist_error:
            raise self.wishlist_error
        return list(self.wishlist)


def _credit_request(**kwargs):
    kwargs.setdefault("audible_wishlist", True)
    return NotificationRequest("us", "$", 11.25, **kwargs)


def _credit_runtime(clients, *, wishlist=(), dismissed=(), recorded=None, **kwargs):
    if not isinstance(clients, dict):
        clients = {"us": clients}
    return NotificationRuntime(
        get_client=lambda locale: clients[locale],
        record_products=(
            recorded.extend if recorded is not None else lambda products: None
        ),
        webhook_client=Webhook(),
        load_wishlist=lambda: list(wishlist),
        load_dismissed_asins=lambda: set(dismissed),
        **kwargs,
    )


def test_audible_wishlist_off_never_fetches_it():
    client = AccountClient(products=[make_product(asin="LOCAL1", price=3)])
    client.get_wishlist = lambda: pytest.fail("Audible wishlist fetched")

    result = run_notification(
        _credit_request(audible_wishlist=False),
        _credit_runtime(client, wishlist=[{"asin": "LOCAL1", "max_price": 5}]),
    )

    assert [hit.asin for hit in result.hits] == ["LOCAL1"]
    assert result.hits[0].reason is None
    assert client.calls == [("batch", ["LOCAL1"])]


def test_audible_wishlist_off_with_empty_local_wishlist_reports_empty():
    result = run_notification(
        _credit_request(audible_wishlist=False),
        _credit_runtime({}, wishlist=[]),
    )

    assert result.empty_wishlist


def test_audible_wishlist_books_alert_only_strictly_below_the_credit_price():
    client = AccountClient(
        wishlist=[
            make_product(asin="BELOW", price=5.99),
            make_product(asin="EQUAL", price=11.25),
            make_product(asin="ABOVE", price=14.95),
            make_product(asin="NOPRICE", price=None),
        ]
    )

    result = run_notification(_credit_request(), _credit_runtime(client, wishlist=[]))

    assert not result.empty_wishlist
    assert [hit.to_dict() for hit in result.hits] == [
        {
            "asin": "BELOW",
            "title": "Test Book",
            "price": 5.99,
            "target": 11.25,
            "url": "https://www.audible.com/pd/BELOW",
            "verdict": "cash",
            "effective_price": 5.99,
            "reason": "below_credit_price",
        }
    ]
    assert client.calls == [("wishlist",)]


def test_audible_wishlist_skips_dismissed_and_plus_titles():
    client = AccountClient(
        wishlist=[
            make_product(asin="DISMISSED", price=1),
            make_product(asin="PLUS", price=1, in_plus_catalog=True),
            make_product(asin="WANTED", price=1),
        ]
    )

    result = run_notification(
        _credit_request(),
        _credit_runtime(client, dismissed={"DISMISSED"}),
    )

    assert [hit.asin for hit in result.hits] == ["WANTED"]


def test_local_entry_governs_its_target_and_marketplace():
    us = AccountClient(
        wishlist=[
            make_product(asin="TARGETED", price=5),
            make_product(asin="UKENTRY", price=1),
        ]
    )
    uk = AccountClient("uk", products=[make_product(asin="UKENTRY", price=12)])
    wishlist = [
        {"asin": "TARGETED", "max_price": 4, "locale": "us"},
        {"asin": "UKENTRY", "max_price": 10, "locale": "uk"},
    ]

    result = run_notification(
        _credit_request(), _credit_runtime({"us": us, "uk": uk}, wishlist=wishlist)
    )

    assert result.hits == ()
    assert us.calls == [("wishlist",)]
    assert uk.calls == [("batch", ["UKENTRY"])]


def test_book_on_both_wishlists_is_fetched_once_and_alerts_once():
    recorded = []
    client = AccountClient(
        wishlist=[
            make_product(asin="TARGETED", price=3),
            make_product(asin="NOTARGET", price=3),
        ]
    )
    wishlist = [
        {"asin": "TARGETED", "max_price": 5, "locale": "us"},
        {"asin": "NOTARGET", "max_price": None},
    ]

    result = run_notification(
        _credit_request(),
        _credit_runtime(client, wishlist=wishlist, recorded=recorded),
    )

    assert [(hit.asin, hit.target, hit.reason) for hit in result.hits] == [
        ("TARGETED", 5, None),
        ("NOTARGET", 11.25, "below_credit_price"),
    ]
    assert client.calls == [("wishlist",)]
    assert sorted(product.asin for product in recorded) == ["NOTARGET", "TARGETED"]


@pytest.mark.parametrize(
    "credit_price, price, plus, locale, alerts",
    [
        (11.25, 5.0, False, "us", True),
        (11.25, 11.25, False, "us", False),
        (11.25, 12.0, False, "us", False),
        (11.25, 5.0, True, "us", False),
        (None, 0.5, False, "us", False),
        (11.25, 5.0, False, "uk", False),
    ],
)
def test_target_less_local_entry_alerts_only_below_a_configured_credit_price(
    credit_price, price, plus, locale, alerts
):
    client = AccountClient(
        locale,
        products=[make_product(asin="NOTARGET", price=price, in_plus_catalog=plus)],
    )
    client.get_wishlist = lambda: pytest.fail("Audible wishlist fetched")

    result = run_notification(
        NotificationRequest("us", "$", credit_price),
        _credit_runtime(
            {locale: client},
            wishlist=[{"asin": "NOTARGET", "max_price": None, "locale": locale}],
        ),
    )

    assert [hit.asin for hit in result.hits] == (["NOTARGET"] if alerts else [])
    if alerts:
        assert result.hits[0].target == credit_price
        assert result.hits[0].reason == "below_credit_price"


def test_below_credit_hit_respects_cooldown_until_the_price_falls_again():
    today = datetime.date(2026, 11, 14)
    client = AccountClient(wishlist=[make_product(asin="SALE1", price=5)])

    def run(previous_price):
        return run_notification(
            _credit_request(cooldown=7),
            _credit_runtime(
                client,
                load_notify_state=lambda: {
                    "SALE1": {"price": previous_price, "date": "2026-11-13"}
                },
                today=lambda: today,
            ),
        )

    repeat = run(5)
    assert repeat.hits == ()
    assert repeat.suppressed == 1
    assert repeat.had_hits

    dropped = run(6)
    assert [hit.asin for hit in dropped.hits] == ["SALE1"]
    assert dropped.pending_notify_state == {"SALE1": {"price": 5, "date": "2026-11-14"}}


def test_audible_wishlist_failure_keeps_local_hits_and_cooldown_entries():
    today = datetime.date(2026, 11, 14)
    client = AccountClient(
        products=[make_product(asin="LOCAL1", price=3)],
        wishlist_error=RuntimeError("wishlist endpoint down"),
    )

    result = run_notification(
        _credit_request(cooldown=3),
        _credit_runtime(
            client,
            wishlist=[{"asin": "LOCAL1", "max_price": 5}],
            load_notify_state=lambda: {"ACCOUNT1": {"price": 4, "date": "2026-11-13"}},
            today=lambda: today,
        ),
    )

    assert [hit.asin for hit in result.hits] == ["LOCAL1"]
    assert result.audible_wishlist_error == "RuntimeError: wishlist endpoint down"
    assert "ACCOUNT1" in result.pending_notify_state


def test_audible_wishlist_auth_failure_is_raised():
    client = AccountClient(
        products=[make_product(asin="LOCAL1", price=3)],
        wishlist_error=RuntimeError("Not authenticated. Run 'deals login' first."),
    )

    with pytest.raises(RuntimeError, match="Not authenticated"):
        run_notification(
            _credit_request(),
            _credit_runtime(client, wishlist=[{"asin": "LOCAL1", "max_price": 5}]),
        )


def test_audible_wishlist_books_are_recorded_in_the_account_marketplace(tmp_config):
    from audible_deals.price_history import load_price_history, record_prices

    client = AccountClient(
        "uk", wishlist=[make_product(asin="UKBOOK", price=20, locale="uk")]
    )

    run_notification(
        NotificationRequest("uk", "£", 7.0, audible_wishlist=True),
        NotificationRuntime(
            get_client=lambda locale: client,
            record_products=record_prices,
            webhook_client=Webhook(),
            load_wishlist=lambda: [],
            load_dismissed_asins=set,
        ),
    )

    assert load_price_history("UKBOOK", "uk")
    assert not load_price_history("UKBOOK", "us")
