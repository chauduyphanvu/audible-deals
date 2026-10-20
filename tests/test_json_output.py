"""``--json`` on the commands that report state: shapes, streams and failures.

Every command here prints exactly one JSON document on stdout, and nothing but
JSON. Human text (progress, hints, warnings) goes to stderr, and a failure
leaves stdout empty and prints the error object on stderr. The exact key sets
are pinned in ``tests/test_golden.py``; this module covers behavior.
"""

from __future__ import annotations

import json
import time

import pytest
from click.testing import CliRunner

from audible_deals import paths
from audible_deals import wishlist as wishlist_mod
from audible_deals.automation_models import MonitorSnapshot
from audible_deals.cli import cli
from audible_deals.config_store import save_config, save_monitors, save_profiles
from audible_deals.state_store import (
    TrackState,
    save_install_record,
    save_monitor_snapshot,
    save_track_state,
)
from tests.conftest import make_product


def run(*args):
    return CliRunner().invoke(cli, list(args))


def document(result, code=0):
    """The one JSON document on stdout (asserting the exit status first)."""
    assert result.exit_code == code, (result.output, result.stderr)
    return json.loads(result.stdout)


def error_object(result, code):
    assert result.exit_code == code, (result.output, result.stderr)
    assert result.stdout == ""
    error = json.loads(result.stderr.strip().splitlines()[-1])["error"]
    assert error["code"] == code
    return error


def _valid_auth(tmp_config):
    (tmp_config / "auth.json").write_text(
        json.dumps(
            {
                "access_token": "at",
                "refresh_token": "rt",
                "locale_code": "us",
                "expires": time.time() + 86400 * 30,
            }
        )
    )


def _wishlist(*items):
    wishlist_mod.save_wishlist(list(items))


def _watch_item(asin, target, title="Wishlisted"):
    return {"asin": asin, "title": title, "max_price": target, "added": ""}


# -- doctor -------------------------------------------------------------------


class TestDoctor:
    def test_all_good_is_ok(self, tmp_config, mock_client):
        _valid_auth(tmp_config)

        result = run("doctor", "--json")

        payload = document(result)
        assert payload["schema_version"] == 1
        assert payload["ok"] is True and payload["failures"] == 0
        assert {"name", "status", "detail"} == set(payload["checks"][0])
        assert {c["status"] for c in payload["checks"]} <= {"pass", "warn", "fail"}
        assert any(c["name"] == "Marketplace reachable" for c in payload["checks"])

    def test_a_failed_check_exits_one_but_still_prints_the_report(
        self, tmp_config, mock_client
    ):
        result = run("doctor", "--json")  # no auth file

        payload = document(result, code=1)
        assert payload["ok"] is False and payload["failures"] >= 1
        failing = [c for c in payload["checks"] if c["status"] == "fail"]
        assert failing and "deals login" in failing[0]["detail"]

    def test_warnings_are_counted_and_do_not_fail(self, tmp_config, mock_client):
        _valid_auth(tmp_config)
        paths.config_file().write_text(json.dumps({"max_price": 5.0, "typo_key": 1}))

        payload = document(run("doctor", "--json"))

        assert payload["ok"] is True and payload["warnings"] >= 1


# -- watch --------------------------------------------------------------------


