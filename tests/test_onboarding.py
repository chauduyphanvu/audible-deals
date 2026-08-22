"""Regression coverage for the state-aware CLI starting experience."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import click
from click.testing import CliRunner

from audible_deals.auth_state import inspect_auth_file
from audible_deals.cli import cli


def _auth_data(expires=None):
    data = {
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "locale_code": "us",
    }
    if expires is not None:
        data["expires"] = expires
    return data


def test_auth_inspection_states(tmp_path):
    auth_file = tmp_path / "auth.json"
    assert inspect_auth_file(auth_file, now=100).status == "missing"

    auth_file.write_text("not json")
    assert inspect_auth_file(auth_file, now=100).status == "malformed"

    auth_file.write_text(json.dumps({}))
    inspection = inspect_auth_file(auth_file, now=100)
    assert inspection.status == "malformed"
    assert "access_token" in inspection.error

    auth_file.write_text(json.dumps(_auth_data(99)))
    expired = inspect_auth_file(auth_file, now=100)
    assert expired.status == "expired"
    assert expired.is_usable is True

    auth_file.write_text(json.dumps(_auth_data(100 + 3600)))
    assert inspect_auth_file(auth_file, now=100).status == "expiring"

    auth_file.write_text(json.dumps(_auth_data("not-a-number")))
    assert inspect_auth_file(auth_file, now=100).status == "unknown_expiry"

    auth_file.write_text(json.dumps(_auth_data(100 + 86400)))
    assert inspect_auth_file(auth_file, now=100).status == "valid"

    auth_file.write_text(json.dumps({**_auth_data(), "locale_code": "invalid"}))
    assert inspect_auth_file(auth_file, now=100).status == "malformed"


def test_doctor_reuses_auth_inspection_for_malformed_auth(tmp_config, monkeypatch):
    import audible_deals.constants as constants

    auth_file = tmp_config / "auth.json"
    auth_file.write_text("[]")
    monkeypatch.setattr(constants, "AUTH_FILE", auth_file)
    result = CliRunner().invoke(cli, ["doctor"])
    assert result.exit_code == 1
    assert "Auth file parseable" in result.output
    assert "Marketplace reachable" in result.output
    assert "Skipped" in result.output

    auth_file.write_text(json.dumps({"expires": time.time() + 86400}))
    result = CliRunner().invoke(cli, ["doctor"])
    assert result.exit_code == 1
    assert "access_token" in result.output
    assert "Skipped" in result.output


def test_doctor_expired_auth_probes_and_reports_refresh_failure(
    tmp_config, mock_client, monkeypatch
):
    import audible_deals.constants as constants

    auth_file = tmp_config / "auth.json"
    auth_file.write_text(json.dumps(_auth_data(time.time() - 1)))
    monkeypatch.setattr(constants, "AUTH_FILE", auth_file)
    mock_client.check_connection.side_effect = RuntimeError("refresh rejected")

    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 1
    assert "Auth token expiry" in result.output
    assert "WARN" in result.output
    assert "Marketplace reachable" in result.output
    assert "refresh rejected" in result.output
    mock_client.check_connection.assert_called_once_with()


def test_doctor_reports_invalid_profile_and_monitor_settings(tmp_config, monkeypatch):
    import audible_deals.constants as constants

    monkeypatch.setattr(constants, "MONITORS_FILE", tmp_config / "monitors.json")
    monkeypatch.setattr(
        constants, "MONITOR_STATE_FILE", tmp_config / "monitor_state.json"
    )
    constants.PROFILES_FILE.write_text('{"bad-profile": {"min_rating": 6}}')
    constants.MONITORS_FILE.write_text(
        '{"bad-monitor": {"mode": "find", "settings": {"min_hours": Infinity}}}'
    )

    result = CliRunner().invoke(cli, ["doctor"])
    output = " ".join(result.output.split())

    assert result.exit_code == 1
    assert "Profile settings valid" in output
    assert "bad-profile" in output
    assert "expected at most 5" in output
    assert "Saved-search monitors" in output
    assert "bad-monitor" in output
    assert "must be finite" in output


def test_bare_dashboard_is_auth_aware_and_local(tmp_config, monkeypatch):
    import audible_deals.constants as constants

    auth_file = tmp_config / "auth.json"
    monkeypatch.setattr(constants, "AUTH_FILE", auth_file)
    result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0
    assert "Authentication is not set up" in result.output
    assert "deals import-auth PATH" in result.output
    assert not auth_file.exists()

    auth_file.write_text(json.dumps({"expires": time.time() + 86400 * 2}))
    result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0
    assert "Saved authentication cannot be read" in result.output
    assert "Authentication is available" not in result.output
    auth_file.unlink()

    log_file = tmp_config / "bare-invocation.log"
    result = CliRunner().invoke(cli, [], env={"DEALS_LOG_FILE": str(log_file)})
    assert result.exit_code == 0
    assert not log_file.exists()

    auth_file.write_text(json.dumps(_auth_data(time.time() + 86400 * 2)))
    (tmp_config / "config.json").write_text(json.dumps({"locale": "uk"}))
    result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0
    assert "marketplace: uk" in result.output
    assert "Authentication is available" in result.output
    assert "deals for-me" in result.output
    assert "deals wishlist" in result.output
    assert "deals track" in result.output

    result = CliRunner().invoke(cli, ["--locale", "us"])
    assert result.exit_code == 0
    assert "marketplace: us" in result.output

    auth_file.write_text(json.dumps(_auth_data(time.time() - 1)))
    result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0
    assert "will refresh automatically" in result.output
    assert "Start with: deals login" not in result.output


def test_top_level_help_uses_workflow_groups(tmp_config):
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    for heading in (
        "Discover:",
        "Library & Results:",
        "Watch & Automate:",
        "Setup & Support:",
    ):
        assert heading in result.output
    assert result.output.index("Discover:") < result.output.index("Library & Results:")
    assert result.output.index("Library & Results:") < result.output.index(
        "Watch & Automate:"
    )
    assert result.output.index("Watch & Automate:") < result.output.index(
        "Setup & Support:"
    )
    help_text = " ".join(result.output.split())
    assert "Find deals: browse the catalog filtered by price and genre." in help_text
    assert "Manage saved-search monitors run by deals track run." in help_text
    assert (
        "Diagnostic checks for auth, config, and marketplace reachability." in help_text
    )
    assert "..." not in result.output.split("Discover:", 1)[1]


def test_for_me_is_visible_and_for_you_is_hidden_from_completion(tmp_config):
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "for-me" in result.output
    assert "for-you" not in result.output

    completions = cli.shell_complete(click.Context(cli), "for-")
    assert [item.value for item in completions] == ["for-me"]


def test_bare_groups_delegate_to_safe_status_commands(tmp_config):
    runner = CliRunner()
    expected = {
        "wishlist": "Wishlist is empty",
        "profile": "No profiles saved",
        "config": "No global defaults set",
        "monitor": "No monitors",
        "track": "Not installed",
    }
    for command, text in expected.items():
        result = runner.invoke(cli, [command])
        assert result.exit_code == 0, result.output
        assert text in result.output


def test_browser_login_is_default_and_pastes_callback(
    tmp_config, mock_client, monkeypatch
):
    import audible_deals.cli.misc as misc

    mock_client.auth_file = tmp_config / "auth.json"
    mock_client.login_external.side_effect = lambda **kwargs: kwargs[
        "login_url_callback"
    ]("https://auth.example/login")
    launch = MagicMock(return_value=0)
    monkeypatch.setattr(misc.click, "launch", launch)

    result = CliRunner().invoke(
        cli,
        ["login"],
        input="https://callback.example/?openid.oa2.authorization_code=x\n",
    )
    assert result.exit_code == 0, result.output
    assert "Page not found" in result.output
    assert "Could not open a browser" not in result.output
    launch.assert_called_once_with("https://auth.example/login")
    mock_client.login_external.assert_called_once()
    mock_client.login.assert_not_called()


def test_login_help_keeps_remote_command_on_one_line(tmp_config):
    result = CliRunner().invoke(cli, ["login", "--help"])
    assert result.exit_code == 0
    assert "deals login --no-open --via-file /tmp/url.txt" in result.output


def test_browser_login_reports_nonzero_browser_exit(
    tmp_config, mock_client, monkeypatch
):
    import audible_deals.cli.misc as misc

    mock_client.auth_file = tmp_config / "auth.json"
    mock_client.login_external.side_effect = lambda **kwargs: kwargs[
        "login_url_callback"
    ]("https://auth.example/login")
    monkeypatch.setattr(misc.click, "launch", MagicMock(return_value=1))

    result = CliRunner().invoke(
        cli,
        ["login"],
        input="https://callback.example/?openid.oa2.authorization_code=x\n",
    )
    assert result.exit_code == 0, result.output
    assert "Could not open a browser; use the URL above." in result.output


def test_login_credentials_and_incompatible_browser_options(tmp_config, mock_client):
    mock_client.auth_file = tmp_config / "auth.json"
    result = CliRunner().invoke(
        cli, ["login", "--credentials"], input="person@example.com\npassword\n"
    )
    assert result.exit_code == 0, result.output
    mock_client.login.assert_called_once_with("person@example.com", "password")

    result = CliRunner().invoke(cli, ["login", "--credentials", "--no-open"])
    assert result.exit_code == 2
    assert "only available with browser sign-in" in result.output


def test_browser_login_via_file_and_no_open(tmp_config, mock_client, monkeypatch):
    import audible_deals.cli.misc as misc

    callback_file = tmp_config / "callback.txt"
    callback_file.write_text(
        "https://callback.example/?openid.oa2.authorization_code=x"
    )
    callback_file.chmod(0o600)
    mock_client.auth_file = tmp_config / "auth.json"
    mock_client.login_external.side_effect = lambda **kwargs: kwargs[
        "login_url_callback"
    ]("https://auth.example/login")
    launch = MagicMock()
    monkeypatch.setattr(misc.click, "launch", launch)

    result = CliRunner().invoke(
        cli,
        ["login", "--no-open", "--via-file", str(callback_file)],
        input="\n",
    )
    assert result.exit_code == 0, result.output
    assert "Save the full callback URL" in result.output
    assert "delete it after this command finishes" in result.output
    launch.assert_not_called()


def test_browser_login_rejects_invalid_callback_urls(
    tmp_config, mock_client, monkeypatch
):
    import audible_deals.cli.misc as misc

    mock_client.auth_file = tmp_config / "auth.json"
    mock_client.login_external.side_effect = lambda **kwargs: kwargs[
        "login_url_callback"
    ]("https://auth.example/login")
    monkeypatch.setattr(misc.click, "launch", MagicMock(return_value=0))

    result = CliRunner().invoke(cli, ["login"], input="not-a-url\n")
    assert result.exit_code == 1
    assert "complete HTTPS URL" in result.output

    result = CliRunner().invoke(cli, ["login"], input="https://callback.example/\n")
    assert result.exit_code == 1
    assert "does not contain an Audible authorization code" in result.output


def test_browser_login_rejects_non_utf8_callback_file(
    tmp_config, mock_client, monkeypatch
):
    import audible_deals.cli.misc as misc

    callback_file = tmp_config / "callback.txt"
    callback_file.write_bytes(b"\xff")
    callback_file.chmod(0o600)
    mock_client.auth_file = tmp_config / "auth.json"
    mock_client.login_external.side_effect = lambda **kwargs: kwargs[
        "login_url_callback"
    ]("https://auth.example/login")
    launch = MagicMock()
    monkeypatch.setattr(misc.click, "launch", launch)

    result = CliRunner().invoke(
        cli,
        ["login", "--no-open", "--via-file", str(callback_file)],
        input="\n",
    )
    assert result.exit_code == 1
    assert "expected UTF-8 text" in result.output
    launch.assert_not_called()
