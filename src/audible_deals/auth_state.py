"""Local, non-network inspection of saved Audible authentication."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

from audible_deals import constants, paths
from audible_deals.secret_store import AUTH_ACCOUNT, SERVICE
from audible_deals.storage import read_text

#: Key of the small file that stands in for ``auth.json`` when the login lives in
#: the OS keychain (see :mod:`audible_deals.secret_store`).
REFERENCE_KEY = "secret_storage"


def auth_reference(auth_data: dict) -> dict:
    """What ``auth.json`` holds in keyring mode: where the login is, and when it expires.

    The expiry and marketplace are not secret, and keeping them here lets
    ``deals doctor`` and the dashboard judge the login without opening the keychain.
    """
    return {
        REFERENCE_KEY: "keyring",
        "service": SERVICE,
        "account": AUTH_ACCOUNT,
        "locale_code": auth_data.get("locale_code"),
        "expires": auth_data.get("expires"),
    }


def parse_auth_reference(data: object) -> dict | None:
    """*data* if it is a keyring reference file, else ``None``."""
    if (
        isinstance(data, dict)
        and data.get(REFERENCE_KEY) == "keyring"
        and isinstance(data.get("account"), str)
    ):
        return data
    return None


@dataclass(frozen=True)
class AuthInspection:
    """The usability and freshness of a local auth file."""

    status: str
    error: str = ""

    @property
    def is_usable(self) -> bool:
        return self.status in {"valid", "expiring", "expired", "unknown_expiry"}


def auth_file_is_reference(path: Path | None = None) -> bool:
    """Whether the auth file only points at the OS keychain (no secrets in it)."""
    try:
        return (
            parse_auth_reference(json.loads(read_text(path or paths.auth_file())))
            is not None
        )
    except (OSError, ValueError):
        return False


def inspect_auth_file(
    path: Path | None = None, *, now: float | None = None
) -> AuthInspection:
    """Inspect local auth data without constructing a client or making requests."""
    auth_file = path or paths.auth_file()
    try:
        if not auth_file.exists():
            return AuthInspection("missing")
    except OSError as exc:
        return AuthInspection("malformed", str(exc))

    try:
        data = json.loads(read_text(auth_file))
        if not isinstance(data, dict):
            raise ValueError("auth file is not a JSON object")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return AuthInspection("malformed", str(exc))

    is_reference = parse_auth_reference(data) is not None
    for key in () if is_reference else ("access_token", "refresh_token"):
        if not isinstance(data.get(key), str) or not data[key]:
            return AuthInspection("malformed", f"missing required key: {key!r}")
    locale_code = data.get("locale_code")
    if not isinstance(locale_code, str) or locale_code not in constants.LOCALE_DOMAIN:
        return AuthInspection("malformed", "missing or invalid locale_code")

    expires = data.get("expires")
    if expires is None:
        return AuthInspection("unknown_expiry")
    try:
        expiry = float(expires)
        if not math.isfinite(expiry):
            raise ValueError("expires is not finite")
    except (TypeError, ValueError):
        return AuthInspection("unknown_expiry")

    current_time = time.time() if now is None else now
    if expiry < current_time:
        return AuthInspection("expired")
    if expiry < current_time + 86400:
        return AuthInspection("expiring")
    return AuthInspection("valid")
