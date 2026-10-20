"""Isolated webhook HTTP delivery: retries, redirect refusal and address pinning.

Delivery uses ``urllib``. The decision to keep it (rather than add ``httpx``)
is recorded in docs/automation.md: the two things a webhook client needs here,
refusing redirects and connecting only to an address that was checked, are each
a few lines on ``http.client`` (a redirect handler that declines, and one
replaced ``_create_connection`` per connection), and ``httpx`` would add a
dependency to the startup path of a command that sends a few requests a day.
"""

from __future__ import annotations

import http.client
import logging
import random
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from audible_deals.webhook_guard import (
    AddressGuard,
    BlockedAddressError,
    Resolver,
    system_resolver,
    uses_proxy,
)

logger = logging.getLogger(__name__)

_RETRY_DELAYS = (2.0, 6.0)


class WebhookDeliveryError(RuntimeError):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _guarded(connection_class: type, guard: AddressGuard) -> type:
    """*connection_class* whose socket only ever connects to checked addresses.

    ``http.client`` opens the socket with ``self._create_connection`` and keeps
    ``self.host`` (the name) for the Host header and TLS server name, so
    swapping that one attribute pins the address without touching either.
    """

    class Guarded(connection_class):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._create_connection = guard.create_connection

    return Guarded


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, guard: AddressGuard) -> None:
        super().__init__()
        self._connection_class: Any = _guarded(http.client.HTTPConnection, guard)

    def http_open(self, req):
        return self.do_open(self._connection_class, req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, guard: AddressGuard, context=None) -> None:
        super().__init__(context=context)
        self._tls_context = context
        self._connection_class: Any = _guarded(http.client.HTTPSConnection, guard)

    def https_open(self, req):
        return self.do_open(self._connection_class, req, context=self._tls_context)


def _env_proxies() -> dict[str, str]:
    return urllib.request.getproxies()


class WebhookClient:
    """POST a webhook body, retrying transient failures.

    By default the client checks, at send time, that the host resolves only to
    public addresses and connects to the address it checked. ``allow_private``
    turns the check off for servers on your own network. Pass ``opener`` to
    replace the transport entirely (tests do).

    When an HTTP proxy is configured for the URL (``HTTPS_PROXY`` and friends,
    minus ``NO_PROXY`` hosts) the proxy resolves the name and makes the
    connection, so the client can neither resolve nor pin it; it only refuses a
    target whose text already says it is private (an address literal,
    ``localhost``) and hands the rest to the proxy. Hosts ``NO_PROXY`` exempts
    are connected to directly and checked in full.
    """

    def __init__(
        self,
        *,
        opener=None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        allow_private: bool = False,
        resolver: Resolver = system_resolver,
        proxies: Mapping[str, str] | None = None,
    ) -> None:
        self._guard = AddressGuard(allow_private=allow_private, resolver=resolver)
        self._injected = opener is not None
        # ProxyHandler({}) stops build_opener adding a handler that reads the
        # proxy variables: proxied URLs take the separate, checked route below.
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
            _GuardedHTTPHandler(self._guard),
            _GuardedHTTPSHandler(self._guard),
        )
        self._proxies = dict(_env_proxies() if proxies is None else proxies)
        self._proxied_opener: urllib.request.OpenerDirector | None = None
        self._sleep = sleep
        self._jitter = jitter

    def _opener_for(self, url: str):
        """The opener for *url* (a proxied route is checked before it is used)."""
        if self._injected or not self._proxies:
            return self._opener
        parsed = urllib.parse.urlparse(url)
        if not uses_proxy(parsed.scheme, parsed.hostname or "", self._proxies):
            return self._opener
        # The proxy resolves the name, so no lookup here: only what the text of
        # the URL gives away (an address literal, localhost) is checked.
        self._guard.check_name(parsed.hostname or "")  # raises if blocked
        if self._proxied_opener is None:
            self._proxied_opener = urllib.request.build_opener(
                urllib.request.ProxyHandler(self._proxies), _NoRedirectHandler()
            )
        return self._proxied_opener

    def post(self, url: str, body: bytes, headers: dict[str, str]) -> None:
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            request = urllib.request.Request(url, data=body, headers=dict(headers))
            try:
                logger.debug(
                    "webhook POST %s attempt %d/3 payload_bytes=%d",
                    url,
                    attempt,
                    len(body),
                )
                self._opener_for(url).open(request, timeout=10)
                return
            except BlockedAddressError as exc:
                # A policy decision, not a transient failure: never retried.
                raise WebhookDeliveryError(f"Webhook refused: {exc}") from exc
            except Exception as exc:
                last_exc = exc
                if attempt < 3:
                    delay = _RETRY_DELAYS[attempt - 1] + self._jitter(-0.3, 0.3)
                    logger.warning("webhook POST attempt %d/3 failed: %s", attempt, exc)
                    self._sleep(max(0.0, delay))
        logger.error("webhook POST failed", exc_info=last_exc)
        raise WebhookDeliveryError(f"Webhook failed: {last_exc}")
