"""Regression tests for scheduler bugfixes (bugs 25, 26)."""

from __future__ import annotations

from pathlib import Path

import pytest

import audible_deals.scheduler as scheduler_mod
from audible_deals.scheduler import SchedulerError, generate_cron_line, uninstall


# ===================================================================
# Bug 25: uninstall() must remove systemd unit files even when the
# user bus is unavailable, mirroring installed()'s file-existence check.
# ===================================================================


class TestUninstallSystemdWithoutUserBus:
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


# ===================================================================
# Bug 26: cron schedule must never produce an inter-fire gap (including
# the hour boundary) shorter than the requested interval.
# ===================================================================


def _fire_minutes(step: int) -> list[int]:
    return list(range(0, 60, step))


def _min_gap_minutes(minutes_list: list[int]) -> int:
    """Smallest gap between consecutive fires, accounting for the hour wrap."""
    gaps = [minutes_list[i + 1] - minutes_list[i] for i in range(len(minutes_list) - 1)]
    # Wrap from the last fire of one hour to minute 0 of the next.
    gaps.append((60 - minutes_list[-1]) + minutes_list[0])
    return min(gaps)


class TestCronLineHonorsInterval:
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


class TestWindowsScheduleHonorsInterval:
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
