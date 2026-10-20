"""User-facing messages: `Warning: ...` / `Note: ...` on stderr, no logger names."""

from __future__ import annotations

import json
import logging
import sys
from unittest import mock

import pytest
from click.testing import CliRunner

from audible_deals import notices
from audible_deals.cli import cli
from audible_deals.logging_setup import (
    NOTE,
    BriefFormatter,
    UserFormatter,
    configure_logging,
)
from tests.conftest import make_raw


def record(level, message, *args, exc=None, name="audible_deals.storage"):
    exc_info = None
    if exc is not None:
        try:
            raise exc
        except type(exc):
            exc_info = sys.exc_info()
    return logging.LogRecord(name, level, __file__, 1, message, args, exc_info)


class TestFormatters:
    @pytest.mark.parametrize(
        ("level", "text"),
        [
            (logging.WARNING, "Warning: disk is odd"),
            (NOTE, "Note: disk is odd"),
            (logging.ERROR, "Error: disk is odd"),
            (logging.CRITICAL, "Error: disk is odd"),
        ],
    )
    def test_default_format_has_a_label_and_no_logger_name(self, level, text):
        assert UserFormatter().format(record(level, "disk is odd")) == text

    def test_an_exception_adds_one_line_not_a_traceback(self):
        out = UserFormatter().format(
            record(logging.WARNING, "could not save", exc=ValueError("no space"))
        )
        assert out == "Warning: could not save: ValueError: no space"

    def test_dash_v_names_the_logger_but_still_has_no_traceback(self):
        out = BriefFormatter("%(levelname)s %(name)s: %(message)s").format(
            record(logging.WARNING, "could not save", exc=ValueError("no space"))
        )
        assert (
            out == "WARNING audible_deals.storage: could not save: ValueError: no space"
        )

    def test_dash_vv_shows_the_traceback(self, capsys):
        configure_logging(2)
        logging.getLogger("audible_deals.x").warning("boom", exc_info=_raised())
        assert "Traceback (most recent call last)" in capsys.readouterr().err

    def test_default_handler_uses_the_user_format(self, capsys):
        configure_logging(0)
        logging.getLogger("audible_deals.x").warning("careful")
        logging.getLogger("audible_deals.x").log(NOTE, "fyi")
        logging.getLogger("audible_deals.x").info("hidden")
        assert capsys.readouterr().err == "Warning: careful\nNote: fyi\n"

    def test_dash_v_shows_info_with_logger_names(self, capsys):
        configure_logging(1)
        logging.getLogger("audible_deals.x").info("detail")
        assert capsys.readouterr().err == "INFO audible_deals.x: detail\n"


def _raised():
    try:
        raise RuntimeError("inner")
    except RuntimeError:
        return sys.exc_info()


class TestNoticeHelpers:
    def test_warn_and_note_go_to_stderr_with_their_label(self, capsys):
        notices.warn("one")
        notices.note("two")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "Warning: one\nNote: two\n"

    def test_untrusted_text_cannot_inject_terminal_controls(self, capsys):
        notices.warn("title\x1b[2J\nsecond line")
        assert capsys.readouterr().err == "Warning: title\\x1b[2J\\nsecond line\n"


def run(*args):
    return CliRunner().invoke(cli, list(args))


class TestAccumulatedMessages:
    def test_a_corrupt_file_read_warns_in_the_user_format(self, tmp_config):
        (tmp_config / "profiles.json").write_text("{not json")

        result = run("profile", "list")

        lines = [line for line in result.stderr.splitlines() if "corrupt" in line]
        assert len(lines) == 1
        assert lines[0].startswith("Warning: ")
        assert "audible_deals" not in lines[0] and "WARNING" not in lines[0]

    def test_a_corrupt_file_read_names_the_logger_only_with_dash_v(self, tmp_config):
        (tmp_config / "profiles.json").write_text("{not json")

        result = run("-v", "profile", "list")

        assert "WARNING audible_deals.storage:" in result.stderr

    def test_the_migration_notice_is_a_note(self, tmp_config):
        (tmp_config / "seen_asins.json").write_text(json.dumps(["B0SEEN0001"]))

        result = run("cache", "info")

        assert result.exit_code == 0, result.output
        notes = [
            ln for ln in result.stderr.splitlines() if "Moved your saved data" in ln
        ]
        assert len(notes) == 1
        assert notes[0].startswith("Note: Moved your saved data into state.db")
        assert "*.migrated" in notes[0]
        assert "Moved your saved data" not in result.stdout

    def test_a_database_failure_is_one_error_line(self, tmp_config, monkeypatch):
        import sqlite3

        from audible_deals import db

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(db, "state", broken)

        result = run("cache", "info")

        assert result.exit_code == 1
        assert result.stderr.strip() == (
            "Error: Local database error: disk I/O error. "
            "Run 'deals doctor' to check it."
        )


