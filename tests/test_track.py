"""Tracking and scheduler behavior."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import audible_deals.config_store as config_store_mod
import audible_deals.constants as constants_mod
import audible_deals.scheduler as scheduler_mod
import audible_deals.wishlist as wishlist_mod
from audible_deals.cli import cli
from audible_deals.scheduler import (
    CRON_MARKER,
    SchedulerError,
    generate_cron_line,
    generate_launchd_plist,
    generate_systemd_service,
    generate_systemd_timer,
    generate_windows_task_command,
    track_command,
    uninstall,
)
from audible_deals.webhooks import format_webhook_message
from tests.conftest import make_product


def _bugfixscheduler_fire_minutes(step: int) -> list[int]:
    return list(range(0, 60, step))


def _bugfixscheduler_min_gap_minutes(minutes_list: list[int]) -> int:
    """Smallest gap between consecutive fires, accounting for the hour wrap."""
    gaps = [minutes_list[i + 1] - minutes_list[i] for i in range(len(minutes_list) - 1)]
    # Wrap from the last fire of one hour to minute 0 of the next.
    gaps.append((60 - minutes_list[-1]) + minutes_list[0])
    return min(gaps)


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
        constants_mod.SEEN_ASINS_FILE.write_text(json.dumps(["B00EXTRA01"]))
        constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        surfaced_on = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
        (constants_mod.HISTORY_DIR / "B00EXTRA01.json").write_text(
            json.dumps(
                {
                    "marketplaces": {
                        "us": [
                            {
                                "date": surfaced_on,
                                "price": 9.99,
                                "title": "Extra",
                            }
                        ]
                    }
                }
            )
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
        eligibility = json.loads(constants_mod.REFRESH_ELIGIBILITY_FILE.read_text())
        assert eligibility["marketplaces"]["us"]["B00EXTRA01"] == surfaced_on

    def test_skips_stale_history_asins(self, mock_client, tmp_config):
        self._seed_wishlist()
        constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        old = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
        (constants_mod.HISTORY_DIR / "B00STALE01.json").write_text(
            json.dumps(
                {
                    "marketplaces": {
                        "us": [{"date": old, "price": 9.99, "title": "Stale"}]
                    }
                }
            )
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
        runner = CliRunner()
        from audible_deals.locking import run_lock

        with run_lock():
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


class TestRunHistoryHelpers:
    def test_append_inserts_newest_first(self):
        from audible_deals.track_service import append_run

        state: dict = {}
        append_run(state, {"at": "2026-01-01", "error": None})
        append_run(state, {"at": "2026-01-02", "error": None})
        assert state["run_history"][0]["at"] == "2026-01-02"
        assert state["run_history"][1]["at"] == "2026-01-01"
        assert "last_run" not in state

    def test_append_caps_at_max(self):
        from audible_deals.track_service import RUN_HISTORY_MAX, append_run

        state: dict = {}
        for i in range(RUN_HISTORY_MAX + 3):
            append_run(state, {"at": f"2026-01-{i + 1:02d}", "error": None})
        assert len(state["run_history"]) == RUN_HISTORY_MAX

    def test_append_removes_legacy_last_run(self):
        from audible_deals.track_service import append_run

        state: dict = {"last_run": {"at": "2025-12-31", "error": None}}
        append_run(state, {"at": "2026-01-01", "error": None})
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


class TestPostWebhookRetry:
    def test_succeeds_on_second_attempt(self):
        from audible_deals.webhook_client import WebhookClient

        calls = []

        class Opener:
            def open(self, request, timeout=10):
                calls.append((request, timeout))
                if len(calls) < 2:
                    raise OSError("transient")

        sleep_calls = []
        WebhookClient(
            opener=Opener(), sleep=sleep_calls.append, jitter=lambda low, high: 0
        ).post(
            "https://example.com/hook",
            b"body",
            {"Content-Type": "application/json"},
        )
        assert len(calls) == 2
        assert len(sleep_calls) == 1

    def test_exhausts_retries_and_raises(self):
        import pytest

        from audible_deals.webhook_client import (
            WebhookClient,
            WebhookDeliveryError,
        )

        class Opener:
            def open(self, request, timeout=10):
                raise OSError("nope")

        with pytest.raises(WebhookDeliveryError, match="Webhook failed"):
            WebhookClient(opener=Opener(), sleep=lambda seconds: None).post(
                "https://example.com/hook",
                b"body",
                {"Content-Type": "application/json"},
            )

    def test_succeeds_first_attempt_no_sleep(self):
        from audible_deals.webhook_client import WebhookClient

        class Opener:
            def open(self, request, timeout=10):
                return None

        sleep_calls = []
        WebhookClient(opener=Opener(), sleep=sleep_calls.append).post(
            "https://example.com/hook", b"body", {"Content-Type": "application/json"}
        )
        assert sleep_calls == []


class TestParseWebhookHeaders:
    def test_valid_header(self):
        from audible_deals.notification_workflow import parse_webhook_headers

        result = parse_webhook_headers(("Authorization: Bearer tok",))
        assert result == {"Authorization": "Bearer tok"}

    def test_multiple_headers(self):
        from audible_deals.notification_workflow import parse_webhook_headers

        result = parse_webhook_headers(("X-Key: abc", "X-Other: def"))
        assert result["X-Key"] == "abc"
        assert result["X-Other"] == "def"

    def test_rejects_missing_colon(self):
        import click
        import pytest

        from audible_deals.notification_workflow import parse_webhook_headers

        with pytest.raises(click.UsageError, match="Name: Value"):
            parse_webhook_headers(("BadHeader",))

    def test_rejects_empty_name(self):
        import click
        import pytest

        from audible_deals.notification_workflow import parse_webhook_headers

        with pytest.raises(click.UsageError):
            parse_webhook_headers((": value",))

    def test_rejects_empty_value(self):
        import click
        import pytest

        from audible_deals.notification_workflow import parse_webhook_headers

        with pytest.raises(click.UsageError):
            parse_webhook_headers(("X-Key: ",))

    def test_rejects_content_type(self):
        import click
        import pytest

        from audible_deals.notification_workflow import parse_webhook_headers

        with pytest.raises(click.UsageError, match="Content-Type"):
            parse_webhook_headers(("content-type: application/xml",))

    def test_rejects_content_type_mixed_case(self):
        import click
        import pytest

        from audible_deals.notification_workflow import parse_webhook_headers

        with pytest.raises(click.UsageError, match="Content-Type"):
            parse_webhook_headers(("Content-Type: text/plain",))

    def test_value_with_colon_preserved(self):
        from audible_deals.notification_workflow import parse_webhook_headers

        result = parse_webhook_headers(("Authorization: Bearer a:b:c",))
        assert result["Authorization"] == "Bearer a:b:c"


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

        def _fake_post(self, url, body, headers):
            captured["headers"] = dict(headers)

        monkeypatch.setattr(
            "audible_deals.webhook_client.WebhookClient.post", _fake_post
        )
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
        assert captured.get("headers", {}).get("X-Api-Key") == "secret"

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


class TestBugfixSchedulerUninstallSystemdWithoutUserBus:
    def test_removes_unit_files_when_bus_unavailable(self, tmp_path, monkeypatch):
        unit_dir = tmp_path / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        service = unit_dir / f"{scheduler_mod.SYSTEMD_UNIT}.service"
        timer = unit_dir / f"{scheduler_mod.SYSTEMD_UNIT}.timer"
        service.write_text("[Service]\n")
        timer.write_text("[Timer]\n")

        monkeypatch.setattr(scheduler_mod.sys, "platform", "linux")
        monkeypatch.setattr(scheduler_mod, "systemd_unit_dir", lambda: unit_dir)
        # No active user bus: _systemd_available() would gate cleanup off.
        monkeypatch.setattr(scheduler_mod, "_systemd_available", lambda: False)
        # crontab path: nothing to remove.
        monkeypatch.setattr(scheduler_mod, "_cron_uninstall", lambda: False)
        # systemctl calls must not raise even with check=False; stub _run.
        monkeypatch.setattr(
            scheduler_mod,
            "_run",
            lambda cmd, **kw: __import__("subprocess").CompletedProcess(cmd, 0, "", ""),
        )

        result = uninstall()

        assert result is True
        assert not service.exists()
        assert not timer.exists()


class TestBugfixSchedulerCronLineHonorsInterval:
    def test_non_divisor_is_rejected(self):
        with pytest.raises(SchedulerError, match="11-minute"):
            generate_cron_line(["/usr/bin/deals", "track", "run"], 660, Path("/l"))

    def test_twentyfive_minutes_is_rejected(self):
        with pytest.raises(SchedulerError, match="25-minute"):
            generate_cron_line(["/usr/bin/deals", "track", "run"], 1500, Path("/l"))

    def test_divisor_of_60_unchanged(self):
        line = generate_cron_line(["/usr/bin/deals", "track", "run"], 1800, Path("/l"))
        assert line.startswith("*/30 * * * * ")

    def test_large_non_divisor_is_rejected(self):
        with pytest.raises(SchedulerError, match="50-minute"):
            generate_cron_line(["/usr/bin/deals", "track", "run"], 3000, Path("/l"))

    def test_61_minutes_is_rejected(self):
        with pytest.raises(SchedulerError, match="61-minute"):
            generate_cron_line(["/usr/bin/deals", "track", "run"], 61 * 60, Path("/l"))

    def test_24_hours_is_daily(self):
        line = generate_cron_line(
            ["/usr/bin/deals", "track", "run"], 24 * 60 * 60, Path("/l")
        )
        assert line.startswith("0 0 * * * ")

    def test_48_hours_and_seconds_are_rejected(self):
        with pytest.raises(SchedulerError, match="48-hour"):
            generate_cron_line(
                ["/usr/bin/deals", "track", "run"], 48 * 60 * 60, Path("/l")
            )
        with pytest.raises(SchedulerError, match="seconds"):
            generate_cron_line(["/usr/bin/deals", "track", "run"], 630, Path("/l"))


class TestWindowsTaskCommand:
    def test_exact_ordinary_payload(self):
        payload = generate_windows_task_command(
            [r"C:\Tools\deals.exe", "track", "run"],
            Path(r"C:\Logs\track.log"),
        )

        assert payload == (
            'cmd.exe /D /S /V:OFF /C ""C:\\Tools\\deals.exe" "track" "run" '
            '>> "C:\\Logs\\track.log" 2>&1"'
        )

    def test_quotes_supported_metacharacters(self):
        payload = generate_windows_task_command(
            [
                r"C:\Program Files\Deals & Tools\deals^(prod!).exe",
                "track & review",
                "run^(!)",
            ],
            Path(r"C:\Logs & Reports\track^(!).log"),
        )

        assert payload == (
            'cmd.exe /D /S /V:OFF /C ""C:\\Program Files\\Deals & Tools\\'
            'deals^(prod!).exe" "track & review" "run^(!)" >> '
            '"C:\\Logs & Reports\\track^(!).log" 2>&1"'
        )

    def test_doubles_trailing_backslashes_before_the_closing_quote(self):
        payload = generate_windows_task_command(
            ["deals", "ends-with-backslash\\", "sentinel"],
            Path(r"C:\Logs\track.log"),
        )

        assert '"ends-with-backslash\\\\" "sentinel"' in payload

    @pytest.mark.parametrize(
        "unsafe", ['"', "\r", "\n", "\0", "<", ">", "|", "%", "%%", "%TEMP%"]
    )
    @pytest.mark.parametrize("target", ["command", "log"])
    def test_rejects_unsafe_or_expanding_values(self, unsafe, target):
        cmd = ["deals", f"track{unsafe}", "run"] if target == "command" else ["deals"]
        log_path = (
            Path(r"C:\Logs\track.log")
            if target == "command"
            else Path(f"C:\\Logs\\track{unsafe}.log")
        )

        with pytest.raises(SchedulerError, match="unsafe characters"):
            generate_windows_task_command(cmd, log_path)

    def test_rejects_empty_command(self):
        with pytest.raises(SchedulerError, match="cannot be empty"):
            generate_windows_task_command([], Path(r"C:\Logs\track.log"))

    def test_install_passes_exact_schtasks_argv(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            scheduler_mod,
            "track_command",
            lambda: [r"C:\Program Files\Audible Deals\deals.exe", "track", "run"],
        )
        monkeypatch.setattr(scheduler_mod, "_run", lambda cmd: calls.append(cmd))

        scheduler_mod._schtasks_install(
            30 * 60, Path(r"C:\Users\Me\Audible Deals\track.log")
        )

        assert calls == [
            [
                "schtasks",
                "/Create",
                "/F",
                "/TN",
                "AudibleDealsTrack",
                "/TR",
                'cmd.exe /D /S /V:OFF /C ""C:\\Program Files\\Audible Deals\\'
                'deals.exe" "track" "run" >> "C:\\Users\\Me\\Audible Deals\\'
                'track.log" 2>&1"',
                "/SC",
                "MINUTE",
                "/MO",
                "30",
            ]
        ]


class TestBugfixSchedulerWindowsScheduleHonorsInterval:
    def test_uses_minutes_without_capping(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(
            scheduler_mod, "track_command", lambda: ["deals", "track", "run"]
        )
        monkeypatch.setattr(scheduler_mod, "_run", lambda cmd: calls.append(cmd))
        scheduler_mod._schtasks_install(61 * 60, tmp_path / "track.log")
        assert ["/SC", "MINUTE", "/MO", "61"] == calls[0][-4:]

    def test_daily_intervals_and_seconds(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(
            scheduler_mod, "track_command", lambda: ["deals", "track", "run"]
        )
        monkeypatch.setattr(scheduler_mod, "_run", lambda cmd: calls.append(cmd))
        scheduler_mod._schtasks_install(48 * 60 * 60, tmp_path / "track.log")
        assert ["/SC", "DAILY", "/MO", "2"] == calls[0][-4:]
        with pytest.raises(SchedulerError, match="seconds"):
            scheduler_mod._schtasks_install(630, tmp_path / "track.log")

    def test_rejects_daily_intervals_above_supported_bound(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            scheduler_mod, "track_command", lambda: ["deals", "track", "run"]
        )
        with pytest.raises(SchedulerError, match="365 days"):
            scheduler_mod._schtasks_install(366 * 24 * 60 * 60, tmp_path / "track.log")
