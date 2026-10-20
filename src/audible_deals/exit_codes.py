"""The exit statuses ``deals`` uses, in one place.

Scripts can rely on these (they are documented under "Exit codes" in the
command reference); nothing else should pass a bare number to ``exit``.

====  =====================================================================
 0    success
 1    error (a failed command, bad data, an unexpected problem)
 2    usage error (bad option or argument)
 3    authentication required or rejected (run ``deals login``)
 4    the Audible request failed (network, server, rate limit)
 10   nothing matched, for commands run with ``--exit-code``
 130  interrupted with Ctrl-C
====  =====================================================================

This module imports nothing heavy so any layer can use it.
"""

from __future__ import annotations

import enum
import sys


class ExitCode(enum.IntEnum):
    OK = 0
    ERROR = 1
    USAGE = 2
    AUTH = 3
    REQUEST = 4
    NO_MATCH = 10
    INTERRUPTED = 130


class AuthRequiredError(RuntimeError):
    """There is no usable saved login (or Audible rejected it)."""

    exit_code = ExitCode.AUTH


def _http_status(exc: BaseException) -> int | None:
    return getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )


def exit_code_for(exc: BaseException) -> ExitCode | None:
    """The code an authentication or request failure maps to, else ``None``.

    The SDK's exception types are looked up only when its module is already
    loaded (an SDK error cannot exist otherwise), so this never imports it.
    """
    if isinstance(exc, AuthRequiredError):
        return ExitCode.AUTH
    module = sys.modules.get("audible.exceptions")
    if module is not None:
        auth_types = tuple(
            t
            for name in ("Unauthorized", "NoRefreshToken", "AuthFlowError")
            if isinstance(t := getattr(module, name, None), type)
        )
        if auth_types and isinstance(exc, auth_types):
            return ExitCode.AUTH
        request_error = getattr(module, "RequestError", None)
        if isinstance(request_error, type) and isinstance(exc, request_error):
            return ExitCode.REQUEST
    if _http_status(exc) in (401, 403):
        return ExitCode.AUTH
    return None


def is_auth_failure(exc: BaseException) -> bool:
    return exit_code_for(exc) is ExitCode.AUTH
