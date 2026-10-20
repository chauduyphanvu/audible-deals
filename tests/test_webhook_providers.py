"""One provider per destination: markup, escaping and format selection."""

from __future__ import annotations

import json

import pytest

from audible_deals import webhooks
from audible_deals.constants import WEBHOOK_FORMATS
from audible_deals.webhooks import (
    format_monitor_webhook_payload,
    format_recap_payload,
    format_webhook_message,
    format_webhook_payload,
    is_teams_connector_url,
    provider_for,
    slack_link,
    slack_text,
)

#: Every character that means something in at least one service's markup.
NASTY = "Tom & Jerry <Live> | A *Bold* _Move_ ~Strike~ `code` [x](y) @everyone"
ZWSP = "​"


def hit(title=NASTY, **overrides):
    values = {
        "asin": "B0ESCAPE01",
        "title": title,
        "price": 3.99,
        "target": 5.0,
        "url": "https://www.audible.com/pd/B0ESCAPE01?a=1&b=2",
    }
    values.update(overrides)
    return values


def recap_payload(title=NASTY):
    return {
        "days": 7,
        "new_count": 1,
        "drops": [
            {
                "asin": "B0ESCAPE01",
                "title": title,
                "old_price": 12.0,
                "new_price": 4.0,
                "drop_pct": 67,
            }
        ],
        "wishlist_hits": [{"asin": "B0ESCAPE01", "title": title}],
        "atl_hits": [{"asin": "B0ESCAPE01", "title": title, "price": 4.0}],
    }


def test_every_format_has_a_provider_and_every_provider_is_a_format():
    assert set(WEBHOOK_FORMATS) == set(webhooks._PROVIDERS)


@pytest.mark.parametrize("fmt", WEBHOOK_FORMATS)
def test_every_provider_formats_deals_recap_and_status(fmt):
    for body, headers in (
        format_webhook_payload([hit()], fmt),
        format_recap_payload(recap_payload(), fmt),
        format_webhook_message("text", fmt, title="Title"),
    ):
        assert isinstance(body, bytes) and body
        assert "Content-Type" in headers
        if headers["Content-Type"] == "application/json":
            json.loads(body)


class TestSlack:
    def test_the_three_control_characters_are_entities(self):
        assert slack_text("a & b <c> d") == "a &amp; b &lt;c&gt; d"

    def test_a_pipe_cannot_end_the_link_label_early(self):
        link = slack_link("https://x.test/a|b", "One | Two")

        assert link == "<https://x.test/a%7Cb|One ¦ Two>"
        assert link.count("|") == 1

    def test_formatting_characters_stay_literal(self):
        escaped = slack_text("*b* _i_ ~s~ `c`")

        for character in "*_~`":
            assert f"{character}{ZWSP}" in escaped

    def test_the_deal_line(self):
        body, _ = format_webhook_payload([hit()], "slack")

        text = json.loads(body)["text"]
        assert "Tom &amp; Jerry &lt;Live&gt; ¦ A *​Bold*​" in text
        assert "<https://www.audible.com/pd/B0ESCAPE01?a=1&amp;b=2|" in text
        assert "$3.99 (target $5.00)" in text
        assert "\n• " in text and text.startswith("*Audible Deals (1)*")

    def test_the_monitor_name_in_the_heading_is_escaped(self):
        event = hit(monitor="a <b> & c", event="new")

        text = json.loads(format_webhook_payload([event], "slack")[0])["text"]

        assert text.startswith("*Audible Deals — a &lt;b&gt; &amp; c (1)*")
        assert "[new] " in text

    def test_recap_and_status(self):
        recap = json.loads(format_recap_payload(recap_payload(), "slack")[0])["text"]
        status = json.loads(format_webhook_message("a <b>", "slack", title="T&T")[0])

        assert "Tom &amp; Jerry &lt;Live&gt;" in recap
        assert "At all-time low:" in recap and "$12.00 -> $4.00 (-67%)" in recap
        assert status == {"text": "*T&amp;T*\na &lt;b&gt;"}


