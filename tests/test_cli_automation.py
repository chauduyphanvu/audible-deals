"""Automation, notification, recap, and tracking CLI behavior."""

from __future__ import annotations

import contextlib
import datetime
import datetime as _datetime
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import click
import pytest
from click.testing import CliRunner

import audible_deals.cli.track as track_mod
import audible_deals.config_store as config_store_mod
import audible_deals.constants as constants_mod
import audible_deals.notification_service as notification_service
import audible_deals.wishlist as wishlist_mod
from audible_deals.cli import cli
from audible_deals.cli.misc import _track_checks
from audible_deals.parsing import parse_interval as _parse_interval
from audible_deals.webhook_client import WebhookClient, WebhookDeliveryError
from tests.conftest import make_product


def _routes_run(runner, args, **kwargs):
    """Invoke the CLI and return the result; fail on unexpected errors."""
    result = runner.invoke(cli, args, catch_exceptions=False, **kwargs)
    return result


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


def _seed_tracked_wishlist():
    wishlist_mod.save_wishlist(
        [{"asin": "B00TRACK01", "title": "Tracked", "max_price": 5.0, "added": ""}]
    )


def _save_webhook_config():
    config_store_mod.save_config(
        {"webhook": "https://example.com/hook", "webhook_format": "generic"}
    )


class TestWatchCommand:
    def test_watch_empty(self, tmp_config, mock_client):
        runner = CliRunner()
        result = runner.invoke(cli, ["watch"])
        assert result.exit_code == 0, result.output
        assert "empty" in result.output

    def test_watch_with_items(self, mock_client, tmp_config):
        # Seed the wishlist

        wishlist_mod.save_wishlist(
            [
                {"asin": "W1", "title": "Book", "max_price": 10.0},
            ]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="W1", title="Book", price=5.0, list_price=20.0),
        ]

        runner = CliRunner()
        result = runner.invoke(cli, ["watch"])
        assert result.exit_code == 0, result.output
        assert "BUY" in result.output

    def test_watch_buy_only(self, mock_client, tmp_config):
        """--buy-only filters to only items at or below target."""

        wishlist_mod.save_wishlist(
            [
                {"asin": "W1", "title": "Cheap Book", "max_price": 10.0},
                {"asin": "W2", "title": "Expensive Book", "max_price": 3.0},
            ]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="W1", title="Cheap Book", price=5.0, list_price=20.0),
            make_product(
                asin="W2", title="Expensive Book", price=15.0, list_price=20.0
            ),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["watch", "--buy-only"])
        assert result.exit_code == 0, result.output
        assert "Cheap Book" in result.output
        assert "Expensive Book" not in result.output

    def test_watch_unavailable_precedes_discount_and_is_summarized_with_buy_only(
        self, mock_client, tmp_config
    ):
        wishlist_mod.save_wishlist(
            [
                {"asin": "W1", "title": "Unavailable", "max_price": 10.0},
                {"asin": "W2", "title": "Cheap", "max_price": 10.0},
            ]
        )
        mock_client.get_products_batch.return_value = [
            make_product(
                asin="W1",
                title="Unavailable",
                price=None,
                list_price=20.0,
            ),
            make_product(asin="W2", title="Cheap", price=5.0),
        ]
        runner = CliRunner()

        shown = runner.invoke(cli, ["watch"])
        buy_only = runner.invoke(cli, ["watch", "--buy-only"])

        assert shown.exit_code == 0, shown.output
        assert "unavailable" in shown.output
        assert "waiting" not in shown.output
        assert buy_only.exit_code == 0, buy_only.output
        assert "Unavailable" not in buy_only.output
        assert "1 unavailable" in buy_only.output

    def test_watch_sort_by_title(self, mock_client, tmp_config):
        """--sort title orders output alphabetically."""

        wishlist_mod.save_wishlist(
            [
                {"asin": "W1", "title": "Zebra Book", "max_price": 10.0},
                {"asin": "W2", "title": "Alpha Book", "max_price": 10.0},
            ]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="W1", title="Zebra Book", price=5.0),
            make_product(asin="W2", title="Alpha Book", price=5.0),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["watch", "--sort", "title"])
        assert result.exit_code == 0, result.output
        alpha_pos = result.output.index("Alpha Book")
        zebra_pos = result.output.index("Zebra Book")
        assert alpha_pos < zebra_pos

    def test_watch_show_url(self, mock_client, tmp_config):
        """--show-url adds URL column to output."""
        from io import StringIO

        from rich.console import Console

        import audible_deals.cli.wishlist as cli_wishlist_mod

        wishlist_mod.save_wishlist(
            [
                {"asin": "W1", "title": "URL Book", "max_price": 10.0},
            ]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="W1", title="URL Book", price=5.0),
        ]
        # Patch the console to use a wide fixed-width instance so Rich
        # does not truncate the URL cell value in a narrow test environment
        from audible_deals.presentation import terminal as display_mod

        buf = StringIO()
        wide_console = Console(file=buf, width=200, highlight=False)
        original_cli = cli_wishlist_mod.console
        original_display = display_mod.console
        cli_wishlist_mod.console = wide_console
        display_mod.console = wide_console
        try:
            runner = CliRunner()
            result = runner.invoke(cli, ["watch", "--show-url"])
        finally:
            cli_wishlist_mod.console = original_cli
            display_mod.console = original_display
        assert result.exit_code == 0, result.output
        captured = buf.getvalue()
        assert "URL" in captured
        assert "/pd/W1" in captured

    @pytest.mark.parametrize("sort_key", ["author", "asin"])
    def test_watch_sort_keys(self, mock_client, tmp_config, sort_key):
        """--sort author and --sort asin run without error."""

        wishlist_mod.save_wishlist(
            [
                {"asin": "W1", "title": "Book A", "max_price": 10.0},
                {"asin": "W2", "title": "Book B", "max_price": 10.0},
            ]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="W1", title="Book A", price=5.0, authors=["Zeta Author"]),
            make_product(
                asin="W2", title="Book B", price=5.0, authors=["Alpha Author"]
            ),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["watch", "--sort", sort_key])
        assert result.exit_code == 0, result.output


class TestRecapWithTitles:
    def _write_history(self, tmp_config, asin: str, entries: list[dict]) -> None:

        hist_dir = constants_mod.HISTORY_DIR
        hist_dir.mkdir(parents=True, exist_ok=True)
        (hist_dir / f"{asin}.json").write_text(
            json.dumps({"marketplaces": {"us": entries}})
        )

    def test_recap_shows_title_in_price_drop(self, tmp_config):
        """recap displays the book title alongside the ASIN for price drops."""
        import datetime

        today = datetime.date.today().isoformat()
        old_date = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        self._write_history(
            tmp_config,
            "DROPTITLE",
            [
                {"date": old_date, "price": 12.00, "title": "The Drop Book"},
                {"date": today, "price": 4.00, "title": "The Drop Book"},
            ],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "7", "--show-new"])
        assert result.exit_code == 0, result.output
        assert "The Drop Book" in result.output
        assert "DROPTITLE" in result.output

    def test_recap_fallback_no_title(self, tmp_config):
        """recap gracefully shows just the ASIN when history entries lack a title."""
        import datetime

        today = datetime.date.today().isoformat()
        old_date = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        # Old-format entries without "title" key
        self._write_history(
            tmp_config,
            "NOTITLE1",
            [
                {"date": old_date, "price": 10.00},
                {"date": today, "price": 3.00},
            ],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "7", "--show-new"])
        assert result.exit_code == 0, result.output
        assert "NOTITLE1" in result.output

    def test_recap_title_stored_in_record_prices(self, tmp_config):
        """_record_prices stores the title in history entries."""
        from audible_deals.price_history import record_prices as _record_prices

        p = make_product(asin="RC01", price=5.99, title="My Title Book")
        _record_prices([p])

        hist_file = constants_mod.HISTORY_DIR / "RC01.json"
        entries = json.loads(hist_file.read_text())["marketplaces"]["us"]
        assert len(entries) == 1
        assert entries[0]["title"] == "My Title Book"

    def test_recap_shows_title_for_new_items(self, tmp_config):
        """recap displays title for newly tracked items when --show-new is passed."""
        import datetime

        today = datetime.date.today().isoformat()
        self._write_history(
            tmp_config,
            "NEWBOOK1",
            [
                {"date": today, "price": 4.99, "title": "Brand New Book"},
            ],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "7", "--show-new"])
        assert result.exit_code == 0, result.output
        assert "Brand New Book" in result.output
        assert "NEWBOOK1" in result.output

    def test_recap_new_items_count_without_show_new(self, tmp_config):
        """recap shows count but not details for new items when --show-new is omitted."""
        import datetime

        today = datetime.date.today().isoformat()
        self._write_history(
            tmp_config,
            "NEWBOOK2",
            [
                {"date": today, "price": 4.99, "title": "Hidden New Book"},
            ],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "7"])
        assert result.exit_code == 0, result.output
        assert "Newly tracked: 1" in result.output
        assert "Hidden New Book" not in result.output

    def test_recap_stable_price_classified_as_new(self, tmp_config):
        """Items with 2+ entries all within window and no drop appear as newly tracked."""
        import datetime

        today = datetime.date.today().isoformat()
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        self._write_history(
            tmp_config,
            "STABLE01",
            [
                {"date": yesterday, "price": 5.99, "title": "Stable Book"},
                {"date": today, "price": 5.99, "title": "Stable Book"},
            ],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "7", "--show-new"])
        assert result.exit_code == 0, result.output
        assert "Stable Book" in result.output
        assert "STABLE01" in result.output

    def test_recap_price_increase_classified_as_new(self, tmp_config):
        """Items with 2+ entries all within window and a price increase appear as newly tracked."""
        import datetime

        today = datetime.date.today().isoformat()
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        self._write_history(
            tmp_config,
            "PRICEUP1",
            [
                {"date": yesterday, "price": 5.00, "title": "Price Up Book"},
                {"date": today, "price": 10.00, "title": "Price Up Book"},
            ],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "7", "--show-new"])
        assert result.exit_code == 0, result.output
        assert "Price Up Book" in result.output
        assert "PRICEUP1" in result.output


