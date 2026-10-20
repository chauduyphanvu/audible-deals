"""Every documented exit status is produced by a real command path."""

from __future__ import annotations

import json
from unittest import mock

import httpx
import pytest
from audible import exceptions as audible_exceptions
from click.testing import CliRunner

from audible_deals.cli import cli
from audible_deals.exit_codes import (
    AuthRequiredError,
    ExitCode,
    exit_code_for,
    is_auth_failure,
)
from tests.conftest import make_raw


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", "https://example.test"))


def _catalog(api, *, products=(), error=None):
    def get(endpoint, **params):
        if error is not None:
            raise error
        return {"products": list(products), "total_results": len(products)}

    api.get_mock.side_effect = get


def _cheap(asin="B0CHEAP001"):
    return make_raw(
        asin,
        price={"lowest_price": {"base": 2.0}, "list_price": {"base": 20.0}},
        runtime_length_min=600,
        language="english",
        series=[],
    )


def run(*args):
    return CliRunner().invoke(cli, list(args))


# Each row: (expected status, how it is produced, what it runs).
# The setup functions get the pytest `api` fixture (a mocked Audible client).


def ok_find(api, *extra):
    _catalog(api, products=[_cheap()])
    return run("find", "--max-price", "5", "--pages", "1", "--exit-code", *extra)


def ok_config_list(api, *extra):
    return run("config", "list", *extra)


def error_unknown_profile(api, *extra):
    return run("profile", "show", "does-not-exist", *extra)


def error_unexpected_bug(api, *extra):
    with mock.patch(
        "audible_deals.cli.catalog.execute_catalog_scan",
        side_effect=ZeroDivisionError("boom"),
    ):
        return run("find", "--max-price", "5", "--pages", "1", *extra)


def usage_bad_option_value(api, *extra):
    return run("find", "--max-price", "not-a-number", *extra)


def usage_unknown_option(api, *extra):
    return run("find", "--no-such-option", *extra)


def auth_no_saved_login(api, *extra):
    (api.tmp_path / "auth.json").unlink()
    return run("find", "--max-price", "5", "--pages", "1", *extra)


def auth_login_rejected(api, *extra):
    _catalog(
        api,
        error=audible_exceptions.Unauthorized(_response(401), {"message": "expired"}),
    )
    return run("find", "--max-price", "5", "--pages", "1", *extra)


def request_failed(api, *extra):
    _catalog(
        api,
        error=audible_exceptions.BadRequest(_response(400), {"message": "bad"}),
    )
    return run("find", "--max-price", "5", "--pages", "1", *extra)


def no_match_find(api, *extra):
    _catalog(api, products=[])
    return run("find", "--max-price", "5", "--pages", "1", "--exit-code", *extra)


def no_match_search(api, *extra):
    _catalog(api, products=[])
    return run("search", "nothing", "--pages", "1", "--exit-code", *extra)


def no_match_last(api, *extra):
    _catalog(api, products=[_cheap()])
    assert run("find", "--max-price", "5", "--pages", "1").exit_code == 0
    return run("last", "--max-price", "0.5", "--exit-code", *extra)


def no_match_watch(api, *extra):
    (api.tmp_path / "wishlist.json").write_text(
        json.dumps(
            [{"asin": "B0WATCH001", "title": "T", "max_price": 1.0, "added": ""}]
        )
    )
    _catalog(api, products=[_cheap("B0WATCH001")])
    return run("watch", "--exit-code", *extra)


def no_match_notify(api, *extra):
    (api.tmp_path / "wishlist.json").write_text(
        json.dumps(
            [{"asin": "B0WATCH001", "title": "T", "max_price": 1.0, "added": ""}]
        )
    )
    _catalog(api, products=[_cheap("B0WATCH001")])
    return run("notify", "--exit-code", *extra)


def interrupted_scan(api, *extra):
    _catalog(api, error=KeyboardInterrupt())
    return run("find", "--max-price", "5", "--pages", "3", *extra)


def interrupted_other_command(api, *extra):
    api.get_mock.side_effect = KeyboardInterrupt
    return run("library", "--json", *extra)


