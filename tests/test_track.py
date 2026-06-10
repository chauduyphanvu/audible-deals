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
        last = state["run_history"][0]
        assert last["hits"] == 1
        assert last["wishlist_checked"] == 1
        assert last["error"] is None
        assert last["webhook_sent"] is False
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
        assert state["run_history"][0]["extra_tracked_checked"] == 1

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
        assert "auth expired" in state["run_history"][0]["error"]


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


# ===================================================================
# Feature 1: run-history ring buffer
# ===================================================================


class TestRunHistoryHelpers:
    def test_append_inserts_newest_first(self):
        from audible_deals.cli.track import _append_run

        state: dict = {}
        _append_run(state, {"at": "2026-01-01", "error": None})
        _append_run(state, {"at": "2026-01-02", "error": None})
        assert state["run_history"][0]["at"] == "2026-01-02"
        assert state["run_history"][1]["at"] == "2026-01-01"
        assert "last_run" not in state

    def test_append_caps_at_max(self):
        from audible_deals.cli.track import _RUN_HISTORY_MAX, _append_run

        state: dict = {}
        for i in range(_RUN_HISTORY_MAX + 3):
            _append_run(state, {"at": f"2026-01-{i + 1:02d}", "error": None})
        assert len(state["run_history"]) == _RUN_HISTORY_MAX

    def test_append_removes_legacy_last_run(self):
        from audible_deals.cli.track import _append_run

        state: dict = {"last_run": {"at": "2025-12-31", "error": None}}
        _append_run(state, {"at": "2026-01-01", "error": None})
        assert "last_run" not in state
        assert state["run_history"][0]["at"] == "2026-01-01"

    def test_run_history_returns_new_format(self):
        from audible_deals.cli.track import _run_history

        state = {"run_history": [{"at": "2026-01-02"}, {"at": "2026-01-01"}]}
        assert _run_history(state)[0]["at"] == "2026-01-02"

    def test_run_history_falls_back_to_last_run(self):
        from audible_deals.cli.track import _run_history

        state = {"last_run": {"at": "2026-01-01", "error": None}}
        result = _run_history(state)
        assert len(result) == 1
        assert result[0]["at"] == "2026-01-01"

    def test_run_history_empty_state(self):
        from audible_deals.cli.track import _run_history

        assert _run_history({}) == []


class TestTrackStatusHistory:
    def test_history_flag_renders_table(self, tmp_config, monkeypatch):
        import audible_deals.cli.track as track_mod

        monkeypatch.setattr(
            track_mod.scheduler, "installed", lambda: (False, "nowhere")
        )
        constants_mod.TRACK_STATE_FILE.write_text(
            json.dumps(
                {
                    "run_history": [
                        {
                            "at": "2026-06-10T10:00:00",
                            "duration_s": 3.5,
                            "wishlist_checked": 2,
                            "extra_tracked_checked": 5,
                            "hits": 0,
                            "error": None,
                        },
                        {
                            "at": "2026-06-10T04:00:00",
                            "duration_s": 2.1,
                            "wishlist_checked": 2,
                            "extra_tracked_checked": 5,
                            "hits": 1,
                            "error": None,
                        },
                    ]
                }
            )
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["track", "status", "--history"])
        assert result.exit_code == 0, result.output
        assert "Run History" in result.output
        assert "2026-06-10T10:00:00" in result.output
        assert "ok" in result.output

    def test_history_flag_shows_error_rows(self, tmp_config, monkeypatch):
        import audible_deals.cli.track as track_mod

        monkeypatch.setattr(
            track_mod.scheduler, "installed", lambda: (False, "nowhere")
        )
        constants_mod.TRACK_STATE_FILE.write_text(
            json.dumps(
                {
                    "run_history": [
                        {
                            "at": "2026-06-10T10:00:00",
                            "duration_s": 1.0,
                            "error": "boom",
                        },
                    ]
                }
            )
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["track", "status", "--history"])
        assert result.exit_code == 0, result.output
        assert "boom" in result.output


# ===================================================================
# Feature 1: doctor consecutive-failure streak
# ===================================================================


