"""Regression tests for confirmed bugs in audible_deals.cli.notify."""

from __future__ import annotations

import datetime
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import click
import pytest
from click.testing import CliRunner

import audible_deals.cli.notify as notify_mod
import audible_deals.constants as constants_mod
import audible_deals.wishlist as wishlist_mod
from audible_deals.cli import cli
from tests.conftest import make_product


# ===================================================================
# Bug 7: webhook POST must not follow redirects (SSRF + header leak)
# ===================================================================


class _RedirectServer(BaseHTTPRequestHandler):
    """First hop replies 302 to /followed; record any hit on /followed."""

    followed = False

    def _drain(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)

    def _record_followed(self):
        type(self).followed = True
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self._drain()
        if self.path == "/followed":
            self._record_followed()
            return
        self.send_response(302)
        self.send_header("Location", "/followed")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/followed":
            self._record_followed()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # silence test server logging
        pass


class TestWebhookNoRedirect:
    def test_post_webhook_does_not_follow_redirect(self, monkeypatch):
        """A 3xx from the validated host must not be followed: following it
        would reach an unvetted target (SSRF) and resend the auth header."""
        _RedirectServer.followed = False
        server = HTTPServer(("127.0.0.1", 0), _RedirectServer)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        # No real sleeps between the three retry attempts.
        monkeypatch.setattr(notify_mod.time, "sleep", lambda s: None)

        try:
            with pytest.raises(click.ClickException, match="Webhook failed"):
                notify_mod._post_webhook(
                    f"http://127.0.0.1:{port}/hook",
                    b"body",
                    {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer SECRET",
                    },
                )
        finally:
            server.shutdown()
            thread.join()

        assert _RedirectServer.followed is False


# ===================================================================
# Bug 9: recap validates flag combination before acquiring the lock
# ===================================================================


class TestRecapValidatesBeforeLock:
    def test_json_and_webhook_rejected_even_when_lock_held(
        self, tmp_config, mock_client
    ):
        """recap --json --webhook is a usage error regardless of lock state;
        it must not silently emit empty JSON when the lock is held."""
        lock_file = tmp_config / ".deals.lock"
        lock_file.write_text("99999")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["recap", "--json", "--webhook", "https://example.com/hook"],
        )
        assert result.exit_code == 2, result.output
        assert "mutually exclusive" in result.output


# ===================================================================
# Bug 8: --exit-code reflects whether items hit target, not whether a
# notification was actually sent (cooldown suppression must not flip it to 1)
# ===================================================================


class TestExitCodeWithCooldown:
    def _wishlist_at_target(self, mock_client, asin, price, target):
        wishlist_mod.save_wishlist(
            [{"asin": asin, "title": "Test Book", "max_price": target, "added": ""}]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin=asin, price=price, title="Test Book"),
        ]

    def test_exit_0_when_all_hits_suppressed_by_cooldown(self, mock_client, tmp_config):
        """An item is at target but suppressed by cooldown: --exit-code is 0
        because an item did hit target, even though nothing was sent."""
        self._wishlist_at_target(mock_client, "EC01", 3.99, 5.0)
        today = datetime.date.today().isoformat()
        constants_mod.NOTIFY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        constants_mod.NOTIFY_STATE_FILE.write_text(
            json.dumps({"EC01": {"price": 3.99, "date": today}})
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["notify", "--cooldown", "3", "--exit-code"])
        assert result.exit_code == 0, result.output

    def test_exit_1_when_no_item_hits_target(self, mock_client, tmp_config):
        """Nothing at target at all: --exit-code is 1."""
        self._wishlist_at_target(mock_client, "EC02", 9.99, 5.0)
        runner = CliRunner()
        result = runner.invoke(cli, ["notify", "--exit-code"])
        assert result.exit_code == 1, result.output
