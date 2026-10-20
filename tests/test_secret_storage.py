"""Optional OS-keychain storage of the login and webhook secrets.

Every test uses an in-memory keyring backend; nothing touches a real keychain.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time

import audible
import keyring
import keyring.core
import pytest
from click.testing import CliRunner
from keyring.backend import KeyringBackend
from keyring.backends import fail
from keyring.errors import PasswordDeleteError

from audible_deals import paths, secret_store
from audible_deals.auth_state import auth_file_is_reference, inspect_auth_file
from audible_deals.auth_store import AuthStore
from audible_deals.cli import cli
from audible_deals.config_store import (
    load_config,
    load_monitors,
    save_config,
    save_monitors,
)
from audible_deals.exit_codes import AuthRequiredError
from audible_deals.secret_store import SecretStoreError
from tests.conftest import assert_mode, bare_env
from tests.webhook_server import RecordingServer

WEBHOOK = "https://hooks.example.test/services/T000/B000/secret-token-123"
HEADER = "Authorization: Bearer hunter2"


class MemoryKeyring(KeyringBackend):
    """A keychain that lives in a dict."""

    priority = 1  # type: ignore[assignment]

    def __init__(self):
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}
        self.fail_on_set_after: int | None = None
        self._sets = 0

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self._sets += 1
        if self.fail_on_set_after is not None and self._sets > self.fail_on_set_after:
            raise RuntimeError("keychain is locked")
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        try:
            del self.items[(service, username)]
        except KeyError:
            raise PasswordDeleteError("not found") from None

    def accounts(self):
        return sorted(name for (_service, name) in self.items)


@pytest.fixture
def vault(monkeypatch):
    backend = MemoryKeyring()
    monkeypatch.setattr(keyring.core, "_keyring_backend", backend)
    return backend


def auth_dict(access="a", expires=None):
    return {
        "access_token": "Atna|" + access * 900,
        "refresh_token": "Atnr|" + "r" * 600,
        "expires": time.time() + 30 * 86400 if expires is None else expires,
        "locale_code": "us",
        "device_private_key": "-----BEGIN RSA PRIVATE KEY-----\n"
        + "K" * 1700
        + "\n-----END RSA PRIVATE KEY-----\n",
        "website_cookies": {"session-id": "1" * 40, "ubid-main": "2" * 40},
        "store_authentication_cookie": {"cookie": "c" * 700},
        "device_info": {"device_name": "n", "device_serial_number": "s"},
        "customer_info": {"account_pool": "Amazon", "user_id": "u"},
        "with_username": False,
        "activation_bytes": None,
        "adp_token": None,
    }


def run(*args, **kwargs):
    return CliRunner().invoke(cli, list(args), **kwargs)


def data_files_text(directory):
    return "\n".join(
        path.read_text(errors="replace")
        for path in directory.rglob("*")
        if path.is_file()
        and path.suffix in {".json", ""}
        and ".lock" not in path.name
        and path.suffix != ".db"
    )


# -- the store -------------------------------------------------------------------


class TestStore:
    def test_a_small_secret_round_trips_as_one_item(self, vault):
        secret_store.put("webhook", WEBHOOK)

        assert secret_store.get("webhook") == WEBHOOK
        assert vault.accounts() == ["webhook"]

    def test_a_large_secret_is_split_so_windows_will_accept_it(self, vault):
        text = json.dumps(auth_dict(), indent=4)
        assert len(text) > 4000

        secret_store.put("auth", text)

        assert secret_store.get("auth") == text
        assert len(vault.items) > 2
        assert all(
            len(v.encode("utf-16-le")) <= 2000 + 200 for v in vault.items.values()
        )
        # the pieces are named under the account and nothing is left over on removal
        secret_store.remove("auth")
        assert vault.items == {}

    def test_replacing_a_large_secret_leaves_no_old_pieces(self, vault):
        secret_store.put("auth", json.dumps(auth_dict("a"), indent=4))
        first = len(vault.items)

        secret_store.put("auth", json.dumps(auth_dict("b"), indent=4))
        assert len(vault.items) == first
        secret_store.put("auth", "short now")

        assert vault.accounts() == ["auth"]
        assert secret_store.get("auth") == "short now"

    def test_a_missing_piece_is_an_error_not_a_truncated_secret(self, vault):
        secret_store.put("auth", json.dumps(auth_dict(), indent=4))
        piece = next(key for key in vault.items if "#" in key[1])
        del vault.items[piece]

        with pytest.raises(SecretStoreError, match="incomplete or damaged"):
            secret_store.get("auth")

    def test_a_tampered_piece_is_detected(self, vault):
        secret_store.put("auth", json.dumps(auth_dict(), indent=4))
        piece = next(key for key in vault.items if "#" in key[1])
        vault.items[piece] = vault.items[piece][:-1] + "!"

        with pytest.raises(SecretStoreError, match="incomplete or damaged"):
            secret_store.get("auth")

    def test_non_ascii_text_survives_the_split(self, vault):
        text = ("Ünïcödé ✓ " * 400) + "end"

        secret_store.put("webhook", text)

        assert secret_store.get("webhook") == text

    def test_references(self):
        assert secret_store.reference("webhook") == "keyring:webhook"
        assert (
            secret_store.reference_account("keyring:monitor:m:webhook")
            == "monitor:m:webhook"
        )
        assert not secret_store.is_reference("https://example.test/keyring:x")
        assert not secret_store.is_reference(None)

    def test_resolve_passes_plain_values_through_and_opens_references(self, vault):
        secret_store.put("webhook", WEBHOOK)
        secret_store.put("webhook_headers", json.dumps([HEADER]))

        assert (
            secret_store.resolve("https://plain.example/hook")
            == "https://plain.example/hook"
        )
        assert secret_store.resolve([HEADER]) == [HEADER]
        assert secret_store.resolve(None) is None
        assert secret_store.resolve("keyring:webhook") == WEBHOOK
        assert secret_store.resolve(["keyring:webhook_headers"]) == [HEADER]

    def test_a_reference_to_nothing_says_so(self, vault):
        with pytest.raises(SecretStoreError, match="no entry 'webhook'"):
            secret_store.resolve("keyring:webhook")

    def test_protect_does_nothing_in_file_mode(self, vault):
        assert secret_store.protect(WEBHOOK, "webhook", mode="file") == WEBHOOK
        assert vault.items == {}

    def test_protect_moves_the_value_in_keyring_mode(self, vault):
        assert (
            secret_store.protect(WEBHOOK, "webhook", mode="keyring")
            == "keyring:webhook"
        )
        assert secret_store.protect([HEADER], "webhook_headers", mode="keyring") == [
            "keyring:webhook_headers"
        ]
        assert secret_store.get("webhook") == WEBHOOK
        assert json.loads(secret_store.get("webhook_headers")) == [HEADER]
        # protecting a reference again is a no-op
        assert (
            secret_store.protect("keyring:webhook", "webhook", mode="keyring")
            == "keyring:webhook"
        )


class TestWhenThereIsNoKeychain:
    def test_without_the_package_the_error_says_how_to_install_it(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "keyring", None)

        with pytest.raises(SecretStoreError) as error:
            secret_store.put("webhook", "x")

        message = str(error.value)
        assert "keyring package is not installed" in message
        assert "audible-deals[keyring]" in message
        assert "deals config set secret-storage file" in message

    def test_without_a_backend_the_error_says_there_is_no_keychain(self, monkeypatch):
        monkeypatch.setattr(keyring.core, "_keyring_backend", fail.Keyring())

        with pytest.raises(SecretStoreError, match="no usable keychain"):
            secret_store.get("webhook")

    def test_a_backend_that_refuses_is_reported_with_the_cause(self, vault):
        vault.fail_on_set_after = 0

        with pytest.raises(SecretStoreError, match="refused to store a secret.*locked"):
            secret_store.put("webhook", "x")

    def test_available_reports_without_raising(self, vault, monkeypatch):
        assert secret_store.available()[0] is True
        monkeypatch.setattr(keyring.core, "_keyring_backend", fail.Keyring())
        usable, detail = secret_store.available()
        assert usable is False and "no usable keychain" in detail


# -- the login -------------------------------------------------------------------


def use_keyring(tmp_config):
    save_config({"secret_storage": "keyring"})


class TestLoginInTheKeychain:
    def _login(self, monkeypatch, store, data):
        monkeypatch.setattr(
            "audible.Authenticator.from_login",
            lambda *a, **kw: audible.Authenticator.from_dict(dict(data)),
        )
        store.login("user", "pw")

    def test_file_mode_writes_exactly_what_the_sdk_would(self, tmp_path, monkeypatch):
        data = auth_dict()
        sdk_file = tmp_path / "sdk.json"
        audible.Authenticator.from_dict(dict(data)).to_file(sdk_file)
        ours = tmp_path / "ours" / "auth.json"

        self._login(monkeypatch, AuthStore(ours, "us"), data)

        assert ours.read_bytes() == sdk_file.read_bytes()
        assert_mode(ours, 0o600)

    def test_keyring_mode_leaves_only_a_reference_on_disk(
        self, tmp_config, vault, monkeypatch
    ):
        use_keyring(tmp_config)
        data = auth_dict()

        self._login(monkeypatch, AuthStore(paths.auth_file(), "us"), data)

        on_disk = paths.auth_file().read_text()
        assert "Atna|" not in on_disk and "Atnr|" not in on_disk
        reference = json.loads(on_disk)
        assert reference["secret_storage"] == "keyring"
        assert reference["account"] == "auth" and reference["locale_code"] == "us"
        assert reference["expires"] == data["expires"]
        assert_mode(paths.auth_file(), 0o600)
        stored = json.loads(secret_store.get("auth"))
        assert stored["access_token"] == data["access_token"]
        assert auth_file_is_reference()

    def test_the_login_loads_from_the_keychain(self, tmp_config, vault, monkeypatch):
        use_keyring(tmp_config)
        data = auth_dict()
        self._login(monkeypatch, AuthStore(paths.auth_file(), "us"), data)

        auth = AuthStore(paths.auth_file(), "us").load_authenticator()

        assert auth.access_token == data["access_token"]
        assert auth.locale.country_code == "us"

    def test_imported_credentials_go_to_the_keychain_too(
        self, tmp_config, vault, tmp_path
    ):
        use_keyring(tmp_config)
        source = tmp_path / "audible-cli.json"
        source.write_text(json.dumps({"access_token": "at", "refresh_token": "rt"}))

        AuthStore(paths.auth_file(), "us").import_auth(source)

        assert "refresh_token" not in paths.auth_file().read_text()
        assert json.loads(secret_store.get("auth"))["refresh_token"] == "rt"

    def test_the_dashboard_and_doctor_judge_it_without_opening_the_keychain(
        self, tmp_config, vault, monkeypatch
    ):
        use_keyring(tmp_config)
        self._login(monkeypatch, AuthStore(paths.auth_file(), "us"), auth_dict())
        vault.items.clear()  # nothing to read: the reference alone must be enough

        assert inspect_auth_file().status == "valid"

    def test_an_expired_reference_is_reported_as_expired(
        self, tmp_config, vault, monkeypatch
    ):
        use_keyring(tmp_config)
        self._login(
            monkeypatch,
            AuthStore(paths.auth_file(), "us"),
            auth_dict(expires=time.time() - 3600),
        )

        assert inspect_auth_file().status == "expired"

    def test_a_refreshed_token_is_written_back_to_the_keychain(
        self, tmp_config, vault, monkeypatch
    ):
        use_keyring(tmp_config)
        self._login(monkeypatch, AuthStore(paths.auth_file(), "us"), auth_dict("a"))
        store = AuthStore(paths.auth_file(), "us")
        auth = store.load_authenticator()

        auth.access_token = "Atna|refreshed"
        auth.expires = time.time() + 60 * 86400
        store.persist_refreshed_auth()

        assert json.loads(secret_store.get("auth"))["access_token"] == "Atna|refreshed"
        reference = json.loads(paths.auth_file().read_text())
        assert reference["expires"] == pytest.approx(auth.expires)
        assert "Atna|refreshed" not in paths.auth_file().read_text()
        # and the next client sees the refreshed login
        assert AuthStore(paths.auth_file(), "us").load_authenticator().access_token == (
            "Atna|refreshed"
        )

    def test_a_login_changed_by_someone_else_is_not_overwritten(
        self, tmp_config, vault, monkeypatch, caplog
    ):
        use_keyring(tmp_config)
        self._login(monkeypatch, AuthStore(paths.auth_file(), "us"), auth_dict("a"))
        store = AuthStore(paths.auth_file(), "us")
        auth = store.load_authenticator()
        auth.access_token = "Atna|mine"
        secret_store.put(
            "auth", json.dumps(auth_dict("z"), indent=4)
        )  # `deals login` elsewhere

        with caplog.at_level(logging.WARNING):
            store.persist_refreshed_auth()
            store.persist_refreshed_auth()

        assert json.loads(secret_store.get("auth"))["access_token"].startswith(
            "Atna|zzz"
        )
        assert (
            len([r for r in caplog.records if "Not saving refreshed" in r.message]) == 1
        )

    def test_a_login_removed_from_the_keychain_means_sign_in_again(
        self, tmp_config, vault, monkeypatch
    ):
        use_keyring(tmp_config)
        self._login(monkeypatch, AuthStore(paths.auth_file(), "us"), auth_dict())
        vault.items.clear()

        with pytest.raises(AuthRequiredError, match="not in the keychain any more"):
            AuthStore(paths.auth_file(), "us").load_authenticator()

    def test_a_missing_keychain_is_a_clear_one_line_error(
        self, tmp_config, monkeypatch
    ):
        paths.auth_file().write_text(
            json.dumps(
                {
                    "secret_storage": "keyring",
                    "service": "audible-deals",
                    "account": "auth",
                    "locale_code": "us",
                    "expires": time.time() + 86400 * 30,
                }
            )
        )
        monkeypatch.setattr(keyring.core, "_keyring_backend", fail.Keyring())

        result = run("categories")

        assert result.exit_code == 1
        assert result.stderr.startswith("Error: Secret storage is set to 'keyring'")
        assert "no usable keychain" in result.stderr
        assert "deals config set secret-storage file" in result.stderr
        assert "Traceback" not in result.output and "Unexpected" not in result.stderr


# -- webhooks --------------------------------------------------------------------


class TestWebhookSecrets:
    @pytest.fixture(autouse=True)
    def _private_ok(self, tmp_config):
        save_config({"webhook_allow_private": True})

    def test_set_stores_a_reference_in_keyring_mode(self, tmp_config, vault):
        save_config({"webhook_allow_private": True, "secret_storage": "keyring"})

        result = run("config", "set", "webhook", "http://127.0.0.1:9/secret-token-123")

        assert result.exit_code == 0, result.output
        text = paths.config_file().read_text()
        assert "secret-token-123" not in text
        assert load_config()["webhook"] == "keyring:webhook"
        assert secret_store.get("webhook").endswith("secret-token-123")

    def test_set_headers_in_keyring_mode(self, tmp_config, vault):
        save_config({"secret_storage": "keyring"})

        run("config", "set", "webhook-headers", HEADER)

        assert "hunter2" not in paths.config_file().read_text()
        assert load_config()["webhook_headers"] == ["keyring:webhook_headers"]
        assert json.loads(secret_store.get("webhook_headers")) == [HEADER]

    def test_file_mode_is_untouched(self, tmp_config, vault):
        run("config", "set", "webhook", "http://127.0.0.1:9/hook")

        assert load_config()["webhook"] == "http://127.0.0.1:9/hook"
        assert vault.items == {}

    def test_show_secrets_opens_the_reference_and_the_default_does_not(
        self, tmp_config, vault
    ):
        save_config({"secret_storage": "keyring", "webhook_allow_private": True})
        run("config", "set", "webhook", "http://127.0.0.1:9/hook")

        shown = run("config", "get", "webhook", "--show-secrets")
        hidden = run("config", "get", "webhook")
        listed = json.loads(run("config", "list", "--json", "--show-secrets").stdout)

        assert "http://127.0.0.1:9/hook" in shown.stdout
        assert "127.0.0.1" not in hidden.stdout and "<redacted>" in hidden.stdout
        assert listed["config"]["webhook"] == "http://127.0.0.1:9/hook"

    def test_replacing_or_resetting_removes_the_keychain_entry(self, tmp_config, vault):
        save_config({"secret_storage": "keyring", "webhook_allow_private": True})
        run("config", "set", "webhook", "http://127.0.0.1:9/hook")
        assert "webhook" in vault.accounts()

        run("config", "reset", "webhook")

        assert vault.accounts() == []
        assert "webhook" not in load_config()

    def test_going_back_to_file_mode_for_one_setting_frees_the_entry(
        self, tmp_config, vault
    ):
        save_config({"secret_storage": "keyring", "webhook_allow_private": True})
        run("config", "set", "webhook", "http://127.0.0.1:9/hook")
        save_config({**load_config(), "secret_storage": "file"})

        run("config", "set", "webhook", "http://127.0.0.1:9/other")

        assert load_config()["webhook"] == "http://127.0.0.1:9/other"
        assert vault.accounts() == []

    def test_a_track_run_sends_to_the_url_and_headers_from_the_keychain(
        self, tmp_config, vault, mock_client
    ):
        from audible_deals import wishlist as wishlist_mod
        from tests.conftest import make_product

        server = RecordingServer()
        try:
            save_config(
                {
                    "secret_storage": "keyring",
                    "webhook_allow_private": True,
                    "webhook": "keyring:webhook",
                    "webhook_headers": ["keyring:webhook_headers"],
                }
            )
            secret_store.put("webhook", server.url)
            secret_store.put("webhook_headers", json.dumps(["X-Token: hunter2"]))
            wishlist_mod.save_wishlist(
                [{"asin": "B0KEYRING1", "title": "T", "max_price": 5.0, "added": ""}]
            )
            mock_client.get_products_batch.return_value = [
                make_product(asin="B0KEYRING1", price=3.0)
            ]

            result = run("track", "run")
            (request,) = server.requests
        finally:
            server.close()

        assert result.exit_code == 0, result.output
        assert request["headers"]["X-Token"] == "hunter2"
        assert json.loads(request["body"])["count"] == 1
        assert "hunter2" not in paths.config_file().read_text()

    def test_a_keychain_failure_in_a_track_run_is_recorded_in_its_history(
        self, tmp_config, vault, mock_client
    ):
        save_config(
            {
                "secret_storage": "keyring",
                "webhook_allow_private": True,
                "webhook": "keyring:webhook",  # the keychain has no such entry
            }
        )

        result = run("track", "run")
        status = json.loads(run("track", "status", "--json", "--history").stdout)

        assert result.exit_code == 1
        assert "keychain has no entry" in result.stderr
        assert status["last_run"]["error"].startswith("SecretStoreError: ")
        assert "keychain has no entry" in status["last_run"]["error"]
        assert len(status["history"]) == 1

    def test_a_monitor_webhook_is_a_reference_and_is_cleaned_up(
        self, tmp_config, vault
    ):
        save_config({"secret_storage": "keyring", "webhook_allow_private": True})

        added = run(
            "monitor",
            "add",
            "cheap",
            "--query",
            "sanderson",
            "--webhook",
            "http://127.0.0.1:9/monitor-secret",
        )

        assert added.exit_code == 0, added.output
        assert load_monitors()["cheap"]["webhook"] == "keyring:monitor:cheap:webhook"
        assert "monitor-secret" not in paths.monitors_file().read_text()
        listed = json.loads(run("monitor", "list", "--json").stdout)
        assert listed["monitors"][0]["has_webhook"] is True

        run("monitor", "remove", "cheap", "-y")

        assert vault.accounts() == []

    def test_a_rejected_monitor_add_never_touches_an_existing_monitors_secret(
        self, tmp_config, vault
    ):
        save_config({"secret_storage": "keyring", "webhook_allow_private": True})
        first = run(
            "monitor",
            "add",
            "cheap",
            "--query",
            "a",
            "--webhook",
            "http://127.0.0.1:9/first",
        )
        assert first.exit_code == 0, first.output

        again = run(
            "monitor",
            "add",
            "cheap",
            "--query",
            "b",
            "--webhook",
            "http://127.0.0.1:9/second",
        )

        assert again.exit_code == 1
        assert "already exists" in again.stderr
        assert secret_store.get("monitor:cheap:webhook") == "http://127.0.0.1:9/first"
        assert vault.accounts() == ["monitor:cheap:webhook"]

    def test_an_over_budget_monitor_add_writes_no_secret(
        self, tmp_config, vault, monkeypatch
    ):
        import audible_deals.cli.monitor as cli_monitor

        save_config({"secret_storage": "keyring", "webhook_allow_private": True})
        monkeypatch.setattr(cli_monitor, "MAX_MONITOR_API_CALLS_PER_RUN", 0)

        result = run(
            "monitor",
            "add",
            "big",
            "--query",
            "a",
            "--webhook",
            "http://127.0.0.1:9/hook",
        )

        assert result.exit_code == 2
        assert vault.accounts() == []
        assert load_monitors() == {}

    def test_a_busy_run_lock_writes_no_secret(self, tmp_config, vault, monkeypatch):
        from audible_deals import monitor_service
        from audible_deals.locking import LockHeldError

        save_config({"secret_storage": "keyring", "webhook_allow_private": True})

        def busy():
            raise LockHeldError("held")

        monkeypatch.setattr(monitor_service, "run_lock", busy)

        result = run(
            "monitor",
            "add",
            "x",
            "--query",
            "a",
            "--webhook",
            "http://127.0.0.1:9/hook",
        )

        assert result.exit_code == 1
        assert "Another run is in progress" in result.stderr
        assert vault.accounts() == []

    def test_a_failed_save_takes_the_new_secret_back_out_of_the_keychain(
        self, tmp_config, vault, monkeypatch
    ):
        from audible_deals import monitor_service

        save_config({"secret_storage": "keyring", "webhook_allow_private": True})

        def broken(monitors):
            raise OSError("disk full")

        monkeypatch.setattr(monitor_service, "save_monitors", broken)

        result = run(
            "monitor",
            "add",
            "x",
            "--query",
            "a",
            "--webhook",
            "http://127.0.0.1:9/hook",
        )

        assert result.exit_code == 1
        assert vault.accounts() == []
        assert load_monitors() == {}

    def test_monitor_alerts_are_sent_to_the_resolved_url(self, tmp_config, vault):
        from audible_deals.automation_models import MonitorDefinition, MonitorEvent
        from audible_deals.notification_service import deliver_monitor_events
        from audible_deals.webhook_client import WebhookClient

        secret_store.put("monitor:cheap:webhook", "http://127.0.0.1:9/real")
        posts = []

        class Client(WebhookClient):
            def post(self, url, body, headers):
                posts.append(url)

        event = MonitorEvent("new", "cheap", "B0X", "T", 3.0, None, "https://x", None)

        deliver_monitor_events(
            (event,),
            MonitorDefinition(name="cheap", webhook="keyring:monitor:cheap:webhook"),
            None,
            "generic",
            {},
            Client(),
        )

        assert posts == ["http://127.0.0.1:9/real"]


# -- switching -------------------------------------------------------------------


def seed_everything(tmp_config):
    """A file-mode setup with a login, a webhook, headers and a monitor webhook."""
    auth_text = json.dumps(auth_dict(), indent=4)
    paths.auth_file().write_text(auth_text)
    save_config(
        {
            "max_price": 5.0,
            "webhook_allow_private": True,
            "webhook": "http://127.0.0.1:9/global-secret",
            "webhook_headers": [HEADER],
        }
    )
    save_monitors(
        {
            "cheap": {
                "version": 1,
                "name": "cheap",
                "enabled": True,
                "locale": "us",
                "mode": "search",
                "query": "x",
                "settings": {"pages": 1, "limit": None},
                "webhook": "http://127.0.0.1:9/monitor-secret",
            },
            "plain": {
                "version": 1,
                "name": "plain",
                "enabled": True,
                "locale": "us",
                "mode": "search",
                "query": "y",
                "settings": {"pages": 1, "limit": None},
            },
        }
    )
    return auth_text


class TestSwitching:
    def test_to_the_keychain_leaves_no_secret_in_any_file(self, tmp_config, vault):
        seed_everything(tmp_config)

        result = run("config", "set", "secret-storage", "keyring", "-y")

        assert result.exit_code == 0, result.output
        text = data_files_text(tmp_config)
        assert "keyring:webhook" in text  # the scan really reads config.json
        for secret in ("Atna|", "Atnr|", "global-secret", "monitor-secret", "hunter2"):
            assert secret not in text
        config = load_config()
        assert config["secret_storage"] == "keyring"
        assert config["webhook"] == "keyring:webhook"
        assert config["webhook_headers"] == ["keyring:webhook_headers"]
        assert config["max_price"] == 5.0  # everything else is untouched
        assert load_monitors()["cheap"]["webhook"] == "keyring:monitor:cheap:webhook"
        assert "webhook" not in load_monitors()["plain"]
        top_level = {name for name in vault.accounts() if "#" not in name}
        assert top_level == {
            "auth",
            "webhook",
            "webhook_headers",
            "monitor:cheap:webhook",
        }
        assert auth_file_is_reference()
        assert "moved: login" in result.stdout

    def test_and_back_restores_the_files_exactly(self, tmp_config, vault):
        original_auth = seed_everything(tmp_config)
        original_config = load_config()
        original_monitors = load_monitors()
        run("config", "set", "secret-storage", "keyring", "-y")

        result = run("config", "set", "secret-storage", "file", "-y")

        assert result.exit_code == 0, result.output
        assert paths.auth_file().read_text() == original_auth
        assert_mode(paths.auth_file(), 0o600)
        restored = load_config()
        assert restored.pop("secret_storage") == "file"
        assert restored == original_config
        assert load_monitors() == original_monitors
        assert vault.items == {}  # nothing is left behind in the keychain

    def test_back_to_files_removes_the_entries_the_references_name(
        self, tmp_config, vault
    ):
        """Hand-renamed references: the keychain entry to delete is the one named."""
        save_config(
            {
                "secret_storage": "keyring",
                "webhook": "keyring:my-hook",
                "webhook_headers": ["keyring:my-headers"],
            }
        )
        secret_store.put("my-hook", WEBHOOK)
        secret_store.put("my-headers", json.dumps([HEADER]))
        secret_store.put("monitor:old-name:webhook", WEBHOOK + "/monitor")
        paths.monitors_file().write_text(
            json.dumps(
                {
                    "cheap": {
                        "name": "cheap",
                        "mode": "search",
                        "query": "q",
                        "webhook": "keyring:monitor:old-name:webhook",
                    }
                }
            )
        )

        result = run("config", "set", "secret-storage", "file", "-y")

        assert result.exit_code == 0, result.output
        assert load_config()["webhook"] == WEBHOOK
        assert load_config()["webhook_headers"] == [HEADER]
        assert load_monitors()["cheap"]["webhook"] == WEBHOOK + "/monitor"
        assert vault.accounts() == []  # nothing is left behind
        assert "left in the keychain" not in result.output

    def test_it_asks_first_and_declining_changes_nothing(self, tmp_config, vault):
        original_auth = seed_everything(tmp_config)
        before = (paths.config_file().read_text(), paths.monitors_file().read_text())

        result = run("config", "set", "secret-storage", "keyring", input="n\n")

        assert "your Audible login (auth.json)" in result.stdout
        assert "the webhook of monitor 'cheap'" in result.stdout
        assert "Cancelled" in result.stdout
        assert paths.auth_file().read_text() == original_auth
        assert (
            paths.config_file().read_text(),
            paths.monitors_file().read_text(),
        ) == before
        assert vault.items == {}

    def test_a_failure_part_way_leaves_the_files_as_they_were(self, tmp_config, vault):
        original_auth = seed_everything(tmp_config)
        before = (paths.config_file().read_text(), paths.monitors_file().read_text())
        vault.fail_on_set_after = (
            4  # the probe, then the login's pieces, then it breaks
        )

        result = run("config", "set", "secret-storage", "keyring", "-y")

        assert result.exit_code == 1
        assert "keychain" in result.stderr
        assert paths.auth_file().read_text() == original_auth
        assert (
            paths.config_file().read_text(),
            paths.monitors_file().read_text(),
        ) == before
        assert load_config().get("secret_storage") is None

    def test_back_to_files_stops_if_a_secret_is_missing(self, tmp_config, vault):
        seed_everything(tmp_config)
        run("config", "set", "secret-storage", "keyring", "-y")
        del vault.items[("audible-deals", "webhook")]
        before = paths.config_file().read_text()

        result = run("config", "set", "secret-storage", "file", "-y")

        assert result.exit_code == 1
        assert "no entry 'webhook'" in result.stderr
        assert paths.config_file().read_text() == before
        assert load_config()["secret_storage"] == "keyring"

    def test_without_a_keychain_it_says_so_and_changes_nothing(
        self, tmp_config, monkeypatch
    ):
        seed_everything(tmp_config)
        monkeypatch.setattr(keyring.core, "_keyring_backend", fail.Keyring())
        before = paths.config_file().read_text()

        result = run("config", "set", "secret-storage", "keyring", "-y")

        assert result.exit_code == 1
        assert "no usable keychain" in result.stderr
        assert paths.config_file().read_text() == before

    def test_with_nothing_to_move_it_just_switches(self, tmp_config, vault):
        result = run("config", "set", "secret-storage", "keyring")

        assert result.exit_code == 0, result.output
        assert load_config()["secret_storage"] == "keyring"
        assert "This moves" not in result.stdout

    def test_switching_twice_is_harmless(self, tmp_config, vault):
        seed_everything(tmp_config)
        run("config", "set", "secret-storage", "keyring", "-y")
        snapshot = dict(vault.items)

        result = run("config", "set", "secret-storage", "keyring", "-y")

        assert result.exit_code == 0, result.output
        assert vault.items == snapshot and "This moves" not in result.stdout

    def test_a_half_finished_switch_still_works(self, tmp_config, vault, monkeypatch):
        """Readers go by what a file holds, not by the setting."""
        seed_everything(tmp_config)
        run("config", "set", "secret-storage", "keyring", "-y")
        save_config({**load_config(), "secret_storage": "file"})  # the setting was lost

        auth = AuthStore(paths.auth_file(), "us").load_authenticator()

        assert auth.access_token.startswith("Atna|")

    def test_an_invalid_choice_is_rejected(self, tmp_config):
        result = run("config", "set", "secret-storage", "vault")

        assert result.exit_code == 1
        assert "Valid: file, keyring" in result.stderr


# -- doctor ----------------------------------------------------------------------


class TestDoctor:
    def _checks(self):
        result = run("doctor", "--json")
        return {c["name"]: c for c in json.loads(result.stdout)["checks"]}

    def test_files_are_reported_plainly(self, tmp_config, mock_client):
        assert self._checks()["Secret storage"]["status"] == "pass"

    def test_a_working_keychain_with_a_background_schedule_warns(
        self, tmp_config, vault, mock_client, monkeypatch
    ):
        from audible_deals import scheduler
        from audible_deals.state_store import save_install_record

        seed_everything(tmp_config)
        run("config", "set", "secret-storage", "keyring", "-y")
        save_install_record({"every": "6h", "interval_s": 21600, "method": "t"})
        monkeypatch.setattr(scheduler, "installed", lambda: (True, "x"))
        monkeypatch.setattr(scheduler, "scheduled_command", lambda: [sys.executable])

        checks = self._checks()

        assert checks["Secret storage"]["status"] == "pass"
        assert "MemoryKeyring" in checks["Secret storage"]["detail"]
        assert checks["Login in keychain"]["status"] == "pass"
        warning = checks["Keychain and background runs"]
        assert warning["status"] == "warn"
        assert "deals config set secret-storage file" in warning["detail"]

    def test_a_missing_keychain_is_a_failure(
        self, tmp_config, mock_client, monkeypatch
    ):
        save_config({"secret_storage": "keyring"})
        monkeypatch.setattr(keyring.core, "_keyring_backend", fail.Keyring())

        row = self._checks()["Secret storage"]

        assert row["status"] == "fail" and "no usable keychain" in row["detail"]

    def test_a_login_missing_from_the_keychain_is_a_failure(
        self, tmp_config, vault, mock_client
    ):
        seed_everything(tmp_config)
        run("config", "set", "secret-storage", "keyring", "-y")
        del vault.items[("audible-deals", "auth")]

        row = self._checks()["Login in keychain"]

        assert row["status"] == "fail" and "deals login" in row["detail"]

    def test_leftover_references_in_file_mode_are_a_warning(
        self, tmp_config, vault, mock_client
    ):
        seed_everything(tmp_config)
        run("config", "set", "secret-storage", "keyring", "-y")
        save_config({**load_config(), "secret_storage": "file"})

        row = self._checks()["Secret storage"]

        assert row["status"] == "warn" and "still in the keychain" in row["detail"]


# -- the default path never loads keyring ----------------------------------------


def test_file_mode_never_imports_the_keyring_package(tmp_path):
    code = (
        "import sys\n"
        "from audible_deals.cli import cli\n"
        "for args in (['config', 'list'], ['config', 'set', 'max-price', '5'],"
        " ['wishlist', 'list'], ['doctor', '--json']):\n"
        "    try:\n"
        "        cli.main(args=args, prog_name='deals', standalone_mode=False)\n"
        "    except SystemExit:\n"
        "        pass\n"
        "    except Exception:\n"
        "        pass\n"
        "print('keyring' in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=bare_env(HOME=str(tmp_path), DEALS_HOME=str(tmp_path / "d")),
    )

    assert result.stdout.strip().endswith("False"), result.stderr


def test_the_secret_modules_stay_off_the_startup_path(tmp_path):
    code = (
        "import sys\n"
        "from audible_deals.cli import cli\n"
        "try:\n"
        "    cli.main(args=['--version'], prog_name='deals', standalone_mode=False)\n"
        "except SystemExit:\n"
        "    pass\n"
        "print([m for m in sys.modules if m.startswith('audible_deals.secret')"
        " or m == 'keyring'])\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=bare_env(HOME=str(tmp_path)),
    )

    assert result.stdout.strip().endswith("[]"), result.stdout + result.stderr