class TestWatch:
    @pytest.fixture
    def prices(self, mock_client):
        mock_client.get_products_batch.return_value = [
            make_product(asin="W0AT00001", price=3.0, list_price=20.0),
            make_product(asin="W0NOT0001", price=15.0, list_price=15.0),
            make_product(asin="W0DISC001", price=12.0, list_price=20.0),
            make_product(asin="W0GONE001", price=None, list_price=None),
        ]
        return mock_client

    def _seed(self):
        _wishlist(
            _watch_item("W0AT00001", 5.0),
            _watch_item("W0NOT0001", 5.0),
            _watch_item("W0DISC001", 5.0),
            _watch_item("W0GONE001", 5.0),
            _watch_item("W0MISS001", 5.0, title="Not in the catalog"),
            {"type": "author", "author": "An Author", "max_price": 4.0, "added": ""},
        )

    def test_reports_every_item_with_its_status(self, tmp_config, prices):
        self._seed()

        payload = document(run("watch", "--json"))

        assert payload["schema_version"] == 1
        assert payload["hits"] == 1
        statuses = {item["asin"]: item["status"] for item in payload["items"]}
        assert statuses == {
            "W0AT00001": "buy",
            "W0NOT0001": "waiting",
            "W0DISC001": "discounted",
            "W0GONE001": "unavailable",
        }
        assert payload["missing"] == ["W0MISS001"]
        assert payload["author_watches"] == 1
        first = next(i for i in payload["items"] if i["asin"] == "W0AT00001")
        assert first["price"] == 3.0 and first["target"] == 5.0
        assert first["url"].startswith("https://")

    def test_buy_only_lists_only_the_hits_but_counts_them_all(self, tmp_config, prices):
        self._seed()

        payload = document(run("watch", "--json", "--buy-only"))

        assert [item["asin"] for item in payload["items"]] == ["W0AT00001"]
        assert payload["hits"] == 1

    def test_the_credit_price_adds_a_verdict(self, tmp_config, prices):
        self._seed()
        save_config({"credit_price": 10})

        payload = document(run("watch", "--json"))

        assert all("verdict" in item for item in payload["items"])
        assert any(item["verdict"] for item in payload["items"])

    def test_without_a_credit_price_the_verdict_is_null(self, tmp_config, prices):
        self._seed()

        payload = document(run("watch", "--json"))

        assert {item["verdict"] for item in payload["items"]} == {None}

    def test_an_empty_wishlist_is_still_a_document(self, tmp_config, mock_client):
        result = run("watch", "--json")

        payload = document(result)
        assert payload == {
            "schema_version": 1,
            "hits": 0,
            "items": [],
            "missing": [],
            "author_watches": 0,
        }
        assert "Wishlist is empty" in result.stderr
        assert "Wishlist is empty" not in result.stdout

    def test_only_author_watches_report_no_items(self, tmp_config, mock_client):
        _wishlist(
            {"type": "author", "author": "An Author", "max_price": 4.0, "added": ""}
        )

        payload = document(run("watch", "--json"))

        assert payload["items"] == [] and payload["author_watches"] == 1
        mock_client.get_products_batch.assert_not_called()

    def test_exit_code_flag_still_applies(self, tmp_config, prices):
        _wishlist(_watch_item("W0NOT0001", 5.0))

        result = run("watch", "--json", "--exit-code")

        assert result.exit_code == 10
        assert json.loads(result.stdout)["hits"] == 0

    def test_it_is_a_single_check(self, tmp_config, mock_client):
        result = run("watch", "--json", "--every", "30m")

        error = error_object(result, 2)
        assert "--every" in error["message"]

    def test_the_human_table_is_unchanged_without_the_flag(self, tmp_config, prices):
        self._seed()

        result = run("watch")

        assert result.exit_code == 0
        assert "Wishlist Price Check" in result.stdout
        assert "BUY" in result.stdout

    def test_a_corrupt_wishlist_warns_on_stderr_and_stdout_stays_json(
        self, tmp_config, mock_client
    ):
        paths.wishlist_file().write_text("{not json")

        result = run("watch", "--json")

        payload = document(result)
        assert payload["items"] == []
        assert "Warning:" in result.stderr


# -- notify -------------------------------------------------------------------