class TestParseInterval:
    def test_minutes(self):
        assert _parse_interval("30m") == 1800

    def test_hours(self):
        assert _parse_interval("2h") == 7200

    def test_combined(self):
        assert _parse_interval("1h30m") == 5400

    def test_seconds(self):
        assert _parse_interval("90s") == 90

    def test_plain_number_treated_as_minutes(self):
        assert _parse_interval("5") == 300

    def test_invalid_raises(self):
        with pytest.raises(click.BadParameter, match="Cannot parse"):
            _parse_interval("abc")

    def test_whitespace_stripped(self):
        assert _parse_interval("  30m  ") == 1800

    def test_zero_raises(self):
        with pytest.raises(click.BadParameter, match="positive"):
            _parse_interval("0")

    def test_zero_minutes_raises(self):
        with pytest.raises(click.BadParameter, match="positive"):
            _parse_interval("0m")

    def test_negative_raises(self):
        with pytest.raises(click.BadParameter, match="Cannot parse"):
            _parse_interval("-5m")

    def test_trailing_garbage_raises(self):
        with pytest.raises(click.BadParameter, match="Cannot parse"):
            _parse_interval("10h15x")


class TestWatchEveryFlag:
    def test_watch_help_shows_every(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["watch", "--help"])
        assert "--every" in result.output

    def test_watch_without_every_runs_once(self, mock_client, tmp_config):
        """watch without --every does a single check and exits."""

        wishlist_mod.save_wishlist(
            [{"asin": "W1", "title": "Test", "max_price": 10.0, "added": ""}]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="W1", price=5.0, title="Test"),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["watch"])
        assert result.exit_code == 0, result.output
        assert "BUY" in result.output


class TestNotifyEmptyWishlist:
    def test_notify_empty_wishlist(self, mock_client, tmp_config):
        """notify with an empty wishlist prints a helpful message."""
        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0, result.output
        assert "empty" in result.output.lower()
        assert "wishlist add" in result.output

    def test_notify_no_hits_outputs_empty_json(self, mock_client, tmp_config):
        """notify with items on wishlist but no hits outputs empty JSON object."""
        import json

        wishlist_mod.save_wishlist(
            [
                {"asin": "NT1", "title": "Some Book", "max_price": 5.0, "added": ""},
            ]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="NT1", price=10.0),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0, result.output
        assert "wishlist add" not in result.output
        parsed = json.loads(result.output)
        assert parsed == {"deals": [], "count": 0}


class TestRecapDaysValidation:
    def test_days_zero_rejected(self, tmp_config):
        """recap --days 0 is rejected as out of range."""
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "0"])
        assert result.exit_code != 0

    def test_days_negative_rejected(self, tmp_config):
        """recap --days -1 is rejected as out of range."""
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "-1"])
        assert result.exit_code != 0

    def test_days_one_accepted(self, tmp_config):
        """recap --days 1 is accepted (minimum valid value)."""
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "1"])
        assert result.exit_code == 0, result.output

    def test_days_default_accepted(self, tmp_config):
        """recap with no --days uses default of 7 and succeeds."""
        runner = CliRunner()
        result = runner.invoke(cli, ["recap"])
        assert result.exit_code == 0, result.output


class TestNotifyEmpty:
    def test_notify_no_hits_prints_empty_json(self, mock_client, tmp_config):
        """notify with wishlist items above target prints '[]' to stdout."""

        # Add a wishlist item with a low target (price above target = no hit)
        wishlist_mod.save_wishlist(
            [
                {
                    "asin": "NE01",
                    "title": "Pricey Book",
                    "max_price": 1.0,
                    "added": "2024-01-01",
                },
            ]
        )
        # Mock get_products_batch to return product with price above target
        mock_client.get_products_batch.return_value = [
            make_product(asin="NE01", price=9.99),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0, result.output
        assert "[]" in result.output

    def test_notify_no_hits_with_webhook_shows_feedback(
        self, mock_client, tmp_config, monkeypatch
    ):
        """notify with no hits and a webhook prints feedback but does not POST."""

        wishlist_mod.save_wishlist(
            [
                {
                    "asin": "NE02",
                    "title": "Pricey Book",
                    "max_price": 1.0,
                    "added": "2024-01-01",
                },
            ]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="NE02", price=9.99),
        ]
        # Use a valid-looking but unreachable webhook; should never be called
        monkeypatch.setattr(
            "audible_deals.cli.notify.validate_webhook_url", lambda url: None
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["notify", "--webhook", "https://example.com/hook"])
        assert result.exit_code == 0, result.output
        assert "[]" not in result.output
        assert "Nothing sent to webhook" in result.output


class TestNotifyZeroTarget:
    def test_notify_zero_target_fires(self, mock_client, tmp_config):
        """notify must fire when max_price=0 and product price is 0 (was falsy bug)."""

        wishlist_mod.save_wishlist(
            [
                {
                    "asin": "Z001",
                    "title": "Free Book",
                    "max_price": 0,
                    "added": "2024-01-01",
                },
            ]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="Z001", price=0.0),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["count"] == 1
        assert data["deals"][0]["asin"] == "Z001"


class TestWatchRecordsPrices:
    """watch command must persist fetched prices to history."""

    def test_watch_records_prices(self, mock_client, tmp_config):
        """After running watch, history should contain an entry for the watched ASIN."""
        from audible_deals.price_history import (
            load_price_history as _load_price_history,
        )

        wishlist_mod.save_wishlist(
            [
                {"asin": "WR1", "title": "Record Me", "max_price": 10.0, "added": ""},
            ]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="WR1", price=7.99, title="Record Me"),
        ]

        runner = CliRunner()
        result = runner.invoke(cli, ["watch"])
        assert result.exit_code == 0, result.output

        history = _load_price_history("WR1")
        assert len(history) == 1
        assert history[0]["price"] == 7.99


class TestPriceHistoryObservationDate:
    def test_backfill_is_chronological_idempotent_and_capped(self, tmp_config):
        from audible_deals.price_history import load_price_history, record_prices

        product = make_product(asin="B00BACK001", price=4.0, title="Backfill")
        record_prices([product], datetime.date(2026, 1, 3))
        record_prices([product], datetime.date(2026, 1, 1))
        product.price = 9.0
        record_prices([product], datetime.date(2026, 1, 1))

        entries = load_price_history(product.asin)
        assert [entry["date"] for entry in entries] == [
            "2026-01-01",
            "2026-01-03",
        ]
        assert entries[0]["price"] == 4.0

        seeded = [
            {
                "date": (
                    datetime.date(2025, 1, 1) + datetime.timedelta(days=day)
                ).isoformat(),
                "price": 4.0,
                "title": product.title,
            }
            for day in range(365)
        ]
        history_file = constants_mod.HISTORY_DIR / f"{product.asin}.json"
        history_file.write_text(json.dumps({"marketplaces": {"us": seeded}}))
        record_prices([product], datetime.date(2026, 1, 1))

        capped = load_price_history(product.asin)
        assert len(capped) == 365
        assert capped[0]["date"] == "2025-01-02"
        assert capped[-1]["date"] == "2026-01-01"


class TestNotifyRecordsPrices:
    """notify command must persist fetched prices to history."""

    def test_notify_records_prices(self, mock_client, tmp_config):
        """notify records prices for fetched items."""
        from audible_deals.price_history import (
            load_price_history as _load_price_history,
        )

        wishlist_mod.save_wishlist(
            [
                {"asin": "NR1", "title": "Deal Book", "max_price": 5.0, "added": ""},
            ]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="NR1", price=3.99, title="Deal Book"),
        ]

        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0, result.output

        history = _load_price_history("NR1")
        assert len(history) == 1
        assert history[0]["price"] == 3.99


