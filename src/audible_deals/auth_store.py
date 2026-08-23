"""Audible authentication loading and persistence."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from audible_deals.constants import LOCALE_DOMAIN
from audible_deals.locking import advisory_lock
from audible_deals.storage import _atomic_write, _atomic_write_bytes

if TYPE_CHECKING:
    import audible

logger = logging.getLogger(__name__)

_UMASK_LOCK = threading.Lock()


@contextlib.contextmanager
def _restrictive_umask():
    """Temporarily set umask to 0o177 so new files are created at 0o600."""
    with _UMASK_LOCK:
        old = os.umask(0o177)
        try:
            yield
        finally:
            os.umask(old)


def _auth_from_libation(data: dict, locale: str) -> dict:
    """Build Mkb79Auth-format auth data from Libation's AccountsSettings.json."""
    accounts = data.get("Accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("No accounts found in Libation settings")
    if not isinstance(accounts[0], dict):
        raise ValueError("Libation account entry must be a JSON object")
    tokens = accounts[0].get("IdentityTokens", {})
    if not isinstance(tokens, dict):
        raise ValueError("Libation IdentityTokens must be a JSON object")
    for key in ("access_token", "refresh_token"):
        if not isinstance(tokens.get(key), str) or not tokens[key]:
            raise ValueError(f"Libation auth missing required key: {key!r}")
    return {
        "website_cookies": tokens.get("website_cookies"),
        "adp_token": tokens.get("adp_token"),
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "device_private_key": tokens.get("device_private_key"),
        "store_authentication_cookie": tokens.get("store_authentication_cookie"),
        "device_info": tokens.get("device_info", {}),
        "customer_info": tokens.get("customer_info", {}),
        "expires": tokens.get("expires", 0),
        "locale_code": tokens.get("locale_code", locale),
        "with_username": tokens.get("with_username", False),
        "encryption": False,
    }


def _validate_audible_cli_auth(data: dict, locale: str | None = None) -> dict:
    """Validate auth data already in audible-cli / Mkb79Auth format."""
    for key in ("access_token", "refresh_token"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"Auth file missing required key: {key!r}")
    if "locale_code" in data and (
        not isinstance(data["locale_code"], str)
        or data["locale_code"] not in LOCALE_DOMAIN
    ):
        raise ValueError(
            f"Unknown locale_code: {data['locale_code']!r}. "
            f"Valid: {', '.join(sorted(LOCALE_DOMAIN))}"
        )
    if "encryption" not in data:
        data["encryption"] = False
    if "locale_code" not in data and locale is not None:
        data["locale_code"] = locale
    return data


def _reject_json_constant(value: str):
    raise ValueError(f"invalid JSON constant: {value}")


class AuthStore:
    """Own saved Audible credentials and the loaded authenticator state."""

    def __init__(self, auth_file: Path, locale: str):
        self.auth_file = auth_file
        self.locale = locale
        self._authenticator: audible.Authenticator | None = None
        self._auth_snapshot: tuple[object, object, object] | None = None
        self._auth_file_fingerprint: bytes | None = None
        self._auth_save_pending = False
        self._auth_save_warned = False
        self._auth_persistence_disabled = False
        self._state_lock = threading.RLock()

    def _prepare_auth_dir(self) -> None:
        self.auth_file.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.auth_file.parent, 0o700)

    def _auth_file_lock(self):
        lock_file = self.auth_file.with_name(f".{self.auth_file.name}.lock")
        return advisory_lock(lock_file, wait=True)

    @staticmethod
    def _auth_fingerprint(contents: bytes) -> bytes:
        return hashlib.sha256(contents).digest()

    @staticmethod
    def _auth_refresh_state(
        auth: audible.Authenticator,
    ) -> tuple[object, object, object]:
        return (auth.access_token, auth.refresh_token, auth.expires)

    def login(self, username: str, password: str) -> None:
        """Interactive Audible login. Persists tokens to auth_file."""
        import audible

        logger.info("login (interactive) locale=%s", self.locale)
        with self._state_lock:
            self._prepare_auth_dir()
            auth = audible.Authenticator.from_login(
                username,
                password,
                locale=self.locale,
                with_username=True,
            )
            with self._auth_file_lock():
                with _restrictive_umask():
                    auth.to_file(self.auth_file)
                os.chmod(self.auth_file, 0o600)
        logger.info("login complete, auth written to %s", self.auth_file)

    def login_external(
        self,
        callback_url_file: Path | None = None,
        login_url_callback: Callable[[str], str] | None = None,
    ) -> None:
        """Login via external browser (for captcha/2FA). Persists tokens."""
        import audible

        if callback_url_file is not None and login_url_callback is not None:
            raise ValueError(
                "Use either callback_url_file or login_url_callback, not both"
            )
        logger.info("login_external locale=%s", self.locale)
        with self._state_lock:
            self._prepare_auth_dir()

            if callback_url_file:

                def _file_callback(oauth_url: str) -> str:
                    from audible_deals.presentation.terminal import safe_text

                    print()
                    print("Open this URL in your browser and log in:")
                    print()
                    print(safe_text(oauth_url))
                    print()
                    print(
                        "After login you'll see a 'Page not found' page. "
                        "That's expected."
                    )
                    print(
                        "Copy the FULL URL from your browser's address bar "
                        f"and save it to:\n  {safe_text(callback_url_file)}"
                    )
                    print()
                    input("Press Enter here once the file is saved...")
                    url = callback_url_file.read_text().strip()
                    if not url:
                        raise RuntimeError(f"File is empty: {callback_url_file}")
                    return url

                auth = audible.Authenticator.from_login_external(
                    locale=self.locale,
                    login_url_callback=_file_callback,
                )
            elif login_url_callback is not None:
                auth = audible.Authenticator.from_login_external(
                    locale=self.locale,
                    login_url_callback=login_url_callback,
                )
            else:
                auth = audible.Authenticator.from_login_external(
                    locale=self.locale,
                )

            with self._auth_file_lock():
                with _restrictive_umask():
                    auth.to_file(self.auth_file)
                os.chmod(self.auth_file, 0o600)
        logger.info("login_external complete, auth written to %s", self.auth_file)

    def import_auth(self, source_path: Path) -> None:
        """Import auth from an audible-cli or Libation-exported JSON file."""
        logger.info("import_auth from %s", source_path)
        with self._state_lock:
            self._prepare_auth_dir()

            raw = source_path.read_text()
            if len(raw) > 1_000_000:
                raise ValueError(
                    f"Auth file too large ({len(raw):,} chars). "
                    "Expected a small JSON credentials file."
                )

            data = json.loads(raw, parse_constant=_reject_json_constant)
            if not isinstance(data, dict):
                raise ValueError("Auth file must contain a JSON object")
            if "Accounts" in data:
                auth_data = _validate_audible_cli_auth(
                    _auth_from_libation(data, self.locale), self.locale
                )
                source_format = "Libation"
            else:
                auth_data = _validate_audible_cli_auth(data, self.locale)
                source_format = "audible-cli"

            with self._auth_file_lock():
                _atomic_write(
                    self.auth_file,
                    json.dumps(auth_data, indent=2, allow_nan=False),
                )
                os.chmod(self.auth_file, 0o600)
        logger.info(
            "import_auth (%s format) written to %s", source_format, self.auth_file
        )

    @property
    def is_authenticated(self) -> bool:
        return self.auth_file.exists()

    def load_authenticator(self) -> audible.Authenticator:
        """Load the authenticator once for the current client session."""
        import audible

        with self._state_lock:
            if self._authenticator is not None:
                return self._authenticator
            if not self.auth_file.exists():
                raise RuntimeError("Not authenticated. Run 'deals login' first.")
            with self._auth_file_lock():
                if not self.auth_file.exists():
                    raise RuntimeError("Not authenticated. Run 'deals login' first.")
                auth_contents = self.auth_file.read_bytes()
                auth = audible.Authenticator.from_file(self.auth_file)
            self._authenticator = auth
            self._auth_snapshot = self._auth_refresh_state(auth)
            self._auth_file_fingerprint = self._auth_fingerprint(auth_contents)
            self._auth_save_pending = False
            self._auth_save_warned = False
            self._auth_persistence_disabled = False
            return auth

    def persist_refreshed_auth(self) -> None:
        """Atomically persist token refreshes without failing successful requests."""
        with self._state_lock:
            auth = self._authenticator
            if auth is None or self._auth_persistence_disabled:
                return
            current = self._auth_refresh_state(auth)
            if current == self._auth_snapshot:
                self._auth_save_pending = False
                return
            try:
                with self._auth_file_lock():
                    disk_contents = self.auth_file.read_bytes()
                    disk_fingerprint = self._auth_fingerprint(disk_contents)
                    if disk_fingerprint != self._auth_file_fingerprint:
                        if not self._auth_save_warned:
                            logger.warning(
                                "Not saving refreshed authentication because %s "
                                "changed after this client loaded it",
                                self.auth_file,
                            )
                            self._auth_save_warned = True
                        self._auth_snapshot = current
                        self._auth_save_pending = False
                        self._auth_persistence_disabled = True
                        return
                    serialized = json.dumps(
                        auth.to_dict(), indent=4, allow_nan=False
                    ).encode("utf-8")
                    _atomic_write_bytes(self.auth_file, serialized)
                    os.chmod(self.auth_file, 0o600)
                    self._auth_file_fingerprint = self._auth_fingerprint(serialized)
            except Exception as exc:
                self._auth_save_pending = True
                if not self._auth_save_warned:
                    logger.warning(
                        "Could not save refreshed authentication to %s: %s; "
                        "will retry when the client closes",
                        self.auth_file,
                        exc,
                    )
                    self._auth_save_warned = True
                return
            self._auth_snapshot = current
            self._auth_save_pending = False

    def retry_pending_persistence(self) -> None:
        with self._state_lock:
            if self._auth_save_pending:
                self.persist_refreshed_auth()

    def unload(self) -> None:
        with self._state_lock:
            self._authenticator = None
            self._auth_snapshot = None
            self._auth_file_fingerprint = None
            self._auth_save_pending = False
            self._auth_save_warned = False
            self._auth_persistence_disabled = False