class TestDoctorTrackStreak:
    def _write_state_with_runs(self, runs):
        constants_mod.TRACK_STATE_FILE.write_text(
            json.dumps(
                {
                    "install": {"every": "6h", "interval_s": 21600, "method": "fake"},
                    "run_history": runs,
                }
            )
        )

    def test_single_failure_shows_plain_fail(self, tmp_config, monkeypatch):
        import audible_deals.cli.misc as misc_mod
        import audible_deals.scheduler as scheduler_mod

        monkeypatch.setattr(scheduler_mod, "installed", lambda: (True, "plist"))
        self._write_state_with_runs(
            [
                {"at": "2026-06-10T10:00:00", "error": "auth expired"},
                {"at": "2026-06-10T04:00:00", "error": None},
            ]
        )
        rows = misc_mod._track_checks()
        last_row = next(r for r in rows if r[0] == "Last tracked run")
        assert last_row[1] == "FAIL"
        assert "Failing for" not in last_row[2]

    def test_three_consecutive_failures_show_streak(self, tmp_config, monkeypatch):
        import audible_deals.cli.misc as misc_mod
        import audible_deals.scheduler as scheduler_mod

        monkeypatch.setattr(scheduler_mod, "installed", lambda: (True, "plist"))
        self._write_state_with_runs(
            [
                {"at": "2026-06-10T10:00:00", "error": "err3"},
                {"at": "2026-06-10T04:00:00", "error": "err2"},
                {"at": "2026-06-09T22:00:00", "error": "err1"},
                {"at": "2026-06-09T16:00:00", "error": None},
            ]
        )
        rows = misc_mod._track_checks()
        last_row = next(r for r in rows if r[0] == "Last tracked run")
        assert last_row[1] == "FAIL"
        assert "Failing for 3 consecutive runs" in last_row[2]
        assert "err3" in last_row[2]

    def test_streak_breaks_on_success(self, tmp_config, monkeypatch):
        import audible_deals.cli.misc as misc_mod
        import audible_deals.scheduler as scheduler_mod

        monkeypatch.setattr(scheduler_mod, "installed", lambda: (True, "plist"))
        self._write_state_with_runs(
            [
                {"at": "2026-06-10T10:00:00", "error": "err2"},
                {"at": "2026-06-10T04:00:00", "error": None},
                {"at": "2026-06-09T22:00:00", "error": "err_old"},
            ]
        )
        rows = misc_mod._track_checks()
        last_row = next(r for r in rows if r[0] == "Last tracked run")
        assert last_row[1] == "FAIL"
        assert "Failing for" not in last_row[2]


# ===================================================================
# Feature 2: webhook retry
# ===================================================================


class TestPostWebhookRetry:
    def test_succeeds_on_second_attempt(self, monkeypatch):
        import audible_deals.cli.notify as notify_mod

        calls = []

        def _fake_urlopen(req, timeout=10):
            calls.append(1)
            if len(calls) < 2:
                raise OSError("transient")

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        sleep_calls = []
        monkeypatch.setattr(
            "audible_deals.cli.notify.time.sleep", lambda s: sleep_calls.append(s)
        )

        notify_mod._post_webhook(
            "https://example.com/hook", b"body", {"Content-Type": "application/json"}
        )
        assert len(calls) == 2
        assert len(sleep_calls) == 1

    def test_exhausts_retries_and_raises(self, monkeypatch):
        import pytest
        import click
        import audible_deals.cli.notify as notify_mod

        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("nope")),
        )
        monkeypatch.setattr("audible_deals.cli.notify.time.sleep", lambda s: None)

        with pytest.raises(click.ClickException, match="Webhook failed"):
            notify_mod._post_webhook(
                "https://example.com/hook",
                b"body",
                {"Content-Type": "application/json"},
            )

    def test_succeeds_first_attempt_no_sleep(self, monkeypatch):
        import audible_deals.cli.notify as notify_mod

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: None)
        sleep_calls = []
        monkeypatch.setattr(
            "audible_deals.cli.notify.time.sleep", lambda s: sleep_calls.append(s)
        )

        notify_mod._post_webhook(
            "https://example.com/hook", b"body", {"Content-Type": "application/json"}
        )
        assert sleep_calls == []