class TestWebhookFormats:
    """Tests for format_webhook_payload function."""

    def _hits(self):
        return [
            {
                "asin": "B001",
                "title": "My Book",
                "price": 3.99,
                "target": 5.0,
                "url": "https://example.com/pd/B001",
            }
        ]

    def test_generic_format(self):
        from audible_deals.webhooks import format_webhook_payload

        body, headers = format_webhook_payload(self._hits(), "generic")
        assert headers["Content-Type"] == "application/json"
        data = json.loads(body)
        assert "deals" in data
        assert data["count"] == 1

    def test_slack_format_has_text_key(self):
        from audible_deals.webhooks import format_webhook_payload

        body, headers = format_webhook_payload(self._hits(), "slack")
        assert headers["Content-Type"] == "application/json"
        data = json.loads(body)
        assert "text" in data
        assert "My Book" in data["text"]

    def test_discord_format_has_content_key(self):
        from audible_deals.webhooks import format_webhook_payload

        body, headers = format_webhook_payload(self._hits(), "discord")
        assert headers["Content-Type"] == "application/json"
        data = json.loads(body)
        assert "content" in data
        assert "My Book" in data["content"]

    def test_teams_format_has_messagecardtype(self):
        from audible_deals.webhooks import format_webhook_payload

        body, headers = format_webhook_payload(self._hits(), "teams")
        assert headers["Content-Type"] == "application/json"
        data = json.loads(body)
        assert data["@type"] == "MessageCard"
        assert "My Book" in str(data)

    def test_ntfy_format_is_plaintext(self):
        from audible_deals.webhooks import format_webhook_payload

        body, headers = format_webhook_payload(self._hits(), "ntfy")
        assert "text/plain" in headers["Content-Type"]
        assert "My Book" in body.decode("utf-8")
        assert headers["Tags"] == "book"

    def test_unknown_format_raises(self):
        from audible_deals.webhooks import format_webhook_payload

        with pytest.raises(ValueError, match="Unknown webhook format"):
            format_webhook_payload(self._hits(), "discord_v2")

    def test_notify_slack_posts_correct_headers(
        self, mock_client, tmp_config, monkeypatch
    ):
        """notify --webhook-format slack sends Content-Type: application/json with 'text' key."""
        import socket

        monkeypatch.setattr(
            "audible_deals.validation.socket.getaddrinfo",
            lambda host, port: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
        )

        wishlist_mod.save_wishlist(
            [
                {"asin": "WH1", "title": "Deal Book", "max_price": 5.0, "added": ""},
            ]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="WH1", price=3.99, title="Deal Book"),
        ]

        captured_requests = []

        def fake_post(self, url, body, headers):
            captured_requests.append((url, body, headers))

        monkeypatch.setattr(
            "audible_deals.webhook_client.WebhookClient.post", fake_post
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "notify",
                "--webhook",
                "https://example.com/slack",
                "--webhook-format",
                "slack",
            ],
        )
        assert result.exit_code == 0, result.output
        assert len(captured_requests) == 1
        _url, body, headers = captured_requests[0]
        assert headers["Content-Type"] == "application/json"
        body = json.loads(body)
        assert "text" in body

    def test_template_format_renders_hits(self):
        from audible_deals.webhooks import format_webhook_payload

        hits = [
            {
                "asin": "B001",
                "title": "My Book",
                "price": 3.99,
                "target": 5.0,
                "url": "https://ex.com",
                "currency": "$",
                "discount_pct": 20.0,
            }
        ]
        tmpl = "{title} is ${price:.2f}"
        body, headers = format_webhook_payload(hits, "generic", template=tmpl)
        assert headers["Content-Type"] == "text/plain; charset=utf-8"
        assert body.decode("utf-8") == "My Book is $3.99"

    def test_template_multiple_hits_joined_with_newline(self):
        from audible_deals.webhooks import format_webhook_payload

        hits = [
            {
                "asin": "B001",
                "title": "Book A",
                "price": 3.99,
                "target": 5.0,
                "url": "u1",
                "currency": "$",
                "discount_pct": 0.0,
            },
            {
                "asin": "B002",
                "title": "Book B",
                "price": 2.99,
                "target": 5.0,
                "url": "u2",
                "currency": "$",
                "discount_pct": 0.0,
            },
        ]
        body, _ = format_webhook_payload(hits, "generic", template="{title}")
        assert body.decode("utf-8") == "Book A\nBook B"

    def test_template_unknown_key_raises_valueerror(self):
        from audible_deals.webhooks import format_webhook_payload

        hits = [
            {
                "asin": "B001",
                "title": "X",
                "price": 1.0,
                "target": 2.0,
                "url": "u",
                "currency": "$",
                "discount_pct": 0.0,
            }
        ]
        with pytest.raises(ValueError, match="unknown key"):
            format_webhook_payload(hits, "generic", template="{nonexistent_field}")

    def test_template_and_format_mutually_exclusive(
        self, mock_client, tmp_config, monkeypatch, tmp_path
    ):
        """--webhook-template and --webhook-format are mutually exclusive."""
        import socket

        monkeypatch.setattr(
            "audible_deals.validation.socket.getaddrinfo",
            lambda host, port: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
        )
        tmpl_file = tmp_path / "tmpl.txt"
        tmpl_file.write_text("{title}")
        wishlist_mod.save_wishlist(
            [{"asin": "B001", "title": "X", "max_price": 5.0, "added": ""}]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="B001", price=3.99)
        ]
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "notify",
                "--webhook",
                "https://example.com/hook",
                "--webhook-format",
                "slack",
                "--webhook-template",
                str(tmpl_file),
            ],
        )
        assert result.exit_code != 0
        assert (
            "mutually exclusive" in result.output.lower()
            or "mutually exclusive" in str(result.exception).lower()
        )

    def test_template_requires_webhook(self, mock_client, tmp_config, tmp_path):

        tmpl_file = tmp_path / "tmpl.txt"
        tmpl_file.write_text("{title}")
        wishlist_mod.save_wishlist(
            [{"asin": "B001", "title": "X", "max_price": 5.0, "added": ""}]
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["notify", "--webhook-template", str(tmpl_file)])
        assert result.exit_code != 0
        assert (
            "requires --webhook" in result.output.lower()
            or "requires --webhook" in str(result.exception).lower()
        )

    def test_template_lib_rejects_non_generic_fmt(self):
        from audible_deals.webhooks import format_webhook_payload

        with pytest.raises(ValueError, match="non-generic"):
            format_webhook_payload([], "slack", template="{title}")

    def test_template_malformed_brace_raises_valueerror(self):
        from audible_deals.webhooks import format_webhook_payload

        hits = [{"asin": "B001", "title": "T", "price": 1.0, "target": 2.0, "url": "u"}]
        with pytest.raises(ValueError, match="Valid keys"):
            format_webhook_payload(hits, "generic", template="abc {price:zzz}")


class TestNotifyHitsSchema:
    def test_stdout_json_excludes_currency_and_discount(self, mock_client, tmp_config):

        wishlist_mod.save_wishlist(
            [{"asin": "B001", "title": "X", "max_price": 5.0, "added": ""}]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="B001", price=3.99, list_price=9.99)
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["count"] == 1
        hit = payload["deals"][0]
        assert set(hit.keys()) == {"asin", "title", "price", "target", "url"}


