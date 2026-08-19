"""Local, non-network inspection of saved Audible authentication."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

from audible_deals import constants


@dataclass(frozen=True)
class AuthInspection:
    """The usability and freshness of a local auth file."""

    status: str
    error: str = ""

    @property
    def is_usable(self) -> bool:
        return self.status in {"valid", "expiring", "unknown_expiry"}


def inspect_auth_file(
    path: Path | None = None, *, now: float | None = None
) -> AuthInspection:
    """Inspect local auth data without constructing a client or making requests."""
    auth_file = path or constants.AUTH_FILE
    try:
        if not auth_file.exists():
            return AuthInspection("missing")
    except OSError as exc:
        return AuthInspection("malformed", str(exc))

    try:
        data = json.loads(auth_file.read_text())
        if not isinstance(data, dict):
            raise ValueError("auth file is not a JSON object")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return AuthInspection("malformed", str(exc))

    for key in ("access_token", "refresh_token"):
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