class TestNotify:
    def _hit(self, mock_client):
        _wishlist(_watch_item("N0HIT0001", 5.0))
        mock_client.get_products_batch.return_value = [
            make_product(asin="N0HIT0001", price=3.0)
        ]

    def test_an_empty_wishlist_prints_valid_json_and_the_hint_on_stderr(
        self, tmp_config, mock_client
    ):
        result = run("notify")

        assert document(result) == {"deals": [], "count": 0}
        assert "Wishlist is empty" in result.stderr
        assert "Wishlist is empty" not in result.stdout

    def test_the_flag_changes_nothing_without_a_webhook(self, tmp_config, mock_client):
        self._hit(mock_client)

        plain = document(run("notify"))
        flagged = document(run("notify", "--json"))

        assert plain == flagged
        assert plain["count"] == 1 and plain["deals"][0]["asin"] == "N0HIT0001"
        assert "schema_version" not in plain  # its shape is unchanged

    def test_with_a_webhook_the_flag_prints_the_deals_too(
        self, tmp_config, mock_client, monkeypatch
    ):
        from audible_deals.webhook_client import WebhookClient

        self._hit(mock_client)
        posts = []
        monkeypatch.setattr(
            WebhookClient, "post", lambda self, *args: posts.append(args)
        )
        monkeypatch.setattr(
            "audible_deals.cli.notify.validate_webhook_url", lambda url: None
        )

        result = run("notify", "--webhook", "https://example.com/hook", "--json")

        assert document(result)["count"] == 1
        assert len(posts) == 1
        assert "Sent 1 deal(s) to webhook" in result.stderr

    def test_with_a_webhook_and_no_flag_stdout_stays_empty(
        self, tmp_config, mock_client, monkeypatch
    ):
        from audible_deals.webhook_client import WebhookClient

        self._hit(mock_client)
        monkeypatch.setattr(WebhookClient, "post", lambda self, *args: None)
        monkeypatch.setattr(
            "audible_deals.cli.notify.validate_webhook_url", lambda url: None
        )

        result = run("notify", "--webhook", "https://example.com/hook")

        assert result.exit_code == 0
        assert result.stdout.strip().startswith("Sent 1 deal(s)")

    def test_no_hits_with_the_flag_and_a_webhook_is_an_empty_document(
        self, tmp_config, mock_client, monkeypatch
    ):
        _wishlist(_watch_item("N0LOW0001", 1.0))
        mock_client.get_products_batch.return_value = [
            make_product(asin="N0LOW0001", price=9.0)
        ]
        monkeypatch.setattr(
            "audible_deals.cli.notify.validate_webhook_url", lambda url: None
        )

        result = run("notify", "--webhook", "https://example.com/hook", "--json")

        assert document(result) == {"deals": [], "count": 0}
        assert "Nothing sent" in result.stderr

    def test_a_busy_lock_still_prints_a_document(
        self, tmp_config, mock_client, monkeypatch
    ):
        from audible_deals.locking import LockHeldError

        class Busy:
            def __enter__(self):
                raise LockHeldError("busy")

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr("audible_deals.cli.notify.run_lock", lambda: Busy())

        result = run("notify", "--json")

        assert document(result) == {"deals": [], "count": 0}
        assert "Note: another deals notify/recap run is in progress" in result.stderr

    def test_cooldown_zero_means_off(self, tmp_config, mock_client):
        self._hit(mock_client)

        first = document(run("notify", "--cooldown", "0"))
        second = document(run("notify", "--cooldown", "0"))

        assert first["count"] == second["count"] == 1

    def test_webhook_formats_come_from_the_shared_list(self):
        from audible_deals.cli import notify as notify_module
        from audible_deals.constants import WEBHOOK_FORMATS

        option = next(
            p for p in notify_module.notify.params if p.name == "webhook_format"
        )
        assert tuple(option.type.choices) == tuple(WEBHOOK_FORMATS)


# -- track status ---------------------------------------------------------------