class TestRunLock:
    def test_acquire_and_release(self, tmp_path):
        """Lock is acquired and its durable lock file remains after exit."""
        import audible_deals.constants as constants_mod

        lock_file = tmp_path / ".deals.lock"
        constants_mod.LOCK_FILE = lock_file
        from audible_deals.locking import run_lock

        with run_lock():
            assert lock_file.exists()
        assert lock_file.exists()

    def test_pid_written_to_lock(self, tmp_path):
        """The lock file records the current PID for diagnostics."""
        import os

        import audible_deals.constants as constants_mod

        lock_file = tmp_path / ".deals.lock"
        constants_mod.LOCK_FILE = lock_file
        from audible_deals.locking import run_lock

        with run_lock():
            assert lock_file.read_text() == f"advisory:{os.getpid()}"

    def test_contention_raises_lock_held_error(self, tmp_path):
        """A held (fresh) lock raises LockHeldError."""
        import audible_deals.constants as constants_mod
        from audible_deals.locking import LockHeldError, run_lock

        lock_file = tmp_path / ".deals.lock"
        constants_mod.LOCK_FILE = lock_file
        with run_lock():
            with pytest.raises(LockHeldError):
                with run_lock():
                    pass

    def test_old_unlocked_lock_file_is_acquired(self, tmp_path):
        """An old lock-file timestamp does not affect advisory lock acquisition."""
        import audible_deals.constants as constants_mod
        from audible_deals.locking import run_lock

        lock_file = tmp_path / ".deals.lock"
        constants_mod.LOCK_FILE = lock_file
        lock_file.write_text("99999")
        old_mtime = time.time() - 700  # 700s > 600s stale threshold
        os.utime(str(lock_file), (old_mtime, old_mtime))
        with run_lock():
            assert lock_file.exists()
        assert lock_file.exists()

    def test_active_lock_is_not_broken_when_older_than_ten_minutes(self, tmp_path):
        import audible_deals.constants as constants_mod
        from audible_deals.locking import LockHeldError, run_lock

        lock_file = tmp_path / ".deals.lock"
        constants_mod.LOCK_FILE = lock_file
        with run_lock():
            old_mtime = time.time() - 700
            os.utime(str(lock_file), (old_mtime, old_mtime))
            with pytest.raises(LockHeldError):
                with run_lock():
                    pass

    def test_legacy_lock_probe_handles_disappearing_file(self, tmp_path, monkeypatch):
        import audible_deals.constants as constants_mod
        import audible_deals.locking as locking_mod

        lock_file = tmp_path / ".deals.lock"
        lock_file.write_text("99999")
        constants_mod.LOCK_FILE = lock_file
        real_open = locking_mod.os.open
        removed = False

        def _disappearing_open(path, flags, mode=0o777):
            nonlocal removed
            fd = real_open(path, flags, mode)
            if path == str(lock_file) and not flags & os.O_EXCL and not removed:
                lock_file.unlink()
                removed = True
            return fd

        monkeypatch.setattr(locking_mod.os, "open", _disappearing_open)

        with locking_mod.run_lock():
            assert lock_file.exists()

    def test_live_legacy_owner_remains_protected(self, tmp_path):
        import audible_deals.constants as constants_mod
        from audible_deals.locking import LockHeldError, run_lock

        lock_file = tmp_path / ".deals.lock"
        lock_file.write_text(str(os.getpid()))
        constants_mod.LOCK_FILE = lock_file

        with pytest.raises(LockHeldError, match="legacy"):
            with run_lock():
                pass

    def test_legacy_creator_winning_create_race_is_not_overwritten(
        self, tmp_path, monkeypatch
    ):
        import audible_deals.constants as constants_mod
        import audible_deals.locking as locking_mod
        from audible_deals.locking import LockHeldError

        lock_file = tmp_path / ".deals.lock"
        constants_mod.LOCK_FILE = lock_file
        real_open = locking_mod.os.open

        def _legacy_wins(path, flags, mode=0o777):
            if path == str(lock_file) and flags & os.O_EXCL and not lock_file.exists():
                lock_file.write_text(str(os.getpid()))
            return real_open(path, flags, mode)

        monkeypatch.setattr(locking_mod.os, "open", _legacy_wins)

        with pytest.raises(LockHeldError, match="legacy"):
            with locking_mod.run_lock():
                pass
        assert lock_file.read_text() == str(os.getpid())

    def test_subprocess_contention_releases_after_crash(self, tmp_path):
        import audible_deals.constants as constants_mod
        from audible_deals.locking import LockHeldError, run_lock

        lock_file = tmp_path / ".deals.lock"
        constants_mod.LOCK_FILE = lock_file
        script = """
import sys
import time
from pathlib import Path
import audible_deals.constants as constants
from audible_deals.locking import run_lock

constants.LOCK_FILE = Path(sys.argv[1])
with run_lock():
    print("locked", flush=True)
    time.sleep(60)
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(lock_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout is not None
            assert proc.stdout.readline().strip() == "locked"
            with pytest.raises(LockHeldError):
                with run_lock():
                    pass
        finally:
            proc.kill()
            proc.wait(timeout=2)
        with run_lock():
            pass

    def test_lock_released_on_exception(self, tmp_path):
        """Lock is released even when the body raises."""
        import audible_deals.constants as constants_mod
        from audible_deals.locking import run_lock

        lock_file = tmp_path / ".deals.lock"
        constants_mod.LOCK_FILE = lock_file
        with pytest.raises(ValueError):
            with run_lock():
                raise ValueError("boom")
        with run_lock():
            pass

    def test_release_does_not_remove_foreign_pid_lock(self, tmp_path):
        """Lock-file diagnostics do not change ownership of the OS lock."""
        import audible_deals.constants as constants_mod
        from audible_deals.locking import run_lock

        lock_file = tmp_path / ".deals.lock"
        constants_mod.LOCK_FILE = lock_file
        with run_lock():
            # Simulate another process overwriting the lock with its own PID
            lock_file.write_text("99999")
        # The file is retained and its diagnostic PID is not ownership.
        assert lock_file.exists()
        assert lock_file.read_text() == "99999"


class TestNotifyLockCLI:
    def test_held_lock_exits_zero_with_message(
        self, tmp_config, mock_client, monkeypatch
    ):
        """When lock is held, notify exits 0 and prints the in-progress message."""
        runner = CliRunner()
        from audible_deals.locking import run_lock

        with run_lock():
            result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0
        assert "in progress" in result.output

    def test_held_lock_notify_no_webhook_emits_empty_json(
        self, tmp_config, mock_client
    ):
        """notify lock-held without --webhook emits empty JSON to stdout."""
        runner = CliRunner()
        from audible_deals.locking import run_lock

        with run_lock():
            result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0
        # CliRunner mixes stderr into output; find the JSON portion
        json_start = result.output.index("{")
        parsed = json.loads(result.output[json_start:])
        assert parsed == {"deals": [], "count": 0}

    def test_held_lock_recap_exits_zero_with_message(
        self, tmp_config, mock_client, monkeypatch
    ):
        """When lock is held, recap exits 0 and prints the in-progress message."""
        runner = CliRunner()
        from audible_deals.locking import run_lock

        with run_lock():
            result = runner.invoke(cli, ["recap"])
        assert result.exit_code == 0
        assert "in progress" in result.output

    def test_held_lock_with_exit_code_exits_one(self, tmp_config, mock_client):
        """notify --exit-code under a held lock must not signal 'deals found'."""
        runner = CliRunner()
        from audible_deals.locking import run_lock

        with run_lock():
            result = runner.invoke(cli, ["notify", "--exit-code"])
        assert result.exit_code == 1

    def test_held_lock_recap_json_emits_empty_json(self, tmp_config, mock_client):
        """recap --json lock-held emits empty JSON with the recap shape."""
        runner = CliRunner()
        from audible_deals.locking import run_lock

        with run_lock():
            result = runner.invoke(cli, ["recap", "--json"])
        assert result.exit_code == 0
        # CliRunner mixes stderr into output; find the JSON portion
        json_start = result.output.index("{")
        parsed = json.loads(result.output[json_start:])
        assert parsed["days"] == 7  # default
        assert parsed["drops"] == []
        assert parsed["new_count"] == 0
        assert parsed["wishlist_hits"] == []


class TestNotifyCooldown:
    def _setup_wishlist_and_product(self, mock_client, asin, price, target):
        wishlist_mod.save_wishlist(
            [{"asin": asin, "title": "Test Book", "max_price": target, "added": ""}]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin=asin, price=price, title="Test Book"),
        ]

    def test_same_price_suppressed_within_cooldown(self, mock_client, tmp_config):
        """Second run at same price within cooldown window is suppressed."""

        self._setup_wishlist_and_product(mock_client, "CD01", 3.99, 5.0)
        today = _datetime.date.today().isoformat()
        constants_mod.NOTIFY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        constants_mod.NOTIFY_STATE_FILE.write_text(
            json.dumps({"CD01": {"price": 3.99, "date": today}})
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["notify", "--cooldown", "3"])
        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert parsed == {"deals": [], "count": 0}

    def test_further_price_drop_renotifies(self, mock_client, tmp_config):
        """Further price drop re-notifies even within cooldown window."""

        self._setup_wishlist_and_product(mock_client, "CD02", 2.99, 5.0)
        today = _datetime.date.today().isoformat()
        constants_mod.NOTIFY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        constants_mod.NOTIFY_STATE_FILE.write_text(
            json.dumps({"CD02": {"price": 3.99, "date": today}})
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["notify", "--cooldown", "7"])
        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert parsed["count"] == 1
        assert parsed["deals"][0]["asin"] == "CD02"

    def test_expired_cooldown_renotifies(self, mock_client, tmp_config):
        """Cooldown expired: re-notifies even at same price."""
        import datetime

        self._setup_wishlist_and_product(mock_client, "CD03", 3.99, 5.0)
        old_date = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
        constants_mod.NOTIFY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        constants_mod.NOTIFY_STATE_FILE.write_text(
            json.dumps({"CD03": {"price": 3.99, "date": old_date}})
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["notify", "--cooldown", "7"])
        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert parsed["count"] == 1

    def test_no_cooldown_flag_no_state_written(self, mock_client, tmp_config):
        """Without --cooldown, notify_state.json is never written."""

        self._setup_wishlist_and_product(mock_client, "CD04", 3.99, 5.0)
        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0, result.output
        assert not constants_mod.NOTIFY_STATE_FILE.exists()

    def test_all_suppressed_exit_code_exits_0(self, mock_client, tmp_config):
        """Items hit target but all suppressed by cooldown + --exit-code → exit 0.

        --exit-code documents "exit 0 if any items hit target"; cooldown only
        suppresses the notification, not the fact that an item was at target.
        """

        self._setup_wishlist_and_product(mock_client, "CD05", 3.99, 5.0)
        today = _datetime.date.today().isoformat()
        constants_mod.NOTIFY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        constants_mod.NOTIFY_STATE_FILE.write_text(
            json.dumps({"CD05": {"price": 3.99, "date": today}})
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["notify", "--cooldown", "3", "--exit-code"])
        assert result.exit_code == 0, result.output

    def test_state_updated_after_send(self, mock_client, tmp_config):
        """Notify state is updated with new price and date after a successful send."""
        import datetime

        self._setup_wishlist_and_product(mock_client, "CD06", 3.99, 5.0)
        runner = CliRunner()
        result = runner.invoke(cli, ["notify", "--cooldown", "7"])
        assert result.exit_code == 0, result.output
        assert constants_mod.NOTIFY_STATE_FILE.exists()
        state = json.loads(constants_mod.NOTIFY_STATE_FILE.read_text())
        assert "CD06" in state
        assert state["CD06"]["price"] == pytest.approx(3.99)
        assert state["CD06"]["date"] == datetime.date.today().isoformat()

    def test_pruned_asin_not_on_wishlist(self, mock_client, tmp_config):
        """State entries for ASINs no longer on wishlist are pruned on save."""

        self._setup_wishlist_and_product(mock_client, "CD07", 3.99, 5.0)
        today = _datetime.date.today().isoformat()
        constants_mod.NOTIFY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        constants_mod.NOTIFY_STATE_FILE.write_text(
            json.dumps({"OLD1": {"price": 1.0, "date": today}})
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["notify", "--cooldown", "7"])
        assert result.exit_code == 0, result.output
        state = json.loads(constants_mod.NOTIFY_STATE_FILE.read_text())
        assert "OLD1" not in state
        assert "CD07" in state


class TestRecapJson:
    def _write_history(self, tmp_config, asin, entries):

        hist_dir = constants_mod.HISTORY_DIR
        hist_dir.mkdir(parents=True, exist_ok=True)
        (hist_dir / f"{asin}.json").write_text(
            json.dumps({"marketplaces": {"us": entries}})
        )

    def test_recap_json_emits_parseable_json(self, tmp_config):
        """recap --json emits valid JSON with required keys."""
        import datetime

        today = datetime.date.today().isoformat()
        old_date = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        self._write_history(
            tmp_config,
            "RJ01",
            [
                {"date": old_date, "price": 10.00, "title": "Recap Book"},
                {"date": today, "price": 5.00, "title": "Recap Book"},
            ],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "7", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "days" in payload
        assert "drops" in payload
        assert "new_count" in payload
        assert "wishlist_hits" in payload

    def test_recap_json_and_webhook_mutually_exclusive(self, tmp_config):
        """recap --json --webhook is a usage error."""
        runner = CliRunner()
        result = runner.invoke(
            cli, ["recap", "--json", "--webhook", "https://example.com/hook"]
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_recap_json_drops_have_drop_pct(self, tmp_config):
        """recap --json drop entries include drop_pct correctly computed."""
        import datetime

        today = datetime.date.today().isoformat()
        old_date = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        self._write_history(
            tmp_config,
            "RJ02",
            [
                {"date": old_date, "price": 10.00, "title": "Pct Book"},
                {"date": today, "price": 5.00, "title": "Pct Book"},
            ],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "7", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert len(payload["drops"]) >= 1
        drop = next(d for d in payload["drops"] if d["asin"] == "RJ02")
        assert drop["drop_pct"] == 50
        assert drop["old_price"] == pytest.approx(10.00)
        assert drop["new_price"] == pytest.approx(5.00)

    def test_recap_json_sorted_biggest_drop_first(self, tmp_config):
        """recap --json drops sorted biggest absolute drop first."""
        import datetime

        today = datetime.date.today().isoformat()
        old_date = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        self._write_history(
            tmp_config,
            "RJS1",
            [
                {"date": old_date, "price": 20.00, "title": "Big Drop"},
                {"date": today, "price": 5.00, "title": "Big Drop"},
            ],
        )
        self._write_history(
            tmp_config,
            "RJS2",
            [
                {"date": old_date, "price": 8.00, "title": "Small Drop"},
                {"date": today, "price": 6.00, "title": "Small Drop"},
            ],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "7", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        drops = payload["drops"]
        assert len(drops) >= 2
        big = next(d for d in drops if d["asin"] == "RJS1")
        small = next(d for d in drops if d["asin"] == "RJS2")
        assert drops.index(big) < drops.index(small)

    def test_recap_json_no_history_emits_empty_payload(self, tmp_config):
        """recap --json with no price history dir emits parseable JSON with empty drops."""
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "7", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["days"] == 7
        assert payload["drops"] == []
        assert payload["new_count"] == 0
        assert payload["wishlist_hits"] == []
        assert "atl_hits" not in payload

    def test_recap_json_no_history_with_atl_includes_atl_hits(self, tmp_config):
        """recap --json --atl with no price history emits empty payload including atl_hits."""
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--days", "7", "--json", "--atl"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["drops"] == []
        assert payload["atl_hits"] == []

    def test_recap_atl_all_json_includes_atl_hits(self, tmp_config):
        """recap --atl-all --json surfaces ATL hits across all tracked ASINs."""
        import datetime

        today = datetime.date.today().isoformat()
        old_date = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        # Write two history entries so the ATL condition can be evaluated
        self._write_history(
            tmp_config,
            "RALL01",
            [
                {"date": old_date, "price": 9.99, "title": "All Book"},
                {"date": today, "price": 5.99, "title": "All Book"},
            ],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--json", "--atl-all"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "atl_hits" in payload
        asins = [h["asin"] for h in payload["atl_hits"]]
        assert "RALL01" in asins

    def test_recap_atl_all_no_history_includes_empty_atl_hits(self, tmp_config):
        """recap --atl-all --json with no history still includes atl_hits key."""
        runner = CliRunner()
        result = runner.invoke(cli, ["recap", "--json", "--atl-all"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "atl_hits" in payload
        assert payload["atl_hits"] == []


class TestRecapWebhook:
    def _write_history(self, tmp_config, asin, entries):

        hist_dir = constants_mod.HISTORY_DIR
        hist_dir.mkdir(parents=True, exist_ok=True)
        (hist_dir / f"{asin}.json").write_text(
            json.dumps({"marketplaces": {"us": entries}})
        )

    def test_recap_webhook_posts(self, tmp_config, monkeypatch):
        """recap --webhook POSTs to the webhook URL."""
        import datetime
        import socket

        monkeypatch.setattr(
            "audible_deals.validation.socket.getaddrinfo",
            lambda host, port: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
        )
        today = datetime.date.today().isoformat()
        old_date = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        self._write_history(
            tmp_config,
            "RW01",
            [
                {"date": old_date, "price": 12.00, "title": "Webhook Book"},
                {"date": today, "price": 4.00, "title": "Webhook Book"},
            ],
        )
        captured = []

        def fake_post(self, url, body, headers):
            captured.append((url, body, headers))

        monkeypatch.setattr(
            "audible_deals.webhook_client.WebhookClient.post", fake_post
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["recap", "--webhook", "https://example.com/hook"],
        )
        assert result.exit_code == 0, result.output
        assert len(captured) == 1
        assert "Sent recap to webhook" in result.output

    def test_recap_empty_webhook_skips_post(self, tmp_config, monkeypatch):
        """recap --webhook skips POST when there's nothing to report."""
        import socket

        monkeypatch.setattr(
            "audible_deals.validation.socket.getaddrinfo",
            lambda host, port: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
        )
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["recap", "--webhook", "https://example.com/hook"],
        )
        assert len(captured) == 0
        assert "Nothing to send" in result.output or result.exit_code == 0


