"""Scheduler files: the launchd plist, atomic writes and the missing-program check."""

from __future__ import annotations

import os
import plistlib
import stat
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from audible_deals import scheduler
from audible_deals.cli import cli
from audible_deals.scheduler import (
    CRON_MARKER,
    generate_cron_line,
    generate_launchd_plist,
    generate_systemd_service,
    generate_windows_task_command,
    parse_cron_command,
    parse_launchd_command,
    parse_systemd_command,
    parse_windows_task_command,
)
from audible_deals.state_store import save_install_record

LOG = Path("/tmp/deals/track.log")


def legacy_plist(cmd, interval_s, log_path, data_dir=None):
    """The hand-built template this release replaced, kept to prove equivalence."""
    from xml.sax.saxutils import escape

    args = "\n".join(f"        <string>{escape(c)}</string>" for c in cmd)
    env = (
        f"""    <key>EnvironmentVariables</key>
    <dict>
        <key>DEALS_HOME</key>
        <string>{escape(str(data_dir))}</string>
    </dict>
"""
        if data_dir is not None
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.audible-deals.track</string>
    <key>ProgramArguments</key>
    <array>
{args}
    </array>
{env}    <key>StartInterval</key>
    <integer>{interval_s}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{escape(str(log_path))}</string>
    <key>StandardErrorPath</key>
    <string>{escape(str(log_path))}</string>
</dict>
</plist>
"""


CASES = [
    (["/usr/local/bin/deals", "track", "run"], 21600, Path("/tmp/track.log"), None),
    (["/usr/local/bin/deals", "track", "run"], 600, Path("/tmp/track.log"), Path("/d")),
    (["/Users/a&b/<my> deals", "track", "run"], 3600, Path("/tmp/t&t.log"), None),
    (
        ["/opt/x/python", "-m", "audible_deals", "track", "run"],
        1800,
        Path("/Users/Zoë/Library/Logs/deals.log"),
        Path("/Users/Zoë/data & <more>"),
    ),
]


@pytest.mark.parametrize(("cmd", "interval", "log", "home"), CASES)
def test_the_plist_means_the_same_as_the_old_template(cmd, interval, log, home):
    new = plistlib.loads(generate_launchd_plist(cmd, interval, log, home).encode())
    old = plistlib.loads(legacy_plist(cmd, interval, log, home).encode())

    assert new == old


def test_the_plist_is_a_valid_launchd_definition():
    plist = plistlib.loads(
        generate_launchd_plist(
            ["/bin/deals", "track", "run"], 900, LOG, Path("/d")
        ).encode()
    )

    assert plist == {
        "Label": "com.audible-deals.track",
        "ProgramArguments": ["/bin/deals", "track", "run"],
        "EnvironmentVariables": {"DEALS_HOME": "/d"},
        "StartInterval": 900,
        "RunAtLoad": False,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
    }
    assert type(plist["StartInterval"]) is int


def test_the_plist_text_starts_like_a_plist():
    text = generate_launchd_plist(["/bin/deals"], 600, LOG)

    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "<!DOCTYPE plist" in text and "<plist version=" in text


class TestAtomicUnitFiles:
    def test_the_files_are_written_whole_and_readable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scheduler, "systemd_unit_dir", lambda: tmp_path / "units")
        monkeypatch.setattr(
            scheduler, "track_command", lambda: ["/bin/deals", "track", "run"]
        )
        calls = []
        monkeypatch.setattr(scheduler, "_run", lambda cmd, **kw: calls.append(cmd))

        description = scheduler._systemd_install(3600, LOG, Path("/d"))

        service = tmp_path / "units" / "audible-deals-track.service"
        timer = tmp_path / "units" / "audible-deals-track.timer"
        assert "ExecStart=/bin/deals track run" in service.read_text()
        assert "OnUnitActiveSec=3600s" in timer.read_text()
        if sys.platform != "win32":
            assert stat.S_IMODE(service.stat().st_mode) == 0o644
        assert [
            p.name for p in (tmp_path / "units").iterdir() if p.name.startswith(".tmp")
        ] == []
        assert description.startswith("systemd user timer")

    def test_a_failed_write_leaves_the_old_file_intact(self, tmp_path, monkeypatch):
        target = tmp_path / "unit.service"
        target.write_text("old contents")

        def broken_replace(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", broken_replace)

        with pytest.raises(OSError):
            scheduler._write_unit(target, "new contents")

        assert target.read_text() == "old contents"
        assert [p.name for p in tmp_path.iterdir()] == ["unit.service"]  # no temp left

    def test_the_plist_is_written_atomically(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            scheduler, "launchd_plist_path", lambda: tmp_path / "LA" / "x.plist"
        )
        monkeypatch.setattr(
            scheduler, "track_command", lambda: ["/bin/deals", "track", "run"]
        )
        monkeypatch.setattr(scheduler, "_run", lambda cmd, **kw: _Proc())
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)

        scheduler._launchd_install(600, LOG, Path("/d"))

        assert parse_launchd_command((tmp_path / "LA" / "x.plist").read_bytes()) == [
            "/bin/deals",
            "track",
            "run",
        ]


class _Proc:
    returncode = 0
    stdout = ""
    stderr = ""


class TestReadingBackWhatWillRun:
    CMD = ["/opt/my tools/deals", "track", "run"]

    def test_launchd(self):
        text = generate_launchd_plist(self.CMD, 600, LOG, Path("/d"))

        assert parse_launchd_command(text.encode()) == self.CMD

    def test_systemd(self):
        text = generate_systemd_service(self.CMD, LOG, Path("/d"))

        assert parse_systemd_command(text) == self.CMD

    def test_cron(self):
        line = generate_cron_line(self.CMD, 1800, LOG, Path("/home/me/d%ata"))

        assert parse_cron_command(line) == self.CMD
        assert parse_cron_command(generate_cron_line(self.CMD, 1800, LOG)) == self.CMD

    def test_windows(self):
        cmd = [r"C:\Program Files\deals\deals.exe", "track", "run"]
        task = generate_windows_task_command(
            cmd, Path(r"C:\logs\track.log"), Path(r"C:\data")
        )

        assert parse_windows_task_command(task) == cmd
        assert (
            parse_windows_task_command(
                generate_windows_task_command(cmd, Path(r"C:\logs\track.log"))
            )
            == cmd
        )

    @pytest.mark.parametrize(
        "junk", [b"", b"not a plist", b"<plist><string>x</string></plist>"]
    )
    def test_garbage_gives_no_command(self, junk):
        assert parse_launchd_command(junk) == []
        assert parse_systemd_command("[Service]\nType=oneshot\n") == []
        assert parse_cron_command("nonsense") == []


class TestMissingProgram:
    @pytest.fixture
    def installed_command(self, monkeypatch):
        def set_command(command):
            monkeypatch.setattr(scheduler, "scheduled_command", lambda: command)

        return set_command

    def test_an_existing_program_is_fine(self, installed_command):
        installed_command([sys.executable, "track", "run"])

        assert scheduler.missing_executable() is None

    def test_a_missing_absolute_path_is_reported(self, installed_command, tmp_path):
        installed_command([str(tmp_path / "gone" / "deals"), "track", "run"])

        assert scheduler.missing_executable() == str(tmp_path / "gone" / "deals")

    def test_a_file_that_is_not_executable_is_reported(
        self, installed_command, tmp_path
    ):
        program = tmp_path / "deals"
        program.write_text("#!/bin/sh\n")
        program.chmod(0o644)
        installed_command([str(program)])

        if sys.platform == "win32":
            pytest.skip("POSIX execute bit")
        assert scheduler.missing_executable() == str(program)

    def test_a_bare_name_is_looked_up_on_the_path(self, installed_command):
        installed_command(["definitely-not-installed-xyz", "track", "run"])

        assert scheduler.missing_executable() == "definitely-not-installed-xyz"

    def test_nothing_readable_means_nothing_to_report(self, installed_command):
        installed_command(None)

        assert scheduler.missing_executable() is None


class TestDoctorWarns:
    def _install(self, tmp_config, monkeypatch, command):
        save_install_record(
            {"every": "6h", "interval_s": 21600, "method": "test", "installed_at": "x"}
        )
        monkeypatch.setattr(scheduler, "installed", lambda: (True, "somewhere"))
        monkeypatch.setattr(scheduler, "scheduled_command", lambda: command)

    def test_a_schedule_pointing_at_a_missing_program_is_a_warning(
        self, tmp_config, mock_client, monkeypatch
    ):
        self._install(
            tmp_config, monkeypatch, ["/nowhere/at/all/deals", "track", "run"]
        )

        result = CliRunner().invoke(cli, ["doctor", "--json"])

        checks = {
            c["name"]: c for c in __import__("json").loads(result.stdout)["checks"]
        }
        row = checks["Scheduled command"]
        assert row["status"] == "warn"
        assert "/nowhere/at/all/deals" in row["detail"]
        assert "deals track install" in row["detail"]

    def test_a_healthy_schedule_adds_no_row(self, tmp_config, mock_client, monkeypatch):
        self._install(tmp_config, monkeypatch, [sys.executable, "track", "run"])

        result = CliRunner().invoke(cli, ["doctor", "--json"])

        names = [c["name"] for c in __import__("json").loads(result.stdout)["checks"]]
        assert "Scheduled command" not in names
        assert "Background tracking" in names


def test_the_crontab_marker_is_still_what_uninstall_looks_for():
    line = generate_cron_line(["/bin/deals", "track", "run"], 1800, LOG)

    assert line.endswith(CRON_MARKER)
