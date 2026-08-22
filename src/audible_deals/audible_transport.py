"""Audible API request transport."""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import TYPE_CHECKING, Any

import click

from audible_deals.auth_store import AuthStore

if TYPE_CHECKING:
    import audible

logger = logging.getLogger(__name__)

_LOG_PARAM_KEYS = (
    "page",
    "num_results",
    "products_sort_by",
    "category_id",
    "title",
    "asins",
    "sort_by",
)
_RETRY_DELAYS = (1.0, 2.0)


def _log_request_params(params: dict[str, Any]) -> dict[str, Any]:
    """Return the request parameters safe and useful to log."""
    snapshot: dict[str, Any] = {k: params[k] for k in _LOG_PARAM_KEYS if k in params}
    keywords = params.get("keywords")
    if keywords:
        snapshot["keywords"] = (
            keywords if len(keywords) <= 80 else keywords[:77] + "..."
        )
    return snapshot


def _retryable_status(exc: Exception) -> int | None:
    """Pull an HTTP status code off an exception or its response, if present."""
    return getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )


def _retry_delay(attempt: int, exc: Exception, status: int | None) -> float:
    """Compute jittered backoff, honoring a numeric 429 Retry-After header."""
    delay = max(0.0, _RETRY_DELAYS[attempt - 1] + random.uniform(-0.3, 0.3))
    if status == 429:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        retry_after = headers.get("Retry-After", "") if headers else ""
        if isinstance(retry_after, str) and retry_after.isdigit():
            delay = max(delay, min(int(retry_after), 120))
    return delay


class AudibleTransport:
    """Own a lazy Audible client and request retry lifecycle."""

    def __init__(self, auth_store: AuthStore):
        self._auth_store = auth_store
        self._client: audible.Client | None = None
        self._lifecycle = threading.Condition()
        self._active_requests = 0
        self._closing = False
        self._abort = threading.Event()

    def _get_client(self) -> audible.Client:
        import audible

        with self._lifecycle:
            if self._client is None:
                auth = self._auth_store.load_authenticator()
                self._client = audible.Client(auth=auth)
            return self._client

    def _begin_request(self) -> None:
        with self._lifecycle:
            while self._closing:
                self._lifecycle.wait()
            self._active_requests += 1

    def _end_request(self) -> None:
        with self._lifecycle:
            self._active_requests -= 1
            if self._active_requests == 0:
                self._lifecycle.notify_all()

    def request(self, endpoint: str, **params: Any) -> dict:
        """Execute a GET request with logging, retry, and tuple unwrapping."""
        self._begin_request()
        try:
            client = self._get_client()
            debug = logger.isEnabledFor(logging.DEBUG)
            for attempt in range(1, 4):
                if debug:
                    logger.debug(
                        "API GET %s params=%s", endpoint, _log_request_params(params)
                    )
                    start = time.monotonic()
                try:
                    response = client.get(endpoint, **params)
                except Exception as exc:
                    if isinstance(exc, click.ClickException):
                        raise
                    if isinstance(exc, RuntimeError) and "Not authenticated" in str(
                        exc
                    ):
                        raise
                    status = _retryable_status(exc)
                    if status is not None and 400 <= status < 500 and status != 429:
                        raise
                    if attempt >= 3:
                        logger.warning(
                            "API GET %s failed (attempt %d/%d): %s; giving up",
                            endpoint,
                            attempt,
                            3,
                            exc,
                        )
                        raise
                    delay = _retry_delay(attempt, exc, status)
                    logger.warning(
                        "API GET %s failed (attempt %d/%d): %s; retrying in %.1fs",
                        endpoint,
                        attempt,
                        3,
                        exc,
                        delay,
                    )
                    if self._abort.wait(delay):
                        raise
                    continue
                self._auth_store.persist_refreshed_auth()
                if isinstance(response, tuple):
                    response = response[0]
                if debug:
                    elapsed_ms = (time.monotonic() - start) * 1000
                    items = 0
                    if isinstance(response, dict):
                        for key in ("products", "items", "categories"):
                            value = response.get(key)
                            if isinstance(value, list):
                                items = len(value)
                                break
                    total = (
                        response.get("total_results")
                        if isinstance(response, dict)
                        else None
                    )
                    logger.debug(
                        "API GET %s done %.0fms items=%d total=%s",
                        endpoint,
                        elapsed_ms,
                        items,
                        total,
                    )
                return response
        finally:
            self._end_request()

    def cancel(self) -> None:
        self._abort.set()

    def reset_abort(self) -> None:
        with self._lifecycle:
            if not self._closing:
                self._abort.clear()

    def close(self) -> None:
        with self._lifecycle:
            while self._closing:
                self._lifecycle.wait()
            self._closing = True
            self._abort.set()
            while self._active_requests:
                self._lifecycle.wait()
            client = self._client
        try:
            if client is not None:
                self._auth_store.retry_pending_persistence()
                client.close()
                with self._lifecycle:
                    self._client = None
            self._auth_store.unload()
        finally:
            with self._lifecycle:
                self._abort.clear()
                self._closing = False
                self._lifecycle.notify_all()