class TestFormatRecapPayload:
    def _payload(self, days=7, drops=None, wishlist_hits=None):
        return {
            "days": days,
            "drops": drops
            or [
                {
                    "asin": "R001",
                    "title": "My Recap Book",
                    "old_price": 10.0,
                    "new_price": 5.0,
                    "drop_pct": 50,
                }
            ],
            "new_count": 0,
            "wishlist_hits": wishlist_hits or [],
        }

    def test_generic_shape(self):
        from audible_deals.webhooks import format_recap_payload

        body, headers = format_recap_payload(self._payload(), "generic")
        assert headers["Content-Type"] == "application/json"
        data = json.loads(body)
        assert "days" in data
        assert "drops" in data

    def test_slack_has_text_key(self):
        from audible_deals.webhooks import format_recap_payload

        body, headers = format_recap_payload(self._payload(), "slack")
        assert headers["Content-Type"] == "application/json"
        data = json.loads(body)
        assert "text" in data
        assert "My Recap Book" in data["text"]

    def test_discord_has_content_key(self):
        from audible_deals.webhooks import format_recap_payload

        body, headers = format_recap_payload(self._payload(), "discord")
        assert headers["Content-Type"] == "application/json"
        data = json.loads(body)
        assert "content" in data
        assert "My Recap Book" in data["content"]

    def test_teams_has_messagecardtype(self):
        from audible_deals.webhooks import format_recap_payload

        body, headers = format_recap_payload(self._payload(), "teams")
        assert headers["Content-Type"] == "application/json"
        data = json.loads(body)
        assert data["@type"] == "MessageCard"
        assert "My Recap Book" in str(data)

    def test_ntfy_is_plaintext(self):
        from audible_deals.webhooks import format_recap_payload

        body, headers = format_recap_payload(self._payload(), "ntfy")
        assert "text/plain" in headers["Content-Type"]
        assert "My Recap Book" in body.decode("utf-8")
        assert headers["Tags"] == "book"

    def test_unknown_format_raises(self):
        from audible_deals.webhooks import format_recap_payload

        with pytest.raises(ValueError, match="Unknown webhook format"):
            format_recap_payload(self._payload(), "unknown_fmt")

    def test_title_includes_days(self):
        from audible_deals.webhooks import format_recap_payload

        body, _ = format_recap_payload(self._payload(days=14), "slack")
        text = json.loads(body)["text"]
        assert "14" in text


