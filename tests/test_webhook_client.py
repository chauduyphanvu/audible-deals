"""Webhook transport isolation and retry tests."""

import urllib.request

import pytest

from audible_deals.webhook_client import WebhookClient, WebhookDeliveryError


class RecordingOpener:
    def __init__(self, failures=0):
        self.failures = failures
        self.calls = []
        self.initial_headers = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        self.initial_headers.append(dict(request.headers))
        if len(self.calls) <= self.failures:
            request.add_header("X-Mutated", "attempt")
            raise OSError(f"failure {len(self.calls)}")
        return None


def test_client_is_instance_local_and_does_not_install_global_opener():
    original = urllib.request._opener

    first = WebhookClient()
    second = WebhookClient()

    assert urllib.request._opener is original
    assert first._opener is not second._opener


def test_retries_use_fresh_requests_copied_headers_timeout_and_exact_delays():
    opener = RecordingOpener(failures=2)
    sleeps = []
    headers = {"Authorization": "secret"}

    WebhookClient(
        opener=opener,
        sleep=sleeps.append,
        jitter=lambda low, high: 0.25,
    ).post("https://example.test/hook", b"body", headers)

    requests = [request for request, _ in opener.calls]
    assert len({id(request) for request in requests}) == 3
    assert [timeout for _, timeout in opener.calls] == [10, 10, 10]
    assert sleeps == [2.25, 6.25]
    assert headers == {"Authorization": "secret"}
    assert all("X-mutated" not in headers for headers in opener.initial_headers)


def test_exactly_three_failures_raise_domain_error():
    opener = RecordingOpener(failures=3)

    with pytest.raises(WebhookDeliveryError, match="Webhook failed: failure 3"):
        WebhookClient(
            opener=opener,
            sleep=lambda seconds: None,
            jitter=lambda low, high: -10,
        ).post("https://example.test/hook", b"body", {})

    assert len(opener.calls) == 3