class FakeCatalog:
    """Enough of Audible for find: cheap titles, and the option to interrupt."""

    def __init__(self, api):
        self.interrupt_on_page = None
        api.get_mock.side_effect = self.get

    def get(self, endpoint, **params):
        if endpoint == "1.0/catalog/categories":
            return {"categories": []}
        page = params.get("page", 0)
        if page == self.interrupt_on_page:
            raise KeyboardInterrupt
        return {
            "products": [
                make_raw(
                    f"B0MSG{page}{i:05d}",
                    price={"lowest_price": {"base": 3.0}, "list_price": {"base": 20.0}},
                    runtime_length_min=600,
                    language="english",
                    series=[],
                )
                for i in range(3)
            ],
            "total_results": 200,
        }


class TestScanNotices:
    FIND = ("find", "--max-price", "5", "--pages", "2")

    def test_the_cache_note_is_on_stderr_with_a_label(self, api):
        FakeCatalog(api)
        run(*self.FIND)

        result = run(*self.FIND)

        notes = [ln for ln in result.stderr.splitlines() if ln.startswith("Note: ")]
        assert len(notes) == 1 and "Using cached results" in notes[0]
        assert "Using cached results" not in result.stdout

    def test_the_interruption_is_a_warning_on_stderr(self, api):
        FakeCatalog(api).interrupt_on_page = 1

        result = run(*self.FIND)

        assert result.exit_code == 130
        assert "Warning: Scan interrupted after" in result.stderr
        assert "Scan interrupted" not in result.stdout

    def test_a_failed_history_write_is_one_warning_without_a_traceback(
        self, api, monkeypatch
    ):
        FakeCatalog(api)

        def broken(*args, **kwargs):
            raise OSError("history is read-only")

        monkeypatch.setattr("audible_deals.result_publication.record_prices", broken)

        result = run(*self.FIND)

        assert result.exit_code == 0, result.output
        assert "Warning: could not record price history: history is read-only" in (
            result.stderr
        )
        assert "Traceback" not in result.output

    def test_known_title_reprice_failure_is_a_warning(self, api):
        import click

        FakeCatalog(api)
        run(*self.FIND)  # index the titles of two pages
        original = api.get_mock.side_effect

        def fail_batches(endpoint, **params):
            if params.get("asins"):
                raise click.ClickException("no batch for you")
            return original(endpoint, **params)

        api.get_mock.side_effect = fail_batches
        # One page only: the second page's titles are known but not scanned.
        result = run("find", "--max-price", "5", "--pages", "1", "--refresh")

        assert result.exit_code == 0, result.output
        assert (
            "Warning: Could not re-price titles from earlier scans (ClickException)"
            in result.stderr
        )
        assert "Traceback" not in result.output


class TestMultilineErrors:
    """An error that lists choices on a second line shows two real lines."""

    def test_text_mode_prints_separate_lines(self, api):
        FakeCatalog(api)

        result = run("find", "--genre", "nosuchgenre")

        assert result.exit_code == 1
        lines = result.stderr.splitlines()
        assert lines[0] == 'Error: No genre matching "nosuchgenre".'
        assert lines[1].startswith("Available: ")
        assert "\\n" not in result.stderr

    def test_json_mode_carries_a_real_newline(self, api):
        FakeCatalog(api)

        result = run("find", "--genre", "nosuchgenre", "--json")

        assert result.exit_code == 1
        error = json.loads(result.stderr.strip().splitlines()[-1])["error"]
        first, second = error["message"].split("\n")
        assert first == 'No genre matching "nosuchgenre".'
        assert second.startswith("Available: ")
        assert "\\n" not in error["message"]
        # One physical line on stderr: the break is the JSON escape \n.
        assert len(result.stderr.strip().splitlines()) == 1

    def test_a_usage_error_keeps_its_lines_too(self, tmp_config):
        result = run("find", "--max-price", "5", "--min-price-drop", "oops")

        assert result.exit_code == 2
        assert "\\n" not in result.stderr

    def test_escape_sequences_are_still_neutralized(self, api):
        import click

        FakeCatalog(api)
        with mock.patch(
            "audible_deals.cli.catalog.execute_catalog_scan",
            side_effect=click.ClickException("first\x1b[2J\nsecond\rthird‮"),
        ):
            result = run("find", "--max-price", "5", "--pages", "1")

        assert result.stderr == "Error: first\\x1b[2J\nsecond\\rthird\\u202e\n"

    def test_crlf_counts_as_one_break(self):
        from audible_deals.presentation.terminal import safe_text

        assert safe_text("a\r\nb", keep_newlines=True) == "a\nb"
        assert safe_text("a\r\nb") == "a\\r\\nb"  # values shown inline stay on one line
