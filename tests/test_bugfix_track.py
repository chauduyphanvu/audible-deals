"""Regression tests for confirmed bugs in audible_deals.cli.track."""

from __future__ import annotations

import json

from click.testing import CliRunner

import audible_deals.cli.track as track_mod
import audible_deals.config_store as config_store_mod
import audible_deals.constants as constants_mod
import audible_deals.wishlist as wishlist_mod
from audible_deals.cli import cli
from tests.conftest import make_product


def _seed_wishlist():
    wishlist_mod.save_wishlist(
        [{"asin": "B00TRACK01", "title": "Tracked", "max_price": 5.0, "added": ""}]
    )


def _config_with_webhook():
    config_store_mod.save_config(
        {"webhook": "https://example.com/hook", "webhook_format": "generic"}
    )


# ===================================================================
# Bug 22: non-auth failures must not be mislabeled as auth errors and
# must not latch auth_error_notified.
# ===================================================================


class TestAuthErrorClassification:
    def test_transient_error_does_not_ping_or_latch(
        self, mock_client, tmp_config, monkeypatch
    ):
        _seed_wishlist()
        _config_with_webhook()
        mock_client.get_products_batch.side_effect = RuntimeError(
            "connection timed out"
        )
        posts = []
        monkeypatch.setattr(track_mod, "post_webhook", lambda *a, **kw: posts.append(a))

        runner = CliRunner()
        result = runner.invoke(cli, ["track", "run"])
        assert result.exit_code != 0

        state = json.loads(constants_mod.TRACK_STATE_FILE.read_text())
        assert "connection timed out" in state["run_history"][0]["error"]
        # A transient (non-auth) failure must not fire the re-authenticate ping
        assert posts == []
        # ...and must not suppress future genuine auth pings
        assert state.get("auth_error_notified") is not True

    def test_genuine_auth_error_pings(self, mock_client, tmp_config, monkeypatch):
        _seed_wishlist()
        _config_with_webhook()
        mock_client.get_products_batch.side_effect = RuntimeError(
            "Not authenticated. Run 'deals login' first."
        )
        posts = []
        monkeypatch.setattr(track_mod, "post_webhook", lambda *a, **kw: posts.append(a))

        runner = CliRunner()
        result = runner.invoke(cli, ["track", "run"])
        assert result.exit_code != 0

        state = json.loads(constants_mod.TRACK_STATE_FILE.read_text())
        assert len(posts) == 1
        assert state.get("auth_error_notified") is True

    def test_transient_then_auth_still_pings(
        self, mock_client, tmp_config, monkeypatch
    ):
        _seed_wishlist()
        _config_with_webhook()
        posts = []
        monkeypatch.setattr(track_mod, "post_webhook", lambda *a, **kw: posts.append(a))
        runner = CliRunner()

        mock_client.get_products_batch.side_effect = RuntimeError("network reset")
        runner.invoke(cli, ["track", "run"])
        assert posts == []  # transient: no ping, no latch

        mock_client.get_products_batch.side_effect = RuntimeError(
            "Not authenticated. Run 'deals login' first."
        )
        runner.invoke(cli, ["track", "run"])
        # The genuine auth failure must still produce an actionable ping
        assert len(posts) == 1

    def test_http_401_classified_as_auth(self, mock_client, tmp_config, monkeypatch):
        _seed_wishlist()
        _config_with_webhook()

        class _AuthHTTPError(Exception):
            status_code = 401

        mock_client.get_products_batch.side_effect = _AuthHTTPError("unauthorized")
        posts = []
        monkeypatch.setattr(track_mod, "post_webhook", lambda *a, **kw: posts.append(a))

        runner = CliRunner()
        runner.invoke(cli, ["track", "run"])
        assert len(posts) == 1


class TestWebhookFormattingError:
    def test_formatting_value_error_not_treated_as_auth(
        self, mock_client, tmp_config, monkeypatch
    ):
        _seed_wishlist()
        _config_with_webhook()
        mock_client.get_products_batch.return_value = [
            make_product(asin="B00TRACK01", price=3.99)
        ]

        def _boom(*a, **kw):
            raise ValueError("Unknown webhook format: 'bogus'")

        monkeypatch.setattr(track_mod, "format_webhook_payload", _boom)
        posts = []
        monkeypatch.setattr(track_mod, "post_webhook", lambda *a, **kw: posts.append(a))

        runner = CliRunner()
        result = runner.invoke(cli, ["track", "run"])
        assert result.exit_code != 0
        # The formatting error must surface as itself, not a re-auth ping,
        # and must not latch auth_error_notified.
        assert posts == []
        state = json.loads(constants_mod.TRACK_STATE_FILE.read_text())
        assert state.get("auth_error_notified") is not True
        assert "Unknown webhook format" in state["run_history"][0]["error"]


# ===================================================================
# Bug 23: failure-path state write must be serialized inside run_lock.
# ===================================================================


class TestFailurePathLocking:
    def test_failure_state_write_acquires_lock(
        self, mock_client, tmp_config, monkeypatch
    ):
        _seed_wishlist()
        mock_client.get_products_batch.side_effect = RuntimeError("boom")

        acquisitions = []
        real_run_lock = track_mod.run_lock

        import contextlib

        @contextlib.contextmanager
        def _counting_lock():
            with real_run_lock():
                acquisitions.append(1)
                yield

        monkeypatch.setattr(track_mod, "run_lock", _counting_lock)

        runner = CliRunner()
        result = runner.invoke(cli, ["track", "run"])
        assert result.exit_code != 0
        # The lock is taken once for the (failed) run and again to serialize
        # the failure-path state write.
        assert len(acquisitions) == 2

        state = json.loads(constants_mod.TRACK_STATE_FILE.read_text())
        assert "boom" in state["run_history"][0]["error"]

    def test_failure_state_write_falls_back_when_lock_held(
        self, mock_client, tmp_config, monkeypatch
    ):
        _seed_wishlist()
        mock_client.get_products_batch.side_effect = RuntimeError("boom")

        import contextlib

        from audible_deals.constants import LockHeldError

        real_run_lock = track_mod.run_lock
        calls = {"n": 0}

        @contextlib.contextmanager
        def _flaky_lock():
            calls["n"] += 1
            if calls["n"] == 1:
                # First (run) acquisition succeeds.
                with real_run_lock():
                    yield
            else:
                # Failure-path acquisition: simulate a concurrent holder.
                raise LockHeldError("held by another process")

        monkeypatch.setattr(track_mod, "run_lock", _flaky_lock)

        runner = CliRunner()
        result = runner.invoke(cli, ["track", "run"])
        assert result.exit_code != 0
        # Even when the lock can't be re-acquired, the failure must still be
        # recorded via the best-effort unlocked save.
        state = json.loads(constants_mod.TRACK_STATE_FILE.read_text())
        assert "boom" in state["run_history"][0]["error"]
