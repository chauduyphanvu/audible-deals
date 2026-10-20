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
import errno
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


#: Errors from the operating system that mean "the network is not there", for
#: the plain OSError a failed socket call raises (the named subclasses such as
#: ConnectionError and TimeoutError are recognized by type).
_NETWORK_ERRNOS = frozenset(
    {errno.ENETUNREACH, errno.ENETDOWN, errno.EHOSTUNREACH, errno.EHOSTDOWN}
)


def network_reason(exc: BaseException) -> str | None:
    """A few words for why a *network* call failed, or ``None`` if *exc* is not one.

    Covers what the HTTP layer raises when no response came back (httpx's
    ``RequestError`` family: connect failures, timeouts, dropped connections)
    and the standard library's own network errors (``ConnectionError``,
    ``TimeoutError``, a failed name lookup). A response with an error status is
    not a network problem and is judged by :func:`_http_status`. Modules are
    looked up only when already loaded, so this never imports anything.
    """
    httpx = sys.modules.get("httpx")
    if httpx is not None and isinstance(exc, httpx.RequestError):
        if isinstance(exc, httpx.TimeoutException):
            return "timed out"
        if isinstance(exc, httpx.ConnectError):
            return "could not connect"
        if isinstance(exc, httpx.ProxyError):
            return "the proxy refused the request"
        return "network error"
    if isinstance(exc, BrokenPipeError):
        return None  # our own closed output, not the network
    if isinstance(exc, TimeoutError):
        return "timed out"
    if isinstance(exc, ConnectionRefusedError):
        return "could not connect"
    if isinstance(exc, ConnectionError):
        return "connection lost"
    gaierror = getattr(sys.modules.get("socket"), "gaierror", None)
    if isinstance(gaierror, type) and isinstance(exc, gaierror):
        return "could not look up the server name"
    ssl_error = getattr(sys.modules.get("ssl"), "SSLError", None)
    if isinstance(ssl_error, type) and isinstance(exc, ssl_error):
        return "secure connection failed"
    if isinstance(exc, OSError) and exc.errno in _NETWORK_ERRNOS:
        return "network unreachable"
    return None


def request_failure_reason(exc: BaseException) -> str:
    """A few words for why an Audible request failed (for a message or a warning)."""
    reason = network_reason(exc)
    if reason is not None:
        return reason
    status = _http_status(exc)
    if status == 429:
        return "Audible is limiting requests"
    if status is not None:
        return f"HTTP {status}"
    return "unexpected response"


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
    if network_reason(exc) is not None:
        return ExitCode.REQUEST
    return None


def is_auth_failure(exc: BaseException) -> bool:
    return exit_code_for(exc) is ExitCode.AUTH
