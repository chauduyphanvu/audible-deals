"""Which Audible store the CLI queries."""

from __future__ import annotations

import json
import time

import pytest
from click.testing import CliRunner

from audible_deals import constants
from audible_deals.auth_state import inspect_auth_file
from audible_deals.cli import cli


def _write_auth(locale_code: str) -> None:
    constants.AUTH_FILE.write_text(
        json.dumps(
            {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "locale_code": locale_code,
                "expires": time.time() + 86400 * 2,
            }
        )
    )


def test_inspection_reports_the_login_marketplace(tmp_config):
    _write_auth("uk")
    assert inspect_auth_file().locale == "uk"


def test_default_marketplace_follows_the_login(tmp_config):
    _write_auth("uk")
    result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0, result.output
    assert "marketplace: uk" in result.output


@pytest.mark.parametrize(
    ("args", "config", "expected"),
    [
        (["--locale", "de"], {}, "de"),
        ([], {"locale": "ca"}, "ca"),
    ],
)
def test_chosen_marketplace_beats_the_login(tmp_config, args, config, expected):
    _write_auth("uk")
    if config:
        (tmp_config / "config.json").write_text(json.dumps(config))
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert f"marketplace: {expected}" in result.output


def test_without_a_login_the_default_stays_us(tmp_config):
    result = CliRunner().invoke(cli, [])
    assert "marketplace: us" in result.output


def test_transport_queries_the_selected_marketplace(monkeypatch, tmp_path):
    import audible

    from audible_deals.audible_transport import AudibleTransport
    from audible_deals.auth_store import AuthStore

    created = {}

    class FakeClient:
        def __init__(self, auth, country_code=None):
            created["country_code"] = country_code

        def get(self, endpoint, **params):
            return {"products": []}

    store = AuthStore(tmp_path / "auth.json", "uk")
    monkeypatch.setattr(store, "load_authenticator", lambda: object())
    monkeypatch.setattr(audible, "Client", FakeClient)
    AudibleTransport(store).request("1.0/catalog/products")
    assert created["country_code"] == "uk"


@pytest.mark.parametrize(
    "args",
    [
        ["--locale", "uk", "library"],
        ["--locale", "uk", "for-me"],
        ["--locale", "uk", "series"],
        ["--locale", "uk", "wishlist", "sync"],
    ],
)
def test_empty_account_data_in_another_store_explains_why(
    tmp_config, mock_client, args
):
    _write_auth("us")
    mock_client.get_library_pages.return_value = iter([])
    mock_client.get_library.return_value = []
    mock_client.get_wishlist.return_value = []
    result = CliRunner().invoke(cli, args)
    output = " ".join(result.output.split())
    assert "Your login belongs to the us store" in output
    assert "Use --locale us to see yours." in output


def test_empty_library_in_the_login_store_has_no_store_hint(tmp_config, mock_client):
    _write_auth("us")
    mock_client.get_library_pages.return_value = iter([])
    result = CliRunner().invoke(cli, ["library"])
    assert result.exit_code == 0, result.output
    assert "Your login belongs" not in result.output
