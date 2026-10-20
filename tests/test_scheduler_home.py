"""Scheduled jobs are pinned to the data directory chosen at install time.

launchd, systemd and cron do not inherit the shell environment, so a job that
relied on ``XDG_CONFIG_HOME`` or ``DEALS_HOME`` would silently read another
directory. Every scheduler writes ``DEALS_HOME`` into the job itself.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from audible_deals import paths, scheduler
from audible_deals.scheduler import (
    SchedulerError,
    generate_cron_line,
    generate_launchd_plist,
    generate_systemd_service,
    generate_windows_task_command,
)
from tests.conftest import posix_scheduler_text

CMD = ["/usr/bin/deals", "track", "run"]
LOG = Path("/data/track.log")


@posix_scheduler_text
class TestSystemd:
    def test_service_sets_the_environment(self):
        unit = generate_systemd_service(CMD, LOG, Path("/home/me/deals-data"))

        assert 'Environment="DEALS_HOME=/home/me/deals-data"' in unit
        # the environment must come before ExecStart in the [Service] section
        assert unit.index("Environment=") < unit.index("ExecStart=")

    def test_without_a_data_dir_nothing_is_added(self):
        assert "DEALS_HOME" not in generate_systemd_service(CMD, LOG)

    def test_quotes_backslashes_and_percent_are_escaped(self):
        unit = generate_systemd_service(CMD, LOG, Path('/data/my "dir"\\x%h'))
        assert 'Environment="DEALS_HOME=/data/my \\"dir\\"\\\\x%%h"' in unit


@posix_scheduler_text
class TestCron:
    def test_line_sets_the_environment_for_the_command(self):
        line = generate_cron_line(CMD, 3600, LOG, Path("/home/me/deals-data"))

        assert line.startswith(
            "0 * * * * DEALS_HOME=/home/me/deals-data /usr/bin/deals"
        )
        assert line.endswith("# audible-deals-track")

    def test_paths_with_spaces_are_quoted(self):
        line = generate_cron_line(CMD, 3600, LOG, Path("/home/me/my data"))
        assert "DEALS_HOME='/home/me/my data' " in line

    def test_percent_is_escaped_for_cron(self):
        line = generate_cron_line(CMD, 3600, Path("/a/100%/track.log"), Path("/d%d"))
        assert "100\\%" in line and "d\\%d" in line
        assert "%" not in line.replace("\\%", "")

    @pytest.mark.skipif(sys.platform == "win32", reason="runs the line through sh")
    @pytest.mark.parametrize(
        "directory", ["/tmp/plain", "/tmp/with space", "/tmp/it's here"]
    )
    def test_a_shell_really_passes_the_variable_through(self, tmp_path, directory):
        probe = [sys.executable, "-c", "import os; print(os.environ['DEALS_HOME'])"]
        line = generate_cron_line(probe, 3600, tmp_path / "log", Path(directory))
        command = line.split(" ", 5)[5].removesuffix(scheduler.CRON_MARKER).strip()
        command = command.replace("\\%", "%")

        subprocess.run(["sh", "-c", command], check=True)

        # cron redirects the job's output to the log file
        assert (tmp_path / "log").read_text().strip() == directory


@posix_scheduler_text
class TestLaunchd:
    def test_plist_has_environment_variables(self):
        plist = generate_launchd_plist(CMD, 3600, LOG, Path("/Users/me/data"))

        assert "<key>EnvironmentVariables</key>" in plist
        assert "<key>DEALS_HOME</key>" in plist
        assert "<string>/Users/me/data</string>" in plist

    def test_markup_in_the_path_is_escaped(self):
        plist = generate_launchd_plist(CMD, 3600, LOG, Path("/Users/a&b/<data>"))
        assert "<string>/Users/a&amp;b/&lt;data&gt;</string>" in plist

    def test_without_a_data_dir_the_plist_is_unchanged(self):
        assert "EnvironmentVariables" not in generate_launchd_plist(CMD, 3600, LOG)


class TestWindows:
    def test_command_sets_the_variable_first(self):
        command = generate_windows_task_command(
            ["C:\\deals.exe", "track", "run"],
            Path("C:\\t.log"),
            Path("C:\\Users\\a b\\data"),
        )
        assert command == (
            'cmd.exe /D /S /V:OFF /C "set "DEALS_HOME=C:\\Users\\a b\\data" && '
            '"C:\\deals.exe" "track" "run" >> "C:\\t.log" 2>&1"'
        )

    def test_unsafe_characters_in_the_data_dir_are_refused(self):
        with pytest.raises(SchedulerError, match="unsafe"):
            generate_windows_task_command(CMD, LOG, Path('C:\\evil"&calc'))
        with pytest.raises(SchedulerError, match="unsafe"):
            generate_windows_task_command(CMD, LOG, Path("C:\\%PATH%"))

    def test_without_a_data_dir_nothing_is_added(self):
        assert "DEALS_HOME" not in generate_windows_task_command(CMD, LOG)


class TestInstallPinsTheCurrentDirectory:
    def test_install_uses_the_directory_this_process_resolves(
        self, tmp_config, monkeypatch
    ):
        seen = {}
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(scheduler, "_systemd_available", lambda: True)
        monkeypatch.setattr(
            scheduler,
            "_systemd_install",
            lambda interval, log, data_dir=None: (
                seen.update(dir=data_dir, log=log) or "x"
            ),
        )

        scheduler.install(3600, tmp_config / "track.log")

        assert seen["dir"] == tmp_config == paths.data_dir()

    def test_an_explicit_directory_wins(self, tmp_config, tmp_path, monkeypatch):
        seen = {}
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            scheduler,
            "_launchd_install",
            lambda interval, log, data_dir=None: seen.update(dir=data_dir) or "x",
        )

        scheduler.install(3600, tmp_config / "track.log", tmp_path / "chosen")

        assert seen["dir"] == tmp_path / "chosen"

    def test_cron_install_writes_the_variable_into_the_crontab(
        self, tmp_config, monkeypatch
    ):
        written = []
        monkeypatch.setattr(scheduler.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(scheduler, "_read_crontab", lambda: ["MAILTO=me"])
        monkeypatch.setattr(scheduler, "_write_crontab", written.append)
        monkeypatch.setattr(scheduler, "track_command", lambda: CMD)

        scheduler._cron_install(3600, tmp_config / "track.log", tmp_config)

        lines = written[0]
        assert lines[0] == "MAILTO=me"
        assert (
            f"DEALS_HOME={tmp_config}" in lines[1]
            or f"DEALS_HOME='{tmp_config}'" in lines[1]
        )

    def test_track_install_reports_where_the_job_will_read_and_write(
        self, tmp_config, monkeypatch
    ):
        from click.testing import CliRunner

        import audible_deals.cli.track as track_mod
        from audible_deals.cli import cli

        monkeypatch.setattr(
            track_mod.scheduler, "install", lambda interval, log: "fake"
        )

        result = CliRunner().invoke(cli, ["track", "install", "--every", "2h"])

        assert result.exit_code == 0, result.output
        assert f"Data: {tmp_config}" in result.output
