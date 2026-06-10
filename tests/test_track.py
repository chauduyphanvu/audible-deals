"""Tests for the track command group and scheduler unit generation."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from click.testing import CliRunner

import audible_deals.config_store as config_store_mod
import audible_deals.constants as constants_mod
import audible_deals.scheduler as scheduler_mod
import audible_deals.wishlist as wishlist_mod
from audible_deals.cli import cli
from audible_deals.scheduler import (
    CRON_MARKER,
    generate_cron_line,
    generate_launchd_plist,
    generate_systemd_service,
    generate_systemd_timer,
    track_command,
)
from audible_deals.webhooks import format_webhook_message
from tests.conftest import make_product


# ===================================================================
# Scheduler unit generation (pure text — no OS calls)
# ===================================================================


class TestTrackCommandResolution:
    def test_returns_absolute_invocation(self):
        cmd = track_command()
        assert cmd
        assert (
            cmd[-2:] == ["track", "run"]
            or cmd[-3:]
            == [
                "-m",
                "audible_deals",
                "track",
            ]
            + ["run"][:0]
        )


class TestLaunchdPlist:
    def test_contains_label_interval_and_command(self):
        out = generate_launchd_plist(
            ["/usr/local/bin/deals", "track", "run"], 21600, Path("/tmp/track.log")
        )
        assert "<string>com.audible-deals.track</string>" in out
        assert "<integer>21600</integer>" in out
        assert "<string>/usr/local/bin/deals</string>" in out
        assert "<string>track</string>" in out
        assert "<string>/tmp/track.log</string>" in out

    def test_escapes_xml_specials(self):
        out = generate_launchd_plist(
            ["/Users/a&b/deals", "track", "run"], 600, Path("/tmp/t.log")
        )
        assert "a&amp;b" in out
        assert "a&b<" not in out


class TestSystemdUnits:
    def test_service_exec_and_log(self):
        out = generate_systemd_service(
            ["/usr/bin/deals", "track", "run"], Path("/tmp/track.log")
        )
        assert "ExecStart=/usr/bin/deals track run" in out
        assert "append:/tmp/track.log" in out

    def test_service_quotes_spaces(self):
        out = generate_systemd_service(
            ["/opt/my tools/deals", "track", "run"], Path("/tmp/t.log")
        )
        assert "'/opt/my tools/deals'" in out

    def test_timer_interval_and_persistence(self):
        out = generate_systemd_timer(21600)
        assert "OnUnitActiveSec=21600s" in out
        assert "Persistent=true" in out
        assert "WantedBy=timers.target" in out


class TestCronLine:
    def test_minutes_schedule(self):
        line = generate_cron_line(["/usr/bin/deals", "track", "run"], 1800, Path("/l"))
        assert line.startswith("*/30 * * * * ")
        assert CRON_MARKER in line
        assert ">> /l 2>&1" in line

    def test_hourly_schedule(self):
        line = generate_cron_line(["/usr/bin/deals", "track", "run"], 21600, Path("/l"))
        assert line.startswith("0 */6 * * * ")


# ===================================================================
# format_webhook_message
# ===================================================================


class TestFormatWebhookMessage:
    def test_slack(self):
        body, headers = format_webhook_message("hi", "slack", title="T")
        assert json.loads(body) == {"text": "*T*\nhi"}
        assert headers["Content-Type"] == "application/json"

    def test_discord(self):
        body, _ = format_webhook_message("hi", "discord", title="T")
        assert json.loads(body) == {"content": "**T**\nhi"}

    def test_teams(self):
        body, _ = format_webhook_message("hi", "teams", title="T")
        payload = json.loads(body)
        assert payload["@type"] == "MessageCard"
        assert payload["sections"] == [{"text": "hi"}]

    def test_ntfy(self):
        body, headers = format_webhook_message("hi", "ntfy", title="T")
        assert body == b"hi"
        assert headers["Title"] == "T"

    def test_generic(self):
        body, _ = format_webhook_message("hi", "generic", title="T")
        assert json.loads(body) == {"message": "hi", "title": "T"}

    def test_unknown_format(self):
        import pytest

        with pytest.raises(ValueError):
            format_webhook_message("hi", "carrier-pigeon")


# ===================================================================
# track run
# ===================================================================


class TestTrackRun:
    def _seed_wishlist(self):
        wishlist_mod.save_wishlist(
            [{"asin": "B00TRACK01", "title": "Tracked", "max_price": 5.0, "added": ""}]
        )

    def test_records_history_and_state(self, mock_client, tmp_config):
        self._seed_wishlist()
        mock_client.get_products_batch.return_value = [
            make_product(asin="B00TRACK01", price=3.99)
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["track", "run"])
        assert result.exit_code == 0, result.output

        state = json.loads(constants_mod.TRACK_STATE_FILE.read_text())
        assert state["last_run"]["hits"] == 1
        assert state["last_run"]["wishlist_checked"] == 1
        assert state["last_run"]["error"] is None
        assert state["last_run"]["webhook_sent"] is False
        assert (constants_mod.HISTORY_DIR / "B00TRACK01.json").exists()

    def test_refreshes_recent_history_asins(self, mock_client, tmp_config):
        self._seed_wishlist()
        constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.date.today().isoformat()
        (constants_mod.HISTORY_DIR / "B00EXTRA01.json").write_text(
            json.dumps([{"date": today, "price": 9.99, "title": "Extra"}])
        )
        mock_client.get_products_batch.side_effect = [
            [make_product(asin="B00TRACK01", price=3.99)],
            [make_product(asin="B00EXTRA01", price=8.99)],
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["track", "run"])
        assert result.exit_code == 0, result.output

        second_call_asins = mock_client.get_products_batch.call_args_list[1][0][0]
        assert second_call_asins == ["B00EXTRA01"]
        state = json.loads(constants_mod.TRACK_STATE_FILE.read_text())
        assert state["last_run"]["extra_tracked_checked"] == 1

    def test_skips_stale_history_asins(self, mock_client, tmp_config):
        self._seed_wishlist()
        constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        old = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
        (constants_mod.HISTORY_DIR / "B00STALE01.json").write_text(
            json.dumps([{"date": old, "price": 9.99, "title": "Stale"}])
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="B00TRACK01", price=3.99)
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["track", "run"])
        assert result.exit_code == 0, result.output
        assert mock_client.get_products_batch.call_count == 1

    def test_lock_held_skips_quietly(self, mock_client, tmp_config):
        self._seed_wishlist()
        constants_mod.LOCK_FILE.write_text("99999")
        runner = CliRunner()
        result = runner.invoke(cli, ["track", "run"])
        assert result.exit_code == 0, result.output
        assert not constants_mod.TRACK_STATE_FILE.exists()

    def test_error_recorded_in_state(self, mock_client, tmp_config):
        self._seed_wishlist()
        mock_client.get_products_batch.side_effect = RuntimeError("auth expired")
        runner = CliRunner()
        result = runner.invoke(cli, ["track", "run"])
        assert result.exit_code != 0
        state = json.loads(constants_mod.TRACK_STATE_FILE.read_text())
        assert "auth expired" in state["last_run"]["error"]


# ===================================================================
# track install / uninstall / status
# ===================================================================


class TestTrackInstall:
    def test_installs_and_records_state(self, tmp_config, monkeypatch):
        monkeypatch.setattr(
            scheduler_mod, "install", lambda interval_s, log_path: "fake scheduler"
        )
        import audible_deals.cli.track as track_mod

        monkeypatch.setattr(
            track_mod.scheduler, "install", lambda interval_s, log_path: "fake"
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["track", "install", "--every", "2h"])
        assert result.exit_code == 0, result.output
        state = json.loads(constants_mod.TRACK_STATE_FILE.read_text())
        assert state["install"]["every"] == "2h"
        assert state["install"]["interval_s"] == 7200

    def test_rejects_tiny_interval(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["track", "install", "--every", "1m"])
        assert result.exit_code != 0
        assert "Minimum interval" in result.output

    def test_webhook_format_requires_webhook(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["track", "install", "--webhook-format", "slack"])
        assert result.exit_code != 0
        assert "--webhook" in result.output

    def test_webhook_saved_to_config(self, tmp_config, monkeypatch):
        import audible_deals.cli.track as track_mod

        monkeypatch.setattr(
            track_mod.scheduler, "install", lambda interval_s, log_path: "fake"
        )
        monkeypatch.setattr(
            "audible_deals.validation.validate_webhook_url", lambda url: None
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "track",
                "install",
                "--webhook",
                "https://example.com/hook",
                "--webhook-format",
                "slack",
            ],
        )
        assert result.exit_code == 0, result.output
        cfg = config_store_mod.load_config()
        assert cfg["webhook"] == "https://example.com/hook"
        assert cfg["webhook_format"] == "slack"


class TestTrackUninstall:
    def test_removes_install_record(self, tmp_config, monkeypatch):
        import audible_deals.cli.track as track_mod

        monkeypatch.setattr(track_mod.scheduler, "uninstall", lambda: True)
        constants_mod.TRACK_STATE_FILE.write_text(
            json.dumps({"install": {"every": "6h"}})
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["track", "uninstall"])
        assert result.exit_code == 0, result.output
        state = json.loads(constants_mod.TRACK_STATE_FILE.read_text())
        assert "install" not in state

    def test_nothing_installed(self, tmp_config, monkeypatch):
        import audible_deals.cli.track as track_mod

        monkeypatch.setattr(track_mod.scheduler, "uninstall", lambda: False)
        runner = CliRunner()
        result = runner.invoke(cli, ["track", "uninstall"])
        assert result.exit_code == 0, result.output
        assert "No schedule" in result.output


class TestTrackStatus:
    def test_not_installed(self, tmp_config, monkeypatch):
        import audible_deals.cli.track as track_mod

        monkeypatch.setattr(
            track_mod.scheduler, "installed", lambda: (False, "nowhere")
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["track", "status"])
        assert result.exit_code == 0, result.output
        assert "Not installed" in result.output

    def test_shows_last_run(self, tmp_config, monkeypatch):
        import audible_deals.cli.track as track_mod

        monkeypatch.setattr(track_mod.scheduler, "installed", lambda: (True, "plist"))
        constants_mod.TRACK_STATE_FILE.write_text(
            json.dumps(
                {
                    "install": {"every": "6h", "method": "launchd agent"},
                    "last_run": {
                        "at": "2026-06-09T08:00:00",
                        "duration_s": 4.2,
                        "wishlist_checked": 3,
                        "extra_tracked_checked": 7,
                        "hits": 1,
                        "error": None,
                    },
                }
            )
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["track", "status"])
        assert result.exit_code == 0, result.output
        assert "every 6h" in result.output
        assert "3 wishlist" in result.output
        assert "1 at" in result.output  # "1 at target" may wrap across lines

    def test_shows_error(self, tmp_config, monkeypatch):
        import audible_deals.cli.track as track_mod

        monkeypatch.setattr(track_mod.scheduler, "installed", lambda: (True, "plist"))
        constants_mod.TRACK_STATE_FILE.write_text(
            json.dumps(
                {
                    "install": {"every": "6h", "method": "launchd agent"},
                    "last_run": {"at": "2026-06-09T08:00:00", "error": "boom"},
                }
            )
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["track", "status"])
        assert result.exit_code == 0, result.output
        assert "boom" in result.output


class TestTrackLog:
    def test_no_log(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["track", "log"])
        assert result.exit_code == 0, result.output
        assert "No log yet" in result.output

    def test_tails_lines(self, tmp_config):
        constants_mod.TRACK_LOG_FILE.write_text(
            "\n".join(f"line{i}" for i in range(100)) + "\n"
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["track", "log", "-n", "3"])
        assert result.exit_code == 0, result.output
        assert "line99" in result.output
        assert "line96" not in result.output


# ===================================================================
# Config keys for webhook
# ===================================================================


class TestWebhookConfigKeys:
    def test_webhook_format_validated(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "webhook-format", "smoke"])
        assert result.exit_code != 0
        assert "Invalid webhook format" in result.output

    def test_webhook_format_accepted(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "webhook-format", "ntfy"])
        assert result.exit_code == 0, result.output
        assert config_store_mod.load_config()["webhook_format"] == "ntfy"
