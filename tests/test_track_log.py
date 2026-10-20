"""track.log: rotation at the start of a run, and reading only the tail."""

from __future__ import annotations

import os
import stat
import sys

import pytest
from click.testing import CliRunner

from audible_deals import paths
from audible_deals.cli import cli
from audible_deals.track_log import KEEP, MAX_BYTES, rotate_log, tail_lines


def generation(path, number):
    return path.with_name(f"{path.name}.{number}")


def fill(path, marker, size=MAX_BYTES + 1):
    text = (marker + "\n") * (size // (len(marker) + 1) + 1)
    path.write_bytes(text.encode())
    return path.stat().st_size


class TestRotation:
    def test_a_small_log_is_left_alone(self, tmp_path):
        log = tmp_path / "track.log"
        log.write_bytes(b"x" * MAX_BYTES)

        assert rotate_log(log) is False
        assert log.stat().st_size == MAX_BYTES
        assert not generation(log, 1).exists()

    def test_a_missing_log_is_not_an_error(self, tmp_path):
        assert rotate_log(tmp_path / "track.log") is False

    def test_a_large_log_moves_to_generation_one_and_is_truncated_in_place(
        self, tmp_path
    ):
        log = tmp_path / "track.log"
        size = fill(log, "first")
        inode = log.stat().st_ino

        assert rotate_log(log) is True

        assert log.stat().st_size == 0
        assert generation(log, 1).stat().st_size == size
        assert generation(log, 1).read_text().startswith("first\n")
        if sys.platform != "win32":
            assert (
                log.stat().st_ino == inode
            )  # same file: an open appender keeps working

    def test_an_open_appender_keeps_writing_into_the_new_log(self, tmp_path):
        """What the scheduler's `>> track.log` redirect does across a rotation."""
        log = tmp_path / "track.log"
        fill(log, "old")
        with open(log, "ab") as appender:
            rotate_log(log)
            appender.write(b"after rotation\n")
            appender.flush()

        assert log.read_text() == "after rotation\n"

    def test_generations_shift_and_the_oldest_is_dropped(self, tmp_path):
        log = tmp_path / "track.log"
        for marker in ("one", "two", "three", "four"):
            fill(log, marker)
            assert rotate_log(log)

        assert KEEP == 3
        assert [generation(log, n).read_text().split("\n")[0] for n in (1, 2, 3)] == [
            "four",
            "three",
            "two",
        ]  # "one" fell off the end
        assert not generation(log, 4).exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_the_rotated_file_keeps_the_mode(self, tmp_path):
        log = tmp_path / "track.log"
        fill(log, "x")
        log.chmod(0o600)

        rotate_log(log)

        assert stat.S_IMODE(generation(log, 1).stat().st_mode) == 0o600

    def test_a_failure_to_rotate_never_raises(self, tmp_path, monkeypatch):
        log = tmp_path / "track.log"
        fill(log, "x")

        def broken(*args, **kwargs):
            raise PermissionError("held open")

        monkeypatch.setattr(os, "open", broken)

        assert rotate_log(log) is False
        assert log.stat().st_size > MAX_BYTES  # untouched

    def test_a_track_run_rotates_before_it_does_anything(self, tmp_config, mock_client):
        log = paths.track_log_file()
        log.parent.mkdir(parents=True, exist_ok=True)
        fill(log, "yesterday")

        result = CliRunner().invoke(cli, ["track", "run"])

        assert result.exit_code == 0, result.output
        assert log.stat().st_size == 0
        assert generation(log, 1).read_text().startswith("yesterday\n")

    def test_a_loop_rotates_at_the_start_of_every_run(
        self, tmp_config, mock_client, monkeypatch
    ):
        from functools import partial

        import audible_deals.cli.track as track_cli
        from audible_deals.track_loop import LoopClock, run_every

        monkeypatch.setattr(
            track_cli,
            "run_every",
            partial(run_every, clock=LoopClock(sleep=lambda s: None), max_runs=2),
        )
        log = paths.track_log_file()
        log.parent.mkdir(parents=True, exist_ok=True)
        rotations = []
        real = track_cli.rotate_log

        def spy(path):
            rotations.append(path)
            return real(path)

        monkeypatch.setattr(track_cli, "rotate_log", spy)

        CliRunner().invoke(cli, ["track", "run", "--every", "30m"])

        assert len(rotations) == 2


class TestTail:
    def test_the_last_lines_in_order(self, tmp_path):
        log = tmp_path / "t.log"
        log.write_text("".join(f"line {i}\n" for i in range(1000)))

        assert tail_lines(log, 3) == ["line 997", "line 998", "line 999"]

    def test_fewer_lines_than_asked_returns_them_all(self, tmp_path):
        log = tmp_path / "t.log"
        log.write_text("a\nb\n")

        assert tail_lines(log, 50) == ["a", "b"]

    def test_a_missing_final_newline_and_crlf(self, tmp_path):
        log = tmp_path / "t.log"
        log.write_bytes(b"a\r\nb\r\nc")

        assert tail_lines(log, 2) == ["b", "c"]

    def test_an_empty_file(self, tmp_path):
        log = tmp_path / "t.log"
        log.write_bytes(b"")

        assert tail_lines(log, 5) == []

    def test_lines_longer_than_a_block_and_invalid_utf8(self, tmp_path):
        log = tmp_path / "t.log"
        log.write_bytes(b"short\n" + b"x" * 30000 + b"\n\xff\xfe bad\n")

        lines = tail_lines(log, 2)

        assert lines[0] == "x" * 30000
        assert lines[1].endswith(" bad")

    def test_a_block_boundary_never_returns_a_cut_line(self, tmp_path):
        log = tmp_path / "t.log"
        rows = [f"row-{i:05d}-" + "y" * (i % 97) for i in range(3000)]
        log.write_text("\n".join(rows) + "\n")

        for count in (1, 7, 100, 999):
            assert tail_lines(log, count) == rows[-count:]

    def test_it_reads_only_the_end_of_a_big_file(self, tmp_path, monkeypatch):
        log = tmp_path / "t.log"
        log.write_bytes(b"old line\n" * 500_000 + b"the end\n")  # ~4.5 MB
        read = []
        real_open = type(log).open

        def counting_open(self, *args, **kwargs):
            handle = real_open(self, *args, **kwargs)
            real_read = handle.read

            def read_spy(size=-1):
                data = real_read(size)
                read.append(len(data))
                return data

            handle.read = read_spy
            return handle

        monkeypatch.setattr(type(log), "open", counting_open)

        assert tail_lines(log, 2) == ["old line", "the end"]
        assert sum(read) < 64 * 1024

    def test_the_command_prints_the_tail(self, tmp_config):
        log = paths.track_log_file()
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("".join(f"run {i}\n" for i in range(300)))

        result = CliRunner().invoke(cli, ["track", "log", "-n", "2"])

        assert result.stdout.splitlines() == ["run 298", "run 299"]

    def test_the_command_escapes_terminal_controls(self, tmp_config):
        log = paths.track_log_file()
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_bytes(b"ok\n\x1b[2Jevil\n")

        result = CliRunner().invoke(cli, ["track", "log"])

        assert "\x1b" not in result.stdout and "\\x1b[2Jevil" in result.stdout
