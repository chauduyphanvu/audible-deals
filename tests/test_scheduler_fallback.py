"""`deals track install` where systemd has no user bus: nothing is left behind, and
the person is told what to do (or cron is used and they are told so)."""

from __future__ import annotations

import logging
import subprocess
import sys

import pytest
from click.testing import CliRunner

from audible_deals import scheduler
from audible_deals.cli import cli
from audible_deals.logging_setup import NOTE
from audible_deals.scheduler import NoSystemdBusError, SchedulerError

NO_BUS = "Failed to connect to bus: No medium found"


class FakeSystem:
    """systemctl, crontab and the unit directory, in memory."""

    def __init__(self, tmp_path, monkeypatch, *, systemctl=True, crontab=True):
        self.units = tmp_path / "units"
        self.commands: list[list[str]] = []
        self.state = "running"
        self.bus_ok = True
        self.crontab_lines: list[str] = []
        present = {"systemctl": systemctl, "crontab": crontab}
        monkeypatch.setattr(scheduler, "systemd_unit_dir", lambda: self.units)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(
            scheduler.shutil,
            "which",
            lambda name: f"/usr/bin/{name}" if present.get(name, True) else None,
        )
        monkeypatch.setattr(
            scheduler, "track_command", lambda: ["/usr/bin/deals", "track", "run"]
        )
        monkeypatch.setattr(scheduler.subprocess, "run", self._run)

    def _run(self, cmd, **kwargs):
        self.commands.append(list(cmd))
        if cmd[0] == "crontab" and cmd[1:] == ["-l"]:
            text = "".join(line + "\n" for line in self.crontab_lines)
            return subprocess.CompletedProcess(cmd, 0, text, "")
        if cmd[0] == "crontab":
            self.crontab_lines = kwargs["input"].splitlines()
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:3] == ["systemctl", "--user", "is-system-running"]:
            return subprocess.CompletedProcess(
                cmd, 0 if self.state == "running" else 1, self.state + "\n", ""
            )
        if not self.bus_ok:
            return subprocess.CompletedProcess(cmd, 1, "", NO_BUS + "\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def unit_files(self):
        return (
            sorted(p.name for p in self.units.glob("*")) if self.units.exists() else []
        )


@pytest.fixture
def system(tmp_path, monkeypatch):
    return FakeSystem(tmp_path, monkeypatch)


class TestWhenSystemdIsUsable:
    @pytest.mark.parametrize("state", ["running", "degraded", "starting"])
    def test_a_user_manager_in_these_states_takes_timers(self, system, state):
        system.state = state

        assert scheduler._systemd_available() is True

    @pytest.mark.parametrize("state", ["offline", "unknown", "stopping", ""])
    def test_no_user_manager_is_not_available(self, system, state):
        system.state = state

        assert scheduler._systemd_available() is False


class TestARolledBackInstall:
    def test_no_bus_leaves_no_unit_files_and_raises_the_specific_error(
        self, system, tmp_path
    ):
        system.bus_ok = False

        with pytest.raises(NoSystemdBusError, match="Failed to connect to bus"):
            scheduler._systemd_install(3600, tmp_path / "t.log")

        assert system.unit_files() == []

    def test_any_other_failure_also_rolls_back(self, system, tmp_path, monkeypatch):
        def broken(cmd, check=True):
            if "enable" in cmd:
                raise SchedulerError("systemctl --user enable failed (1): masked")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(scheduler, "_run", broken)

        with pytest.raises(SchedulerError, match="masked") as raised:
            scheduler._systemd_install(3600, tmp_path / "t.log")

        assert not isinstance(raised.value, NoSystemdBusError)
        assert system.unit_files() == []

    def test_a_schedule_that_was_already_there_comes_back_as_it_was(
        self, system, tmp_path
    ):
        system.units.mkdir()
        service = system.units / "audible-deals-track.service"
        service.write_text("old service")
        system.bus_ok = False

        with pytest.raises(NoSystemdBusError):
            scheduler._systemd_install(3600, tmp_path / "t.log")

        assert service.read_text() == "old service"
        assert system.unit_files() == ["audible-deals-track.service"]


class TestInstallFallsBackToCron:
    def test_no_bus_with_cron_installs_the_crontab_entry_and_says_so(
        self, system, tmp_path, caplog
    ):
        system.bus_ok = False

        with caplog.at_level(NOTE, logger="audible_deals.scheduler"):
            description = scheduler.install(3 * 3600, tmp_path / "t.log", tmp_path)

        assert description == "crontab entry"
        assert system.unit_files() == []
        assert len(system.crontab_lines) == 1
        assert system.crontab_lines[0].startswith("0 */3 * * * ")
        (record,) = [r for r in caplog.records if r.levelno == NOTE]
        assert "no user bus" in record.getMessage()
        assert "crontab entry was installed instead" in record.getMessage()

    def test_a_manager_that_is_not_running_also_uses_cron(
        self, system, tmp_path, caplog
    ):
        system.state = "offline"

        with caplog.at_level(NOTE, logger="audible_deals.scheduler"):
            description = scheduler.install(3600, tmp_path / "t.log", tmp_path)

        assert description == "crontab entry"
        assert not any(
            c[:3] == ["systemctl", "--user", "daemon-reload"] for c in system.commands
        )
        assert "no user manager running" in caplog.text

    def test_without_systemctl_cron_is_used_quietly(
        self, tmp_path, monkeypatch, caplog
    ):
        system = FakeSystem(tmp_path, monkeypatch, systemctl=False)

        with caplog.at_level(logging.DEBUG, logger="audible_deals.scheduler"):
            description = scheduler.install(3600, tmp_path / "t.log", tmp_path)

        assert description == "crontab entry"
        assert system.crontab_lines
        assert not [r for r in caplog.records if r.levelno == NOTE]

    def test_no_bus_and_no_cron_says_what_to_do_instead(self, tmp_path, monkeypatch):
        system = FakeSystem(tmp_path, monkeypatch, crontab=False)
        system.bus_ok = False

        with pytest.raises(SchedulerError) as raised:
            scheduler.install(90 * 60, tmp_path / "t.log", tmp_path)

        message = str(raised.value)
        assert "no user bus" in message
        assert "crontab is not installed" in message
        assert "nothing was installed" in message
        assert "deals track run --every 1h30m" in message
        assert system.unit_files() == []

    def test_neither_systemd_nor_cron_suggests_running_in_the_foreground(
        self, tmp_path, monkeypatch
    ):
        FakeSystem(tmp_path, monkeypatch, systemctl=False, crontab=False)

        with pytest.raises(SchedulerError, match="deals track run --every 6h"):
            scheduler.install(6 * 3600, tmp_path / "t.log", tmp_path)


class TestTheCommand:
    def test_the_failure_is_one_actionable_line_and_nothing_is_recorded(
        self, tmp_config, tmp_path, monkeypatch
    ):
        system = FakeSystem(tmp_path, monkeypatch, crontab=False)
        system.bus_ok = False
        runner = CliRunner()

        result = runner.invoke(cli, ["track", "install", "--every", "2h"])
        status = runner.invoke(cli, ["track", "status", "--json"])

        assert result.exit_code == 1
        assert "deals track run --every 2h" in result.stderr
        assert system.unit_files() == []
        assert '"installed": false' in status.stdout

    def test_with_cron_the_install_succeeds_and_the_output_says_how(
        self, tmp_config, tmp_path, monkeypatch
    ):
        system = FakeSystem(tmp_path, monkeypatch)
        system.bus_ok = False

        result = CliRunner().invoke(cli, ["track", "install", "--every", "2h"])

        assert result.exit_code == 0, result.output
        assert "Installed: crontab entry" in result.stdout
        assert "Note:" in result.stderr and "crontab entry was installed instead" in (
            result.stderr
        )
        assert system.unit_files() == []