class TestWatchSkipsAuthorEntries:
    def test_watch_with_only_author_entries_prints_hint(self, tmp_config, mock_client):
        """watch with only author entries prints a hint and returns 0 hits."""

        wishlist_mod.save_wishlist(
            [
                {
                    "type": "author",
                    "author": "Brandon Sanderson",
                    "max_price": 5.0,
                    "added": "2024-01-01",
                }
            ]
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["watch"])
        assert result.exit_code == 0, result.output
        assert "notify" in result.output.lower()
        mock_client.get_products_batch.assert_not_called()

    def test_watch_mixed_skips_author_fetches_asin(self, tmp_config, mock_client):
        """watch with mixed entries fetches only ASIN items."""

        wishlist_mod.save_wishlist(
            [
                {
                    "asin": "B00ASIN001",
                    "title": "Normal Book",
                    "max_price": 10.0,
                    "added": "",
                },
                {
                    "type": "author",
                    "author": "Brandon Sanderson",
                    "max_price": 5.0,
                    "added": "2024-01-01",
                },
            ]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="B00ASIN001", price=8.0, title="Normal Book"),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["watch"])
        assert result.exit_code == 0, result.output
        # Should only request the ASIN, not author entry
        called_asins = mock_client.get_products_batch.call_args[0][0]
        assert "B00ASIN001" in called_asins
        assert len(called_asins) == 1


class TestNotifyAuthorHits:
    def _save_author_wishlist(self, author, max_price):
        wishlist_mod.save_wishlist(
            [
                {
                    "type": "author",
                    "author": author,
                    "max_price": max_price,
                    "added": "2024-01-01",
                }
            ]
        )

    def test_notify_author_hit_appears_in_json_output(self, tmp_config, mock_client):
        """notify with an author watch fires when search returns a matching title."""

        self._save_author_wishlist("Brandon Sanderson", 5.0)
        author_product = make_product(
            asin="B00AUTH001",
            title="Mistborn",
            authors=["Brandon Sanderson"],
            price=3.99,
            length_minutes=600,
        )
        mock_client.get_products_batch.return_value = []
        mock_client.search_pages.return_value = iter([([author_product], 1, 1)])
        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["count"] == 1
        assert payload["deals"][0]["asin"] == "B00AUTH001"
        assert payload["deals"][0]["target"] == 5.0

    def test_notify_author_hit_deduped_against_asin_hits(self, tmp_config, mock_client):
        """Author hit whose ASIN is already an ASIN-entry hit is not duplicated."""

        wishlist_mod.save_wishlist(
            [
                {
                    "asin": "B00AUTH001",
                    "title": "Mistborn",
                    "max_price": 5.0,
                    "added": "",
                },
                {
                    "type": "author",
                    "author": "Brandon Sanderson",
                    "max_price": 5.0,
                    "added": "2024-01-01",
                },
            ]
        )
        shared_product = make_product(
            asin="B00AUTH001",
            title="Mistborn",
            authors=["Brandon Sanderson"],
            price=3.99,
            length_minutes=600,
        )
        mock_client.get_products_batch.return_value = [shared_product]
        mock_client.search_pages.return_value = iter([([shared_product], 1, 1)])
        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        asins = [d["asin"] for d in payload["deals"]]
        assert asins.count("B00AUTH001") == 1

    def test_notify_author_above_price_not_hit(self, tmp_config, mock_client):
        """Author search result above max_price is not a hit."""

        self._save_author_wishlist("Brandon Sanderson", 2.0)
        author_product = make_product(
            asin="B00AUTH002",
            title="Way of Kings",
            authors=["Brandon Sanderson"],
            price=9.99,
            length_minutes=600,
        )
        mock_client.get_products_batch.return_value = []
        mock_client.search_pages.return_value = iter([([author_product], 1, 1)])
        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["count"] == 0

    def test_notify_author_records_prices(self, tmp_config, mock_client):
        """notify records price history for author-search results."""
        from audible_deals.price_history import (
            load_price_history as _load_price_history,
        )

        self._save_author_wishlist("Brandon Sanderson", 5.0)
        author_product = make_product(
            asin="B00AUTH003",
            title="Elantris",
            authors=["Brandon Sanderson"],
            price=3.99,
            length_minutes=600,
        )
        mock_client.get_products_batch.return_value = []
        mock_client.search_pages.return_value = iter([([author_product], 1, 1)])
        runner = CliRunner()
        runner.invoke(cli, ["notify"])
        history = _load_price_history("B00AUTH003")
        assert len(history) == 1
        assert history[0]["price"] == 3.99

    def test_author_hit_json_contains_author_field(self, tmp_config, mock_client):
        """Author-watch hit dicts must include an 'author' key with the watch name."""

        self._save_author_wishlist("Brandon Sanderson", 5.0)
        author_product = make_product(
            asin="B00AUTH010",
            title="The Final Empire",
            authors=["Brandon Sanderson"],
            price=3.99,
            length_minutes=600,
        )
        mock_client.get_products_batch.return_value = []
        mock_client.search_pages.return_value = iter([([author_product], 1, 1)])
        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["count"] == 1
        deal = payload["deals"][0]
        assert deal["author"] == "Brandon Sanderson"

    def test_asin_hit_json_has_no_author_field(self, tmp_config, mock_client):
        """ASIN-watch hit dicts must not include an 'author' key."""

        wishlist_mod.save_wishlist(
            [
                {
                    "asin": "B00AUTH011",
                    "title": "Elantris",
                    "max_price": 5.0,
                    "added": "2024-01-01",
                }
            ]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="B00AUTH011", price=3.99)
        ]
        mock_client.search_pages.return_value = iter([])
        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["count"] == 1
        deal = payload["deals"][0]
        assert "author" not in deal