class TestTrackStatus:
    @pytest.fixture(autouse=True)
    def _no_scheduler(self, monkeypatch):
        import audible_deals.cli.track as track_mod

        monkeypatch.setattr(track_mod.scheduler, "installed", lambda: (False, "none"))

    def test_nothing_installed(self, tmp_config):
        payload = document(run("track", "status", "--json"))

        assert payload == {
            "schema_version": 1,
            "installed": False,
            "schedule": None,
            "monitors": {"enabled": 0, "with_errors": []},
            "last_run": None,
        }

    def test_installed_with_a_run(self, tmp_config, monkeypatch):
        import audible_deals.cli.track as track_mod

        monkeypatch.setattr(track_mod.scheduler, "installed", lambda: (True, "plist"))
        save_install_record(
            {
                "every": "6h",
                "method": "launchd agent",
                "installed_at": "2026-06-01T09:00:00",
            }
        )
        run_record = {
            "at": "2026-06-09T08:00:00",
            "duration_s": 4.2,
            "wishlist_checked": 3,
            "hits": 1,
        }
        save_track_state(TrackState(runs=[run_record]))

        payload = document(run("track", "status", "--json"))

        assert payload["installed"] is True
        assert payload["schedule"] == {
            "every": "6h",
            "method": "launchd agent",
            "installed_at": "2026-06-01T09:00:00",
            "present": True,
            "where": "plist",
        }
        assert payload["last_run"]["hits"] == 1
        assert "history" not in payload

    def test_history_adds_every_run(self, tmp_config):
        save_track_state(
            TrackState(
                runs=[
                    {"at": "2026-06-10T10:00:00", "hits": 0},
                    {"at": "2026-06-10T04:00:00", "error": "boom"},
                ]
            )
        )

        payload = document(run("track", "status", "--json", "--history"))

        assert [r["at"] for r in payload["history"]] == [
            "2026-06-10T10:00:00",
            "2026-06-10T04:00:00",
        ]
        assert payload["last_run"] == payload["history"][0]

    def test_the_group_default_is_still_the_human_status(self, tmp_config):
        result = run("track")

        assert result.exit_code == 0
        assert "Not installed" in result.output

    def test_monitors_with_errors_are_named(self, tmp_config):
        save_monitors({"good": _monitor_definition(), "bad": _monitor_definition()})
        save_monitor_snapshot("bad", MonitorSnapshot(last_error="boom"))

        payload = document(run("track", "status", "--json"))

        assert payload["monitors"] == {"enabled": 2, "with_errors": ["bad"]}


# -- monitors -------------------------------------------------------------------


def _monitor_definition(name="cheap", **overrides):
    definition = {
        "version": 1,
        "name": name,
        "enabled": True,
        "locale": "us",
        "mode": "search",
        "query": "sanderson",
        "settings": {"pages": 2, "limit": None, "sort": "price", "max_price": 5.0},
        "webhook": "https://hooks.example.test/secret-token",
        "webhook_headers": ["Authorization: Bearer hunter2"],
    }
    definition.update(overrides)
    return definition