class TestDiscord:
    def test_markdown_characters_are_backslash_escaped(self):
        body, _ = format_webhook_payload([hit()], "discord")

        content = json.loads(body)["content"]
        assert (
            "Tom & Jerry \\<Live\\> \\| A \\*Bold\\* \\_Move\\_ \\~Strike\\~" in content
        )
        assert "\\`code\\` \\[x\\]\\(y\\)" in content
        assert "(<https://www.audible.com/pd/B0ESCAPE01?a=1&b=2>)" in content

    def test_mentions_in_a_title_never_ping(self):
        for body, _ in (
            format_webhook_payload([hit()], "discord"),
            format_recap_payload(recap_payload(), "discord"),
            format_webhook_message("@everyone", "discord", title="T"),
        ):
            assert json.loads(body)["allowed_mentions"] == {"parse": []}

    def test_status_and_recap(self):
        status = json.loads(format_webhook_message("hi *x*", "discord", title="T_")[0])
        recap = json.loads(format_recap_payload(recap_payload(), "discord")[0])

        assert status["content"] == "**T\\_**\nhi \\*x\\*"
        assert recap["content"].startswith("**Audible Recap (last 7 days)**")


class TestTeams:
    def _card(self, body):
        data = json.loads(body)
        assert data["type"] == "message"
        (attachment,) = data["attachments"]
        assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
        assert attachment["contentUrl"] is None
        card = attachment["content"]
        assert card["$schema"] == "http://adaptivecards.io/schemas/adaptive-card.json"
        assert card["type"] == "AdaptiveCard"
        return card

    def test_deals_are_one_text_block_each_under_a_heading(self):
        body, headers = format_webhook_payload([hit(), hit("Second")], "teams")

        card = self._card(body)
        assert headers == {"Content-Type": "application/json"}
        texts = [block["text"] for block in card["body"]]
        assert texts[0] == "Audible Deals (2)"
        assert len(texts) == 3 and all(b["wrap"] for b in card["body"])
        assert texts[2].startswith("• [Second](https://www.audible.com/pd/B0ESCAPE01")

    def test_markdown_in_a_title_is_escaped(self):
        card = self._card(format_webhook_payload([hit()], "teams")[0])

        text = card["body"][1]["text"]
        assert "\\*Bold\\*" in text and "\\<Live\\>" in text and "\\|" in text

    def test_recap_and_status(self):
        recap = self._card(format_recap_payload(recap_payload(), "teams")[0])
        status = self._card(format_webhook_message("it broke", "teams", title="T")[0])

        recap_texts = [block["text"] for block in recap["body"]]
        assert recap_texts[0].startswith("Audible Recap")
        assert "At all-time low:" in recap_texts
        assert [block["text"] for block in status["body"]] == ["T", "it broke"]
        assert status["body"][0]["color"] == "Attention"

    @pytest.mark.parametrize(
        "url",
        [
            "https://contoso.webhook.office.com/webhookb2/a@b/IncomingWebhook/c/d",
            "https://CONTOSO.WEBHOOK.OFFICE.COM/webhookb2/x",
            "https://outlook.office.com/webhook/abc/IncomingWebhook/def",
            "https://outlook.office365.com/webhook/abc",
        ],
    )
    def test_connector_urls_keep_the_messagecard(self, url):
        assert is_teams_connector_url(url)
        data = json.loads(format_webhook_payload([hit()], "teams", url=url)[0])
        assert data["@type"] == "MessageCard"

    @pytest.mark.parametrize(
        "url",
        [
            "https://prod-12.westus.logic.azure.com:443/workflows/abc/triggers/manual",
            "https://example.com/webhook",
            "https://outlook.office.com/owa/",  # not a connector path
            "https://evil.example/.webhook.office.com",
            "https://webhook.office.com.evil.example/x",
            None,
        ],
    )
    def test_everything_else_gets_the_adaptive_card(self, url):
        assert not is_teams_connector_url(url)
        data = json.loads(format_webhook_payload([hit()], "teams", url=url)[0])
        assert data["type"] == "message"

    def test_teams_legacy_is_always_a_messagecard(self):
        for url in (None, "https://prod-12.westus.logic.azure.com/workflows/x"):
            data = json.loads(
                format_webhook_payload([hit()], "teams-legacy", url=url)[0]
            )
            assert data["@type"] == "MessageCard"
            assert data["title"] == "Audible Deals (1)"
            assert "\\*Bold\\*" in data["sections"][0]["text"]

    def test_the_url_choice_applies_to_recaps_and_status_messages_too(self):
        url = "https://contoso.webhook.office.com/webhookb2/x"

        assert (
            json.loads(format_recap_payload(recap_payload(), "teams", url=url)[0])[
                "@type"
            ]
            == "MessageCard"
        )
        assert (
            json.loads(format_webhook_message("x", "teams", url=url)[0])["@type"]
            == "MessageCard"
        )