class TestNotifyAuthorCooldown:
    def _save_author_wishlist(self, author, max_price):
        wishlist_mod.save_wishlist(
            [
                {
                    "type": "author",
                    "author": author,
                    "max_price": max_price,
                    "added": "2024-01-01",
                }
            ]
        )

    def test_author_hit_cooldown_state_persisted(self, tmp_config, mock_client):
        """After an author hit fires, its ASIN is in notify_state."""

        self._save_author_wishlist("Brandon Sanderson", 5.0)
        author_product = make_product(
            asin="B00AUTH010",
            title="Warbreaker",
            authors=["Brandon Sanderson"],
            price=3.99,
            length_minutes=600,
        )
        mock_client.get_products_batch.return_value = []
        mock_client.search_pages.return_value = iter([([author_product], 1, 1)])
        runner = CliRunner()
        result = runner.invoke(cli, ["notify", "--cooldown", "3"])
        assert result.exit_code == 0, result.output
        state = json.loads(constants_mod.NOTIFY_STATE_FILE.read_text())
        assert "B00AUTH010" in state

    def test_author_hit_cooldown_suppressed_second_run(self, tmp_config, mock_client):
        """Author hit suppressed within cooldown window on second run."""

        self._save_author_wishlist("Brandon Sanderson", 5.0)
        today = _datetime.date.today().isoformat()
        constants_mod.NOTIFY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        constants_mod.NOTIFY_STATE_FILE.write_text(
            json.dumps({"B00AUTH011": {"price": 3.99, "date": today}})
        )
        author_product = make_product(
            asin="B00AUTH011",
            title="Rhythm of War",
            authors=["Brandon Sanderson"],
            price=3.99,
            length_minutes=600,
        )
        mock_client.get_products_batch.return_value = []
        mock_client.search_pages.return_value = iter([([author_product], 1, 1)])
        runner = CliRunner()
        result = runner.invoke(cli, ["notify", "--cooldown", "7"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["count"] == 0

    def test_author_hit_cooldown_state_not_pruned(self, tmp_config, mock_client):
        """Author-hit ASIN in notify_state is not pruned after save (cooldown fix)."""

        self._save_author_wishlist("Brandon Sanderson", 5.0)
        author_product = make_product(
            asin="B00AUTH012",
            title="The Final Empire",
            authors=["Brandon Sanderson"],
            price=3.99,
            length_minutes=600,
        )
        mock_client.get_products_batch.return_value = []
        mock_client.search_pages.return_value = iter([([author_product], 1, 1)])
        runner = CliRunner()
        result = runner.invoke(cli, ["notify", "--cooldown", "7"])
        assert result.exit_code == 0, result.output
        state = json.loads(constants_mod.NOTIFY_STATE_FILE.read_text())
        # B00AUTH012 is not a wishlist ASIN, but it should be retained (not pruned)
        assert "B00AUTH012" in state

    def test_suppressed_author_asin_survives_prune_when_asin_hit_fires(
        self, tmp_config, mock_client
    ):
        """Regression: suppressed author ASIN state must survive when another ASIN hit fires.

        Day 1: author ASIN B00AUTH020 fires and is recorded in notify_state.
        Day 2: B00AUTH020 is suppressed by cooldown; ASIN-entry B00ASIN001 fires.
        After day-2 save, B00AUTH020 state entry must still be present (not pruned).
        """

        # Wishlist: one ASIN-entry + one author watch
        wishlist_mod.save_wishlist(
            [
                {
                    "asin": "B00ASIN001",
                    "title": "ASIN Book",
                    "max_price": 10.0,
                    "added": "",
                },
                {
                    "type": "author",
                    "author": "Brandon Sanderson",
                    "max_price": 5.0,
                    "added": "2024-01-01",
                },
            ]
        )

        today = _datetime.date.today().isoformat()
        # Pre-populate: day-1 author hit was recorded
        constants_mod.NOTIFY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        constants_mod.NOTIFY_STATE_FILE.write_text(
            json.dumps({"B00AUTH020": {"price": 3.99, "date": today}})
        )

        # Day 2: ASIN hit fires; author hit is suppressed
        asin_product = make_product(asin="B00ASIN001", price=5.0, title="ASIN Book")
        author_product = make_product(
            asin="B00AUTH020",
            title="The Way of Kings",
            authors=["Brandon Sanderson"],
            price=3.99,
            length_minutes=600,
        )
        mock_client.get_products_batch.return_value = [asin_product]
        mock_client.search_pages.return_value = iter([([author_product], 1, 1)])

        runner = CliRunner()
        result = runner.invoke(cli, ["notify", "--cooldown", "7"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        # Only ASIN hit fires on day 2
        assert payload["count"] == 1
        assert payload["deals"][0]["asin"] == "B00ASIN001"

        # Suppressed author ASIN state must survive
        state = json.loads(constants_mod.NOTIFY_STATE_FILE.read_text())
        assert "B00AUTH020" in state, "Suppressed author ASIN was incorrectly pruned"


class TestFormatRecapPayloadAtlHits:
    def _payload_with_atl(self):
        return {
            "days": 7,
            "drops": [
                {
                    "asin": "D001",
                    "title": "Drop Book",
                    "old_price": 10.0,
                    "new_price": 5.0,
                    "drop_pct": 50,
                }
            ],
            "new_count": 0,
            "wishlist_hits": [],
            "atl_hits": [
                {"asin": "A001", "title": "ATL Book", "price": 2.99, "target": None}
            ],
        }

    def _payload_atl_only(self):
        return {
            "days": 7,
            "drops": [],
            "new_count": 0,
            "wishlist_hits": [],
            "atl_hits": [
                {
                    "asin": "A002",
                    "title": "ATL Only Book",
                    "price": 1.99,
                    "target": None,
                }
            ],
        }

    def test_slack_includes_atl_hits(self):
        from audible_deals.webhooks import format_recap_payload

        body, _ = format_recap_payload(self._payload_with_atl(), "slack")
        text = json.loads(body)["text"]
        assert "ATL Book" in text
        assert "2.99" in text

    def test_discord_includes_atl_hits(self):
        from audible_deals.webhooks import format_recap_payload

        body, _ = format_recap_payload(self._payload_with_atl(), "discord")
        content = json.loads(body)["content"]
        assert "ATL Book" in content
        assert "2.99" in content

    def test_teams_includes_atl_hits(self):
        from audible_deals.webhooks import format_recap_payload

        body, _ = format_recap_payload(self._payload_with_atl(), "teams")
        data = json.loads(body)
        assert "ATL Book" in str(data)

    def test_ntfy_includes_atl_hits(self):
        from audible_deals.webhooks import format_recap_payload

        body, _ = format_recap_payload(self._payload_with_atl(), "ntfy")
        text = body.decode("utf-8")
        assert "ATL Book" in text
        assert "2.99" in text

    def test_no_atl_hits_key_renders_cleanly(self):
        """Payload without atl_hits key does not crash and renders normally."""
        from audible_deals.webhooks import format_recap_payload

        payload = {
            "days": 7,
            "drops": [
                {
                    "asin": "D1",
                    "title": "Book",
                    "old_price": 10.0,
                    "new_price": 5.0,
                    "drop_pct": 50,
                }
            ],
            "new_count": 0,
            "wishlist_hits": [],
        }
        body, _ = format_recap_payload(payload, "slack")
        text = json.loads(body)["text"]
        assert "Book" in text

    def test_empty_atl_hits_no_section(self):
        """Empty atl_hits list does not add an ATL section."""
        from audible_deals.webhooks import format_recap_payload

        payload = {**self._payload_with_atl(), "atl_hits": []}
        body, _ = format_recap_payload(payload, "slack")
        text = json.loads(body)["text"]
        assert "all-time low" not in text.lower()


class TestDisplayRecapAtlAllLabel:
    def _capture_recap(self, **kwargs):
        from io import StringIO

        from rich.console import Console

        from audible_deals.presentation import terminal as display_mod
        from audible_deals.presentation.reports import display_recap

        buf = StringIO()
        old_console = display_mod.console
        display_mod.console = Console(file=buf, highlight=False, markup=False)
        try:
            display_recap([], [], [], 7, **kwargs)
        finally:
            display_mod.console = old_console
        return buf.getvalue()

    def test_atl_all_false_uses_wishlist_label(self):
        """atl_all=False (default) uses 'Wishlist items at all-time low:'."""
        output = self._capture_recap(atl_hits=[], atl_all=False)
        assert "Wishlist items at all-time low" in output

    def test_atl_all_true_uses_tracked_label(self):
        """atl_all=True uses 'Tracked items at all-time low:'."""
        output = self._capture_recap(atl_hits=[], atl_all=True)
        assert "Tracked items at all-time low" in output

    def test_atl_none_shows_no_atl_section(self):
        """atl_hits=None means no ATL section at all."""
        output = self._capture_recap(atl_hits=None)
        assert "all-time low" not in output.lower()


class TestCreditAdviceInNotify:
    def test_hit_includes_verdict_and_effective_price(self, mock_client, tmp_config):
        config_store_mod.save_config({"credit_price": 11.25})
        wishlist_mod.save_wishlist(
            [{"asin": "B001", "title": "X", "max_price": 5.0, "added": ""}]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="B001", price=3.99, list_price=9.99)
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0, result.output
        hit = json.loads(result.output)["deals"][0]
        assert hit["verdict"] == "cash"
        assert hit["effective_price"] == 3.99

    def test_hit_schema_unchanged_without_config(self, mock_client, tmp_config):
        wishlist_mod.save_wishlist(
            [{"asin": "B002", "title": "Y", "max_price": 5.0, "added": ""}]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="B002", price=3.99)
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["notify"])
        assert result.exit_code == 0, result.output
        hit = json.loads(result.output)["deals"][0]
        assert "verdict" not in hit
        assert "effective_price" not in hit


class TestRoutesWatchCommand:
    def test_watch_empty_wishlist(self, tmp_config, mock_client):
        result = _routes_run(CliRunner(), ["watch"])
        assert result.exit_code == 0
        assert "empty" in result.output.lower()

    def test_watch_with_items(self, tmp_config, mock_client):
        wl = [{"asin": "WA01", "title": "Watch Book", "max_price": 5.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="WA01", title="Watch Book", price=3.99),
        ]
        result = _routes_run(CliRunner(), ["watch"])
        assert result.exit_code == 0
        assert "BUY" in result.output

    def test_watch_buy_only(self, tmp_config, mock_client):
        wl = [
            {"asin": "WA02", "title": "Cheap", "max_price": 5.0, "added": ""},
            {"asin": "WA03", "title": "Expensive", "max_price": 2.0, "added": ""},
        ]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="WA02", title="Cheap", price=3.99),
            make_product(asin="WA03", title="Expensive", price=9.99),
        ]
        result = _routes_run(CliRunner(), ["watch", "--buy-only"])
        assert result.exit_code == 0

    def test_watch_with_sort(self, tmp_config, mock_client):
        wl = [{"asin": "WA04", "title": "Sort Book", "max_price": 10.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="WA04", title="Sort Book", price=5.99),
        ]
        result = _routes_run(CliRunner(), ["watch", "--sort", "title"])
        assert result.exit_code == 0

    def test_watch_show_url(self, tmp_config, mock_client):
        wl = [{"asin": "WA05", "title": "URL Book", "max_price": 10.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="WA05", title="URL Book", price=5.99),
        ]
        result = _routes_run(CliRunner(), ["watch", "--show-url"])
        assert result.exit_code == 0


class TestRoutesRecapCommand:
    def test_recap_no_history(self, tmp_config, mock_client):
        result = _routes_run(CliRunner(), ["recap"])
        assert result.exit_code == 0
        assert "No price history" in result.output

    def test_recap_with_data(self, tmp_config, mock_client):
        hist_dir = tmp_config / "history"
        hist_dir.mkdir()
        import datetime

        today = datetime.date.today().isoformat()
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        entries = [
            {"date": yesterday, "price": 9.99, "title": "Recap Book"},
            {"date": today, "price": 4.99, "title": "Recap Book"},
        ]
        (hist_dir / "R001.json").write_text(
            json.dumps({"marketplaces": {"us": entries}})
        )
        result = _routes_run(CliRunner(), ["recap"])
        assert result.exit_code == 0

    def test_recap_with_days(self, tmp_config, mock_client):
        result = _routes_run(CliRunner(), ["recap", "--days", "30"])
        assert result.exit_code == 0

    def test_recap_show_new(self, tmp_config, mock_client):
        hist_dir = tmp_config / "history"
        hist_dir.mkdir()
        import datetime

        today = datetime.date.today().isoformat()
        entries = [{"date": today, "price": 4.99, "title": "New Item"}]
        (hist_dir / "R002.json").write_text(
            json.dumps({"marketplaces": {"us": entries}})
        )
        result = _routes_run(CliRunner(), ["recap", "--show-new"])
        assert result.exit_code == 0


class TestRoutesNotifyCommand:
    def test_notify_empty_wishlist(self, tmp_config, mock_client):
        result = _routes_run(CliRunner(), ["notify"])
        assert result.exit_code == 0
        assert "empty" in result.output.lower()

    def test_notify_no_hits(self, tmp_config, mock_client):
        wl = [{"asin": "N001", "title": "Notify Book", "max_price": 2.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="N001", price=9.99),
        ]
        result = _routes_run(CliRunner(), ["notify"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 0

    def test_notify_with_hits(self, tmp_config, mock_client):
        wl = [{"asin": "N002", "title": "Deal Book", "max_price": 5.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="N002", price=3.99),
        ]
        result = _routes_run(CliRunner(), ["notify"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 1


class TestRoutesNotifyExitCode:
    def test_no_hits_with_flag_exits_1(self, tmp_config, mock_client):
        wl = [{"asin": "EC01", "title": "Book", "max_price": 2.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="EC01", price=9.99),
        ]
        result = CliRunner().invoke(cli, ["notify", "--exit-code"])
        assert result.exit_code == 1

    def test_with_hit_and_flag_exits_0(self, tmp_config, mock_client):
        wl = [{"asin": "EC02", "title": "Book", "max_price": 5.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="EC02", price=3.99),
        ]
        result = CliRunner().invoke(cli, ["notify", "--exit-code"])
        assert result.exit_code == 0

    def test_no_flag_no_hits_exits_0(self, tmp_config, mock_client):
        wl = [{"asin": "EC03", "title": "Book", "max_price": 2.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="EC03", price=9.99),
        ]
        result = CliRunner().invoke(cli, ["notify"])
        assert result.exit_code == 0


class TestRoutesWatchExitCode:
    def test_no_hits_with_flag_exits_1(self, tmp_config, mock_client):
        wl = [{"asin": "WEC1", "title": "Book", "max_price": 2.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="WEC1", price=9.99),
        ]
        result = CliRunner().invoke(cli, ["watch", "--exit-code"])
        assert result.exit_code == 1

    def test_with_hit_and_flag_exits_0(self, tmp_config, mock_client):
        wl = [{"asin": "WEC2", "title": "Book", "max_price": 5.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_products_batch.return_value = [
            make_product(asin="WEC2", price=3.99),
        ]
        result = CliRunner().invoke(cli, ["watch", "--exit-code"])
        assert result.exit_code == 0

    def test_exit_code_with_every_is_usage_error(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["watch", "--exit-code", "--every", "5m"])
        assert result.exit_code != 0
        assert "exit-code" in result.output.lower() or "every" in result.output.lower()


class TestTrackChecksMalformedHistory:
    def test_run_history_string_does_not_crash(self, tmp_config):
        """run_history that is a string (not a list) must not raise."""
        constants_mod.TRACK_STATE_FILE.write_text(
            json.dumps({"install": {"every": "6h"}, "run_history": "oops"})
        )
        rows = _track_checks()
        assert any(r[0] == "Last tracked run" for r in rows)

    def test_run_history_non_dict_entry_does_not_crash(self, tmp_config):
        """A list containing non-dict entries must not raise AttributeError."""
        constants_mod.TRACK_STATE_FILE.write_text(
            json.dumps({"install": {"every": "6h"}, "run_history": ["x", 5, None]})
        )
        rows = _track_checks()
        # No well-formed run remains, so it reports "never ran".
        last = next(r for r in rows if r[0] == "Last tracked run")
        assert last[1] == "WARN"

    def test_doctor_survives_malformed_run_history(
        self, tmp_config, mock_client, monkeypatch
    ):
        monkeypatch.setattr(constants_mod, "AUTH_FILE", tmp_config / "auth.json")
        monkeypatch.setattr(constants_mod, "CONFIG_DIR", tmp_config)
        constants_mod.TRACK_STATE_FILE.write_text(
            json.dumps({"install": {"every": "6h"}, "run_history": "oops"})
        )
        result = CliRunner().invoke(cli, ["doctor"])
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Traceback" not in result.output


def test_notification_workflow_imports_in_fresh_interpreter():
    result = subprocess.run(
        [sys.executable, "-c", "import audible_deals.notification_workflow"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


class TestWebhookNoRedirect:
    def test_post_webhook_does_not_follow_redirect(self, monkeypatch):
        """A 3xx from the validated host must not be followed: following it
        would reach an unvetted target (SSRF) and resend the auth header."""
        _RedirectServer.followed = False
        server = HTTPServer(("127.0.0.1", 0), _RedirectServer)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            with pytest.raises(WebhookDeliveryError, match="Webhook failed"):
                WebhookClient(sleep=lambda seconds: None).post(
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


class TestNotifyExitCodeWithCooldown:
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

    def test_exit_1_when_wishlist_is_empty(self, mock_client, tmp_config):
        result = CliRunner().invoke(cli, ["notify", "--exit-code"])
        assert result.exit_code == 1, result.output


class TestTrackAuthErrorClassification:
    def test_transient_error_does_not_ping_or_latch(
        self, mock_client, tmp_config, monkeypatch
    ):
        _seed_tracked_wishlist()
        _save_webhook_config()
        mock_client.get_products_batch.side_effect = RuntimeError(
            "connection timed out"
        )
        posts = []
        monkeypatch.setattr(
            WebhookClient, "post", lambda self, *args: posts.append(args)
        )

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
        _seed_tracked_wishlist()
        _save_webhook_config()
        mock_client.get_products_batch.side_effect = RuntimeError(
            "Not authenticated. Run 'deals login' first."
        )
        posts = []
        monkeypatch.setattr(
            WebhookClient, "post", lambda self, *args: posts.append(args)
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["track", "run"])
        assert result.exit_code != 0

        state = json.loads(constants_mod.TRACK_STATE_FILE.read_text())
        assert len(posts) == 1
        assert state.get("auth_error_notified") is True

    def test_transient_then_auth_still_pings(
        self, mock_client, tmp_config, monkeypatch
    ):
        _seed_tracked_wishlist()
        _save_webhook_config()
        posts = []
        monkeypatch.setattr(
            WebhookClient, "post", lambda self, *args: posts.append(args)
        )
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
        _seed_tracked_wishlist()
        _save_webhook_config()

        class _AuthHTTPError(Exception):
            status_code = 401

        mock_client.get_products_batch.side_effect = _AuthHTTPError("unauthorized")
        posts = []
        monkeypatch.setattr(
            WebhookClient, "post", lambda self, *args: posts.append(args)
        )

        runner = CliRunner()
        runner.invoke(cli, ["track", "run"])
        assert len(posts) == 1


class TestTrackWebhookFormattingError:
    def test_formatting_value_error_not_treated_as_auth(
        self, mock_client, tmp_config, monkeypatch
    ):
        _seed_tracked_wishlist()
        _save_webhook_config()
        mock_client.get_products_batch.return_value = [
            make_product(asin="B00TRACK01", price=3.99)
        ]

        def _boom(*a, **kw):
            raise ValueError("Unknown webhook format: 'bogus'")

        monkeypatch.setattr(notification_service, "format_webhook_payload", _boom)
        posts = []
        monkeypatch.setattr(
            WebhookClient, "post", lambda self, *args: posts.append(args)
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["track", "run"])
        assert result.exit_code != 0
        # The formatting error must surface as itself, not a re-auth ping,
        # and must not latch auth_error_notified.
        assert posts == []
        state = json.loads(constants_mod.TRACK_STATE_FILE.read_text())
        assert state.get("auth_error_notified") is not True
        assert "Unknown webhook format" in state["run_history"][0]["error"]


class TestTrackFailurePathLocking:
    def test_failure_state_write_acquires_lock(
        self, mock_client, tmp_config, monkeypatch
    ):
        _seed_tracked_wishlist()
        mock_client.get_products_batch.side_effect = RuntimeError("boom")

        acquisitions = []
        real_run_lock = track_mod.run_lock

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

    def test_failure_state_write_does_not_bypass_held_lock(
        self, mock_client, tmp_config, monkeypatch
    ):
        _seed_tracked_wishlist()
        mock_client.get_products_batch.side_effect = RuntimeError("boom")

        from audible_deals.locking import LockHeldError

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
        # A held lock must never be bypassed by an unlocked state write.
        assert not constants_mod.TRACK_STATE_FILE.exists()


@pytest.mark.parametrize("fmt", ["slack", "discord", "teams"])
def test_webhook_formats_render_large_integer_target(fmt):
    from audible_deals.webhooks import format_webhook_payload

    target = 10**400
    body, _ = format_webhook_payload(
        [
            {
                "asin": "HUGE1",
                "title": "Huge Target",
                "price": 1.0,
                "target": target,
                "url": "https://example.com/HUGE1",
            }
        ],
        fmt,
    )

    assert f"target ${target}.00" in str(json.loads(body))


def test_webhook_template_formats_large_integer_target():
    from audible_deals.webhooks import format_webhook_payload

    target = 10**400
    body, _ = format_webhook_payload(
        [
            {
                "asin": "HUGE1",
                "title": "Huge Target",
                "price": 1.0,
                "target": target,
                "url": "https://example.com/HUGE1",
            }
        ],
        "generic",
        template="{target:.2f}",
    )

    assert body.decode() == f"{target}.00"


def test_webhook_template_preserves_ordinary_integer_target_formatting():
    from audible_deals.webhooks import format_webhook_payload

    body, _ = format_webhook_payload(
        [
            {
                "asin": "INT1",
                "title": "Integer Target",
                "price": 1.0,
                "target": 5,
                "url": "https://example.com/INT1",
            }
        ],
        "generic",
        template="{target}|{target:.2f}",
    )

    assert body.decode() == "5.0|5.00"