class TestMonitors:
    def test_list(self, tmp_config):
        save_monitors(
            {
                "cheap": _monitor_definition(),
                "paused": _monitor_definition("paused", enabled=False, webhook=None),
            }
        )
        save_monitor_snapshot("cheap", MonitorSnapshot(initialized=True))

        result = run("monitor", "list", "--json")

        payload = document(result)
        assert payload["schema_version"] == 1
        by_name = {m["name"]: m for m in payload["monitors"]}
        assert list(by_name) == ["cheap", "paused"]
        cheap = by_name["cheap"]
        assert cheap["valid"] is True and cheap["enabled"] is True
        assert cheap["mode"] == "search" and cheap["query"] == "sanderson"
        assert cheap["has_webhook"] is True
        assert isinstance(cheap["estimated_calls"], int)
        assert by_name["paused"]["enabled"] is False
        assert by_name["paused"]["has_webhook"] is False

    def test_secrets_never_appear(self, tmp_config):
        save_monitors({"cheap": _monitor_definition()})

        for args in (("monitor", "list"), ("monitor", "show", "cheap")):
            result = run(*args, "--json")
            assert "secret-token" not in result.output
            assert "hunter2" not in result.output

    def test_no_monitors_is_an_empty_list(self, tmp_config):
        assert document(run("monitor", "list", "--json")) == {
            "schema_version": 1,
            "monitors": [],
        }

    def test_a_malformed_monitor_is_reported_not_fatal(self, tmp_config):
        save_monitors(
            {"good": _monitor_definition(), "broken": {"version": 1, "mode": "nope"}}
        )

        payload = document(run("monitor", "list", "--json"))

        by_name = {m["name"]: m for m in payload["monitors"]}
        assert by_name["good"]["valid"] is True
        assert by_name["broken"]["valid"] is False
        assert by_name["broken"]["error"]

    def test_show(self, tmp_config):
        save_monitors({"cheap": _monitor_definition()})
        save_monitor_snapshot(
            "cheap",
            MonitorSnapshot(initialized=True, last_error="rate limited"),
        )

        payload = document(run("monitor", "show", "cheap", "--json"))

        monitor = payload["monitor"]
        assert payload["schema_version"] == 1
        assert monitor["name"] == "cheap"
        assert monitor["settings"]["max_price"] == 5.0
        assert monitor["last_error"] == "rate limited"

    def test_show_unknown_is_a_json_error(self, tmp_config):
        error = error_object(run("monitor", "show", "ghost", "--json"), 1)
        assert "ghost" in error["message"]

    def test_a_corrupt_file_warns_and_lists_nothing(self, tmp_config):
        paths.monitors_file().write_text("{not json")

        result = run("monitor", "list", "--json")

        assert document(result)["monitors"] == []
        assert "Warning:" in result.stderr


# -- config and profiles ------------------------------------------------------


class TestConfig:
    def test_list_redacts_secrets(self, tmp_config):
        save_config(
            {
                "max_price": 5.0,
                "webhook": "https://hooks.example.test/secret-token",
                "webhook_headers": ["Authorization: Bearer hunter2"],
            }
        )

        result = run("config", "list", "--json")

        payload = document(result)
        assert payload["schema_version"] == 1
        assert payload["config"]["max_price"] == 5.0
        assert payload["config"]["webhook"] == "<redacted>"
        assert payload["config"]["webhook_headers"] == ["Authorization: <redacted>"]
        assert "secret-token" not in result.output and "hunter2" not in result.output

    def test_list_can_show_secrets_on_request(self, tmp_config):
        save_config({"webhook": "https://hooks.example.test/secret-token"})

        payload = document(run("config", "list", "--json", "--show-secrets"))

        assert payload["config"]["webhook"].endswith("secret-token")

    def test_nothing_set_is_an_empty_object(self, tmp_config):
        assert document(run("config", "list", "--json")) == {
            "schema_version": 1,
            "config": {},
        }

    def test_get_a_set_key(self, tmp_config):
        save_config({"max_price": 5.0})

        payload = document(run("config", "get", "max-price", "--json"))

        assert payload == {
            "schema_version": 1,
            "key": "max_price",
            "set": True,
            "value": 5.0,
        }

    def test_get_an_unset_key(self, tmp_config):
        payload = document(run("config", "get", "max-price", "--json"))

        assert payload["set"] is False and payload["value"] is None

    def test_get_redacts_the_webhook(self, tmp_config):
        save_config({"webhook": "https://hooks.example.test/secret-token"})

        payload = document(run("config", "get", "webhook", "--json"))

        assert payload["value"] == "<redacted>"

    def test_get_an_unknown_key_is_a_json_error(self, tmp_config):
        error = error_object(run("config", "get", "bogus", "--json"), 1)
        assert "bogus" in error["message"]

    def test_a_corrupt_file_warns_and_stdout_stays_json(self, tmp_config):
        paths.config_file().write_text("{not json")

        result = run("config", "list", "--json")

        assert document(result)["config"] == {}
        assert "Warning:" in result.stderr


