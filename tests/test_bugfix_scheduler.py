"""Regression tests for scheduler bugfixes (bugs 25, 26)."""

from __future__ import annotations

from pathlib import Path

import audible_deals.scheduler as scheduler_mod
from audible_deals.scheduler import generate_cron_line, uninstall


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
    def test_non_divisor_does_not_undershoot_interval(self):
        # 11 minutes -> */11 would fire ...:44,:55,:00 (a 5-minute boundary gap).
        line = generate_cron_line(["/usr/bin/deals", "track", "run"], 660, Path("/l"))
        step = int(line.split(" ", 1)[0].removeprefix("*/"))
        assert _min_gap_minutes(_fire_minutes(step)) >= 11

    def test_twentyfive_minutes_does_not_undershoot(self):
        line = generate_cron_line(["/usr/bin/deals", "track", "run"], 1500, Path("/l"))
        step = int(line.split(" ", 1)[0].removeprefix("*/"))
        assert _min_gap_minutes(_fire_minutes(step)) >= 25

    def test_divisor_of_60_unchanged(self):
        line = generate_cron_line(["/usr/bin/deals", "track", "run"], 1800, Path("/l"))
        assert line.startswith("*/30 * * * * ")

    def test_large_non_divisor_falls_back_to_hourly(self):
        # 50 minutes has no divisor of 60 between 50 and 59; round up to hourly.
        line = generate_cron_line(["/usr/bin/deals", "track", "run"], 3000, Path("/l"))
        assert line.startswith("0 * * * * ")
