"""The exact requests each webhook format sends, captured from real deliveries.

``tests/fixtures/webhooks/<format>.<kind>.json`` was recorded by running the real
CLI against a fake Audible API and a local HTTP server standing in for the
webhook (``deals notify`` and ``deals recap`` with every ``--webhook-format``, and
a failed ``deals track run`` for the status message), with titles that contain
``< > | &`` and ``* _ ~ ` [ ]`` plus a mention. The generic fixtures hold the raw
inputs; this test feeds them to every provider and checks that the bytes on the
wire are unchanged, then sends them through the real client to a local server.

After an intended change to a payload, rewrite the fixtures from the same
inputs with ``UPDATE_GOLDEN=1 uv run pytest tests/test_webhook_fixtures.py`` and
review the diff.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from audible_deals.constants import WEBHOOK_FORMATS
from audible_deals.webhook_client import WebhookClient
from audible_deals.webhooks import (
    format_recap_payload,
    format_webhook_message,
    format_webhook_payload,
)
from tests.webhook_server import RecordingServer

FIXTURES = Path(__file__).parent / "fixtures" / "webhooks"
KEPT_HEADERS = ("Content-Type", "Title", "Tags", "Priority")
KINDS = ("deals", "recap", "status")


def load(fmt: str, kind: str) -> dict:
    return json.loads((FIXTURES / f"{fmt}.{kind}.json").read_text(encoding="utf-8"))


def regenerate(fmt: str, kind: str) -> dict:
    """What the providers produce today from the captured raw inputs."""
    raw = load("generic", kind)["request"]["body"]
    if kind == "deals":
        body, headers = format_webhook_payload(raw["deals"], fmt)
    elif kind == "recap":
        body, headers = format_recap_payload(raw, fmt)
    else:
        body, headers = format_webhook_message(raw["message"], fmt, title=raw["title"])
    text = body.decode("utf-8")
    parsed = (
        json.loads(text)
        if headers["Content-Type"].startswith("application/json")
        else text
    )
    return {
        "headers": {k: v for k, v in headers.items() if k in KEPT_HEADERS},
        "body": parsed,
    }


def test_every_format_and_kind_has_a_fixture():
    expected = {f"{fmt}.{kind}.json" for fmt in WEBHOOK_FORMATS for kind in KINDS}
    assert {path.name for path in FIXTURES.glob("*.json")} == expected


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("fmt", WEBHOOK_FORMATS)
def test_the_payload_is_unchanged(fmt, kind):
    path = FIXTURES / f"{fmt}.{kind}.json"
    actual = regenerate(fmt, kind)

    if os.environ.get("UPDATE_GOLDEN"):
        record = load(fmt, kind)
        record["request"] = actual
        path.write_text(
            json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return
    assert actual == load(fmt, kind)["request"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("fmt", WEBHOOK_FORMATS)
def test_the_client_puts_exactly_those_bytes_on_the_wire(fmt, kind):
    recorded = load(fmt, kind)["request"]
    raw = load("generic", kind)["request"]["body"]
    if kind == "deals":
        body, headers = format_webhook_payload(raw["deals"], fmt)
    elif kind == "recap":
        body, headers = format_recap_payload(raw, fmt)
    else:
        body, headers = format_webhook_message(raw["message"], fmt, title=raw["title"])
    server = RecordingServer()
    try:
        WebhookClient(allow_private=True, proxies={}).post(server.url, body, headers)
        (request,) = server.requests
    finally:
        server.close()

    assert request["body"] == body
    received = {k: v for k, v in request["headers"].items() if k in KEPT_HEADERS}
    assert received == recorded["headers"]
    sent_text = request["body"].decode("utf-8")
    sent = (
        json.loads(sent_text)
        if received["Content-Type"].startswith("application/json")
        else sent_text
    )
    assert sent == recorded["body"]


def test_the_awkward_titles_really_are_in_the_fixtures():
    deals = load("generic", "deals")["request"]["body"]["deals"]

    assert deals[0]["title"] == (
        "Tom & Jerry <Live> | A *Bold* _Move_ ~Strike~ `code` @everyone"
    )