class TestProfiles:
    def test_list(self, tmp_config):
        save_profiles(
            {
                "scifi": {"genre": "sci-fi", "max_price": 5.0, "skip_owned": True},
                "broken": {"genre": 12},
            }
        )

        payload = document(run("profile", "list", "--json"))

        by_name = {p["name"]: p for p in payload["profiles"]}
        assert payload["schema_version"] == 1
        assert by_name["scifi"] == {
            "name": "scifi",
            "valid": True,
            "options": {"genre": "sci-fi", "max_price": 5.0, "skip_owned": True},
        }
        assert by_name["broken"]["valid"] is False
        assert by_name["broken"]["options"] is None
        assert by_name["broken"]["error"]

    def test_none_saved_is_an_empty_list(self, tmp_config):
        assert document(run("profile", "list", "--json")) == {
            "schema_version": 1,
            "profiles": [],
        }

    def test_show(self, tmp_config):
        save_profiles({"scifi": {"genre": "sci-fi", "max_price": 5.0}})

        payload = document(run("profile", "show", "scifi", "--json"))

        assert payload == {
            "schema_version": 1,
            "profile": {
                "name": "scifi",
                "options": {"genre": "sci-fi", "max_price": 5.0},
            },
        }

    @pytest.mark.parametrize("profiles", [{}, {"broken": {"genre": 12}}])
    def test_show_a_missing_or_malformed_profile_is_a_json_error(
        self, tmp_config, profiles
    ):
        save_profiles(profiles)
        name = "broken" if profiles else "ghost"

        error = error_object(run("profile", "show", name, "--json"), 1)

        assert name in error["message"]

    def test_a_corrupt_file_warns_and_stdout_stays_json(self, tmp_config):
        paths.profiles_file().write_text("{not json")

        result = run("profile", "list", "--json")

        assert document(result)["profiles"] == []
        assert "Warning:" in result.stderr


# -- categories, detail, compare ---------------------------------------------


class TestCatalogLookups:
    def test_categories(self, tmp_config, mock_client):
        mock_client.get_categories.return_value = [
            {"id": "18580606011", "name": "Science Fiction & Fantasy"},
            {"id": "18574426011", "name": "Mystery"},
        ]

        result = run("categories", "--json")

        assert document(result) == {
            "schema_version": 1,
            "locale": "us",
            "parent": None,
            "categories": [
                {"id": "18580606011", "name": "Science Fiction & Fantasy"},
                {"id": "18574426011", "name": "Mystery"},
            ],
        }
        assert "Tip:" not in result.stdout

    def test_subcategories_name_their_parent(self, tmp_config, mock_client):
        mock_client.get_categories.return_value = [{"id": "1", "name": "Fantasy"}]

        payload = document(run("categories", "--parent", "18580606011", "--json"))

        assert payload["parent"] == "18580606011"
        mock_client.get_categories.assert_called_once_with(root="18580606011")

    def test_detail(self, tmp_config, mock_client):
        mock_client.get_product.return_value = make_product(
            asin="D0DETAIL01", price=4.0, list_price=20.0
        )

        save_config({"credit_price": 10})

        payload = document(run("detail", "D0DETAIL01", "--json"))

        assert payload["schema_version"] == 1
        assert payload["product"]["asin"] == "D0DETAIL01"
        assert payload["credit_price"] == 10.0
        assert payload["verdict"]
        assert isinstance(payload["effective_price"], float)

    def test_detail_without_a_credit_price(self, tmp_config, mock_client):
        mock_client.get_product.return_value = make_product(asin="D0DETAIL02")

        payload = document(run("detail", "D0DETAIL02", "--json"))

        assert payload["credit_price"] is None
        assert payload["verdict"] is None and payload["effective_price"] is None

    def test_compare(self, tmp_config, mock_client):
        mock_client.get_products_batch.return_value = [
            make_product(asin="C0MPARE001", price=4.0),
            make_product(asin="C0MPARE002", price=8.0),
        ]

        result = run("compare", "C0MPARE001", "C0MPARE002", "C0MISSING1", "--json")

        payload = document(result)
        assert payload["schema_version"] == 1
        assert [p["asin"] for p in payload["products"]] == ["C0MPARE001", "C0MPARE002"]
        assert payload["missing"] == ["C0MISSING1"]
        assert payload["credit_price"] is None
        assert "Not found: C0MISSING1" in result.stderr

    def test_compare_with_too_few_products_is_a_json_error(
        self, tmp_config, mock_client
    ):
        mock_client.get_products_batch.return_value = [make_product(asin="C0MPARE001")]

        error = error_object(run("compare", "C0MPARE001", "C0MISSING1", "--json"), 1)

        assert "at least 2" in error["message"]

    def test_detail_not_found_is_a_json_error(self, tmp_config, mock_client):
        mock_client.get_product.side_effect = ValueError("No such product: X0NONE0001")

        error = error_object(run("detail", "X0NONE0001", "--json"), 1)

        assert "X0NONE0001" in error["message"]