ROWS = [
    (ExitCode.OK, "a scan with a match", ok_find),
    (ExitCode.OK, "config list", ok_config_list),
    (ExitCode.ERROR, "an unknown profile", error_unknown_profile),
    (ExitCode.ERROR, "an unexpected bug", error_unexpected_bug),
    (ExitCode.USAGE, "a bad option value", usage_bad_option_value),
    (ExitCode.USAGE, "an unknown option", usage_unknown_option),
    (ExitCode.AUTH, "no saved login", auth_no_saved_login),
    (ExitCode.AUTH, "a rejected login (HTTP 401)", auth_login_rejected),
    (ExitCode.REQUEST, "a failed request", request_failed),
    (ExitCode.NO_MATCH, "find --exit-code, nothing matched", no_match_find),
    (ExitCode.NO_MATCH, "search --exit-code, nothing matched", no_match_search),
    (ExitCode.NO_MATCH, "last --exit-code, nothing matched", no_match_last),
    (ExitCode.NO_MATCH, "watch --exit-code, no item at target", no_match_watch),
    (ExitCode.NO_MATCH, "notify --exit-code, no item at target", no_match_notify),
    (ExitCode.INTERRUPTED, "Ctrl-C during a scan", interrupted_scan),
    (ExitCode.INTERRUPTED, "Ctrl-C elsewhere", interrupted_other_command),
]


@pytest.mark.parametrize(
    ("code", "why", "scenario"), ROWS, ids=[f"{r[0].value}-{r[1]}" for r in ROWS]
)
def test_each_documented_status_is_produced_by_a_real_command(code, why, scenario, api):
    result = scenario(api)

    assert result.exit_code == code, (why, result.output)


def test_every_documented_status_has_a_row():
    assert {row[0] for row in ROWS} == set(ExitCode)


class TestOneLineErrors:
    def test_a_bug_is_one_line_without_a_traceback(self, api):
        result = error_unexpected_bug(api)

        assert result.output.strip().splitlines()[-1].startswith("Error: Unexpected")
        assert "ZeroDivisionError: boom" in result.output
        assert "-vv" in result.output
        assert "Traceback" not in result.output

    def test_vv_keeps_the_traceback(self, api):
        with mock.patch(
            "audible_deals.cli.catalog.execute_catalog_scan",
            side_effect=ZeroDivisionError("boom"),
        ):
            result = run("-vv", "find", "--max-price", "5", "--pages", "1")

        assert isinstance(result.exception, ZeroDivisionError)

    def test_auth_message_is_actionable(self, api):
        result = auth_no_saved_login(api)

        assert "Not authenticated. Run 'deals login' first." in result.output

    def test_a_rejected_login_says_to_sign_in_again(self, api):
        result = auth_login_rejected(api)

        assert "deals login" in result.output
        assert "Traceback" not in result.output


class TestJsonErrors:
    @pytest.mark.parametrize(
        ("scenario", "code"),
        [
            (error_unexpected_bug, 1),
            (usage_bad_option_value, 2),
            (auth_no_saved_login, 3),
            (request_failed, 4),
            (interrupted_scan, 130),
        ],
    )
    def test_with_json_the_error_is_one_json_object_on_stderr(
        self, api, scenario, code
    ):
        result = scenario(api, "--json")

        assert result.exit_code == code
        error = json.loads(result.stderr.strip().splitlines()[-1])["error"]
        assert error["code"] == code
        assert isinstance(error["message"], str) and error["message"]
        assert not result.stderr.startswith("Error:")
        # Interrupted scans still print what they gathered (a JSON array);
        # every other failure leaves stdout empty.
        if code == 130:
            assert isinstance(json.loads(result.stdout), list)
        else:
            assert result.stdout == ""


class TestClassification:
    def test_typed_errors_and_sdk_errors(self):
        assert exit_code_for(AuthRequiredError("x")) is ExitCode.AUTH
        assert (
            exit_code_for(audible_exceptions.Unauthorized(_response(401), {}))
            is ExitCode.AUTH
        )
        assert exit_code_for(audible_exceptions.NoRefreshToken("x")) is ExitCode.AUTH
        assert (
            exit_code_for(audible_exceptions.BadRequest(_response(400), {}))
            is ExitCode.REQUEST
        )
        assert exit_code_for(audible_exceptions.NetworkError()) is ExitCode.REQUEST

    def test_any_http_401_or_403_is_an_authentication_failure(self):
        class Rejected(Exception):
            status_code = 403

        assert is_auth_failure(Rejected("no"))

    def test_text_alone_no_longer_marks_an_authentication_failure(self):
        # The old check matched "Not authenticated" in the message.
        assert (
            exit_code_for(RuntimeError("Not authenticated. Run 'deals login'")) is None
        )
        assert exit_code_for(ValueError("whatever")) is None


def test_no_command_passes_a_bare_number_to_exit():
    import re
    from pathlib import Path

    import audible_deals

    root = Path(audible_deals.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            if re.search(r"\bexit\(\s*-?\d", line) or re.search(
                r"\b(sys\.exit|SystemExit)\(", line
            ):
                offenders.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
    assert offenders == [], "use ExitCode members:\n" + "\n".join(offenders)