class TestNtfy:
    def test_prices_use_the_shared_format_and_the_target_is_shown(self):
        body, headers = format_webhook_payload([hit(price=3, target=5)], "ntfy")

        text = body.decode()
        assert "$3.00 (target $5.00) https://www.audible.com/pd/B0ESCAPE01" in text
        assert headers["Tags"] == "book" and headers["Priority"] == "default"

    def test_a_huge_price_does_not_raise(self):
        body, _ = format_webhook_payload([hit(price=10**400, target=10**400)], "ntfy")

        assert f"${10**400}.00" in body.decode()

    def test_a_missing_target_is_a_dash(self):
        body, _ = format_webhook_payload([hit(target=None)], "ntfy")

        assert "(target -)" in body.decode()

    def test_a_non_latin1_title_is_encoded_so_the_request_can_be_sent(self):
        event = hit(monitor="cheap sci-fi", event="new")

        _, headers = format_webhook_payload([event], "ntfy")

        assert headers["Title"].startswith("=?UTF-8?B?")
        headers["Title"].encode("latin-1")  # would raise for the raw em dash
        import base64

        encoded = headers["Title"][len("=?UTF-8?B?") : -2]
        assert base64.b64decode(encoded).decode() == "Audible Deals — cheap sci-fi (1)"

    def test_a_monitor_alert_is_actually_deliverable(self):
        """The em dash in a monitor heading used to fail http.client's Latin-1 headers."""
        from audible_deals.webhook_client import WebhookClient
        from tests.webhook_server import RecordingServer

        body, headers = format_webhook_payload(
            [hit(monitor="böcher", event="new")], "ntfy"
        )
        server = RecordingServer()
        try:
            WebhookClient(allow_private=True, proxies={}).post(
                server.url, body, headers
            )
            (request,) = server.requests
        finally:
            server.close()

        assert request["body"].decode().startswith("Audible Deals — böcher (1)")
        assert request["headers"]["Title"].startswith("=?UTF-8?B?")

    def test_an_ascii_title_is_sent_as_is(self):
        _, headers = format_webhook_payload([hit()], "ntfy")

        assert headers["Title"] == "Audible Deals (1)"

    def test_status_is_high_priority_with_a_warning_tag(self):
        body, headers = format_webhook_message("it broke", "ntfy", title="T")

        assert body == b"it broke"
        assert (headers["Tags"], headers["Priority"]) == ("warning", "high")


class TestGenericAndMonitors:
    def test_generic_monitor_events_keep_their_own_shape(self):
        events = [{"event": "new", "monitor": "m", "asin": "B0X", "title": "T"}]

        body, _ = format_monitor_webhook_payload(events, "generic", locale="uk")

        assert json.loads(body) == {
            "monitor": "m",
            "locale": "uk",
            "events": events,
            "count": 1,
        }

    def test_other_formats_reuse_the_deal_layout_for_events(self):
        events = [
            {
                "event": "price_drop",
                "monitor": "m",
                "asin": "B0X",
                "title": "T",
                "price": 3.0,
                "target": None,
                "url": "https://www.audible.com/pd/B0X",
            }
        ]

        body, _ = format_monitor_webhook_payload(events, "slack", locale="us")

        text = json.loads(body)["text"]
        assert text.startswith("*Audible Deals — m (1)*") and "[price_drop] " in text

    def test_unknown_formats_still_raise(self):
        for call in (
            lambda: format_webhook_payload([hit()], "teamz"),
            lambda: format_recap_payload(recap_payload(), "teamz"),
            lambda: format_webhook_message("x", "teamz"),
            lambda: provider_for("teamz"),
        ):
            with pytest.raises(ValueError, match="Unknown webhook format"):
                call()
