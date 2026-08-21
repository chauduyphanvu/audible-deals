"""Isolated webhook HTTP delivery with retries and redirect refusal."""

from __future__ import annotations

import logging
import random
import time
import urllib.request
from collections.abc import Callable

logger = logging.getLogger(__name__)

_RETRY_DELAYS = (2.0, 6.0)


class WebhookDeliveryError(RuntimeError):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class WebhookClient:
    def __init__(
        self,
        *,
        opener=None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._opener = opener or urllib.request.build_opener(_NoRedirectHandler())
        self._sleep = sleep
        self._jitter = jitter

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
                self._opener.open(request, timeout=10)
                return
            except Exception as exc:
                last_exc = exc
                if attempt < 3:
                    delay = _RETRY_DELAYS[attempt - 1] + self._jitter(-0.3, 0.3)
                    logger.warning("webhook POST attempt %d/3 failed: %s", attempt, exc)
                    self._sleep(max(0.0, delay))
        logger.error("webhook POST failed", exc_info=last_exc)
        raise WebhookDeliveryError(f"Webhook failed: {last_exc}")
