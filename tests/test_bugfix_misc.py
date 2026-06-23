"""Regression tests for bugs in audible_deals.cli.misc."""

from __future__ import annotations

import json
import subprocess as sp

from click.testing import CliRunner

import audible_deals.constants as constants_mod
from audible_deals.cli import cli
from audible_deals.cli.misc import _track_checks


# ---------------------------------------------------------------------------
# Bug 13: completions fallback (python -m) must not echo CLI help text
# ---------------------------------------------------------------------------


class TestCompletionsHelpTextLeak:
    def _fake_help_run(self, *args, **kwargs):
        """Mimic the python -m fallback emitting plain help text on stdout."""
        return sp.CompletedProcess(
            args[0],
            0,
            stdout=(
                "Usage: python -m audible_deals [OPTIONS] COMMAND [ARGS]...\n\n"
                "Options:\n  --help  Show this message and exit.\n"
            ),
            stderr="",
        )

    def test_fallback_does_not_echo_help_text(self, monkeypatch):
        """When the spawned process returns help text, completions must fail
        rather than echoing the help banner into the shell config."""
        monkeypatch.setattr("subprocess.run", self._fake_help_run)
        monkeypatch.setattr("shutil.which", lambda _: None)

        result = CliRunner().invoke(cli, ["completions", "bash"])

        assert result.exit_code != 0
        assert "Usage:" not in result.output

    def test_fallback_uses_fixed_prog_name(self, monkeypatch):
        """The fallback must spawn with a stable prog_name so Click derives the
        _DEALS_COMPLETE var, not the python -m form."""
        calls = []

        def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return sp.CompletedProcess(
                args[0], 0, stdout="_deals_completion() { :; }\n", stderr=""
            )

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("shutil.which", lambda _: None)

        result = CliRunner().invoke(cli, ["completions", "bash"])

        assert result.exit_code == 0
        cmd = calls[0][0][0]
        assert "-m" not in cmd
        joined = " ".join(cmd)
        assert "prog_name='deals'" in joined

    def test_valid_completion_script_is_echoed(self, monkeypatch):
        """A genuine completion script (no Usage banner) is still emitted."""

        def fake_run(*args, **kwargs):
            return sp.CompletedProcess(
                args[0], 0, stdout="_deals_completion() { :; }\n", stderr=""
            )

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("shutil.which", lambda _: None)

        result = CliRunner().invoke(cli, ["completions", "bash"])

        assert result.exit_code == 0
        assert "_deals_completion" in result.output


# ---------------------------------------------------------------------------
# Bug 14: completions must not exit 0 when generation fails / is empty
# ---------------------------------------------------------------------------


class TestCompletionsExitCode:
    def test_nonzero_subprocess_surfaces_failure(self, monkeypatch):
        def fake_run(*args, **kwargs):
            return sp.CompletedProcess(args[0], 1, stdout="", stderr="boom\n")

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/deals")

        result = CliRunner().invoke(cli, ["completions", "bash"])

        assert result.exit_code != 0
        assert "boom" in result.output

    def test_empty_output_surfaces_failure(self, monkeypatch):
        def fake_run(*args, **kwargs):
            return sp.CompletedProcess(args[0], 0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/deals")

        result = CliRunner().invoke(cli, ["completions", "bash"])

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Bug 15: doctor / _track_checks must not crash on malformed run_history
# ---------------------------------------------------------------------------


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