# -- cross-cutting ------------------------------------------------------------

#: One invocation per command that needs a login, with whatever it needs first.
NEEDS_LOGIN = {
    "categories": lambda: ("categories", "--json"),
    "detail": lambda: ("detail", "A0DETAIL01", "--json"),
    "compare": lambda: ("compare", "A0COMPARE1", "A0COMPARE2", "--json"),
    "watch": lambda: (_wishlist(_watch_item("A0WATCH001", 5.0)), ("watch", "--json"))[
        1
    ],
    "notify": lambda: (_wishlist(_watch_item("A0WATCH001", 5.0)), ("notify", "--json"))[
        1
    ],
}


@pytest.mark.parametrize("name", sorted(NEEDS_LOGIN))
def test_without_a_login_stdout_is_empty_and_stderr_has_the_error_object(name, api):
    (api.tmp_path / "auth.json").unlink()
    args = NEEDS_LOGIN[name]()

    result = run(*args)

    error = error_object(result, 3)
    assert "deals login" in error["message"]


class TestStreams:
    def test_the_console_is_restored_after_a_json_run(self, tmp_config, mock_client):
        _valid_auth(tmp_config)
        save_config({"max_price": 5.0})

        document(run("doctor", "--json"))
        after = run("config", "list")

        assert after.exit_code == 0
        assert "max_price" in after.stdout  # human output is back on stdout
        assert "max_price" not in after.stderr

    def test_the_console_is_restored_even_when_the_command_fails(
        self, tmp_config, mock_client
    ):
        run("monitor", "show", "ghost", "--json")
        save_config({"max_price": 5.0})

        after = run("config", "list")

        assert "max_price" in after.stdout

    def test_human_text_never_reaches_stdout_in_json_mode(
        self, tmp_config, mock_client
    ):
        # A progress/hint line and a warning in one run: stdout must stay one
        # parseable document.
        paths.profiles_file().write_text("{not json")
        mock_client.get_categories.return_value = [{"id": "1", "name": "Mystery"}]

        result = run("categories", "--json")

        assert document(result)["categories"]
        assert "Tip:" not in result.stdout
        assert result.stdout.lstrip().startswith("{")

    @pytest.mark.parametrize(
        "args",
        [
            ("config", "list"),
            ("profile", "list"),
            ("monitor", "list"),
            ("track", "status"),
        ],
    )
    def test_the_human_form_is_unchanged_without_the_flag(self, tmp_config, args):
        result = run(*args)

        assert result.exit_code == 0
        with pytest.raises(json.JSONDecodeError):
            json.loads(result.stdout)

    def test_a_database_failure_is_a_json_error(self, tmp_config, monkeypatch):
        import sqlite3

        from audible_deals import db

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(db, "state", broken)

        error = error_object(run("track", "status", "--json"), 1)

        assert "Local database error" in error["message"]