# ===================================================================
# Feature 2: header parsing
# ===================================================================


class TestParseWebhookHeaders:
    def test_valid_header(self):
        from audible_deals.cli.notify import _parse_webhook_headers

        result = _parse_webhook_headers(("Authorization: Bearer tok",))
        assert result == {"Authorization": "Bearer tok"}

    def test_multiple_headers(self):
        from audible_deals.cli.notify import _parse_webhook_headers

        result = _parse_webhook_headers(("X-Key: abc", "X-Other: def"))
        assert result["X-Key"] == "abc"
        assert result["X-Other"] == "def"

    def test_rejects_missing_colon(self):
        import pytest
        import click
        from audible_deals.cli.notify import _parse_webhook_headers

        with pytest.raises(click.UsageError, match="Name: Value"):
            _parse_webhook_headers(("BadHeader",))

    def test_rejects_empty_name(self):
        import pytest
        import click
        from audible_deals.cli.notify import _parse_webhook_headers

        with pytest.raises(click.UsageError):
            _parse_webhook_headers((": value",))

    def test_rejects_empty_value(self):
        import pytest
        import click
        from audible_deals.cli.notify import _parse_webhook_headers

        with pytest.raises(click.UsageError):
            _parse_webhook_headers(("X-Key: ",))

    def test_rejects_content_type(self):
        import pytest
        import click
        from audible_deals.cli.notify import _parse_webhook_headers

        with pytest.raises(click.UsageError, match="Content-Type"):
            _parse_webhook_headers(("content-type: application/xml",))

    def test_rejects_content_type_mixed_case(self):
        import pytest
        import click
        from audible_deals.cli.notify import _parse_webhook_headers

        with pytest.raises(click.UsageError, match="Content-Type"):
            _parse_webhook_headers(("Content-Type: text/plain",))

    def test_value_with_colon_preserved(self):
        from audible_deals.cli.notify import _parse_webhook_headers

        result = _parse_webhook_headers(("Authorization: Bearer a:b:c",))
        assert result["Authorization"] == "Bearer a:b:c"


# ===================================================================
# Feature 2: --webhook-header wired into notify and recap commands
# ===================================================================


class TestWebhookHeaderOption:
    def test_notify_webhook_header_requires_webhook(self, tmp_config, mock_client):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["notify", "--webhook-header", "X-Key: abc"],
        )
        assert result.exit_code != 0
        assert "--webhook" in result.output

    def test_recap_webhook_header_requires_webhook(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["recap", "--webhook-header", "X-Key: abc"],
        )
        assert result.exit_code != 0
        assert "--webhook" in result.output

    def test_notify_header_present_in_post(self, tmp_config, mock_client, monkeypatch):
        import audible_deals.wishlist as wishlist_mod

        wishlist_mod.save_wishlist(
            [{"asin": "B00NOTIF01", "title": "T", "max_price": 5.0, "added": ""}]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="B00NOTIF01", price=3.99)
        ]
        captured = {}

        def _fake_urlopen(req, timeout=10):
            captured["headers"] = dict(req.headers)

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        monkeypatch.setattr(
            "audible_deals.cli.notify.validate_webhook_url", lambda url: None
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "notify",
                "--webhook",
                "https://example.com/hook",
                "--webhook-header",
                "X-Api-Key: secret",
            ],
        )
        assert result.exit_code == 0, result.output
        # urllib capitalizes header names
        assert captured.get("headers", {}).get("X-api-key") == "secret"

    def test_track_install_webhook_header_requires_webhook(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["track", "install", "--webhook-header", "X-Key: abc"],
        )
        assert result.exit_code != 0
        assert "--webhook" in result.output

    def test_track_install_saves_webhook_headers(self, tmp_config, monkeypatch):
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
                "--webhook-header",
                "Authorization: Bearer tok",
            ],
        )
        assert result.exit_code == 0, result.output
        cfg = config_store_mod.load_config()
        assert cfg["webhook_headers"] == ["Authorization: Bearer tok"]
