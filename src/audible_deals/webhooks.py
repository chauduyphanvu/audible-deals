"""Webhook payload formatting for deal, recap and status notifications.

Every destination is one :class:`Provider`: a class that knows how to format
the three things we send (deals, a recap, a status message) for that service,
including the escaping its markup needs. Supporting a new service means adding
a provider and listing it in ``_PROVIDERS`` (and in ``constants.WEBHOOK_FORMATS``).

The module-level ``format_*`` functions are the entry points. They take the
configured format name and, when known, the webhook URL: ``teams`` normally
means a Power Automate Workflows webhook (Adaptive Card), but a URL on the
retired Office 365 connector hosts keeps the MessageCard it expects, and
``teams-legacy`` forces it.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import urllib.parse
from decimal import Decimal
from typing import Any

from audible_deals.presentation.common import price_str

logger = logging.getLogger(__name__)


def _strict_json_dumps(value, **kwargs) -> str:
    return json.dumps(value, allow_nan=False, **kwargs)


def parse_webhook_headers(
    raw: tuple[str, ...] | list[str], *, strict: bool
) -> dict[str, str]:
    """Parse 'Name: Value' strings into a headers dict.

    strict=True raises ValueError on malformed entry / empty name / empty value /
    Content-Type override. strict=False logs a warning and skips bad entries.
    """
    result: dict[str, str] = {}
    for item in raw:
        if ":" not in item:
            msg = f"--webhook-header {item!r}: must be in 'Name: Value' format"
            if strict:
                raise ValueError(msg)
            logger.warning("Skipping invalid webhook_headers entry: %r", item)
            continue
        name, _, value = item.partition(":")
        name = name.strip()
        value = value.strip()
        if not name:
            msg = f"--webhook-header {item!r}: header name must not be empty"
            if strict:
                raise ValueError(msg)
            logger.warning("Skipping invalid webhook_headers entry: %r", item)
            continue
        if not value:
            msg = f"--webhook-header {item!r}: header value must not be empty"
            if strict:
                raise ValueError(msg)
            logger.warning("Skipping invalid webhook_headers entry: %r", item)
            continue
        if name.lower() == "content-type":
            msg = "--webhook-header: Content-Type is set automatically and cannot be overridden"
            if strict:
                raise ValueError(msg)
            logger.warning("Skipping invalid webhook_headers entry: %r", item)
            continue
        result[name] = value
    return result


_TEMPLATE_KEYS = "title, price, target, url, currency, asin, discount_pct"


def _webhook_item_prefix(hit: dict) -> str:
    return f"[{hit['event']}] " if hit.get("event") else ""


def _template_number(value: Any) -> float | Decimal:
    try:
        return float(value or 0.0)
    except OverflowError:
        return Decimal(value)


def _render_webhook_template(
    hits: list[dict],
    template: str,
    currency: str,
    extras: dict[str, dict] | None,
) -> tuple[bytes, dict[str, str]]:
    rendered_parts: list[str] = []
    for h in hits:
        extra = (extras or {}).get(h.get("asin", ""), {})
        mapping = {
            "title": h.get("title", ""),
            "price": float(h.get("price") or 0.0),
            "target": _template_number(h.get("target")),
            "url": h.get("url", ""),
            "currency": extra.get("currency", currency),
            "asin": h.get("asin", ""),
            "discount_pct": float(extra.get("discount_pct") or 0.0),
        }
        try:
            rendered_parts.append(template.format_map(mapping))
        except KeyError as e:
            raise ValueError(
                f"Template references unknown key {{{e.args[0]}}}. Valid keys: {_TEMPLATE_KEYS}."
            ) from e
        except (IndexError, ValueError) as e:
            raise ValueError(
                f"Template format error: {e}. Valid keys: {_TEMPLATE_KEYS}. "
                "Use {{ and }} for literal braces."
            ) from e
    body = "\n".join(rendered_parts)
    return body.encode("utf-8"), {"Content-Type": "text/plain; charset=utf-8"}


# ---------------------------------------------------------------------------
# Escaping: titles are untrusted text going into each service's markup
# ---------------------------------------------------------------------------

_ZERO_WIDTH_SPACE = "​"
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_~|>#\[\]()<])")
_SLACK_FORMATTING = re.compile(r"([*_~`])")


def slack_text(value: object) -> str:
    """Escape *value* for Slack's mrkdwn (``&``, ``<`` and ``>`` are the control characters).

    Slack has no escape for ``*``, ``_``, ``~`` or the backtick, which would turn
    part of a title bold, italic, struck-through or monospaced; a zero-width
    space after each one keeps them literal.
    """
    text = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _SLACK_FORMATTING.sub(r"\1" + _ZERO_WIDTH_SPACE, text)


def slack_link(url: str, label: object) -> str:
    """``<url|label>``; a ``|`` in the label would end it early, so it becomes ``¦``."""
    safe_url = (
        str(url)
        .replace("&", "&amp;")
        .replace("<", "%3C")
        .replace(">", "%3E")
        .replace("|", "%7C")
        .replace(" ", "%20")
    )
    return f"<{safe_url}|{slack_text(label).replace('|', '¦')}>"


def markdown_text(value: object) -> str:
    """Backslash-escape the characters CommonMark-style markup (Discord, Teams) acts on."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", str(value))


def markdown_url(url: str) -> str:
    return str(url).replace(" ", "%20").replace(">", "%3E").replace("<", "%3C")


def _header_text(value: str) -> str:
    """A header value: plain when ASCII, otherwise an RFC 2047 encoded word.

    HTTP headers are Latin-1 on the wire, so an em dash in a title would fail the
    request; ntfy decodes encoded words in its ``Title`` header.
    """
    if value.isascii():
        return value
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"=?UTF-8?B?{encoded}?="


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

Payload = tuple[str, dict[str, str]]
_JSON_HEADERS = {"Content-Type": "application/json"}

#: Hosts of the retired Office 365 connectors, which still expect a MessageCard.
_TEAMS_CONNECTOR_HOST = re.compile(
    r"(\.webhook\.office\.com|^outlook\.office(365)?\.com)$", re.IGNORECASE
)


def _price(value: object, currency: str) -> str:
    return price_str(value, currency)  # type: ignore[arg-type]


def _recap_lines(payload: dict, currency: str) -> list[tuple[str, str]]:
    """``(title, "$old -> $new (-pct%)")`` for the largest drops."""
    return [
        (
            d["title"],
            f"{currency}{d['old_price']:.2f} -> {currency}{d['new_price']:.2f} "
            f"(-{d['drop_pct']}%)",
        )
        for d in payload.get("drops", [])[:10]
    ]


def _recap_atl(payload: dict, currency: str) -> list[tuple[str, str]]:
    return [
        (h["title"], f"{currency}{h['price']:.2f}")
        for h in (payload.get("atl_hits") or [])[:10]
    ]


class Provider:
    """One destination's three payloads. Subclasses override the markup hooks."""

    name = ""

    # -- text hooks (default: no markup) -----------------------------------
    def bold(self, text: str) -> str:
        return text

    def text(self, value: object) -> str:
        """*value* made safe to place in this provider's markup."""
        return str(value)

    def link(self, url: str, label: object) -> str:
        return f"{self.text(label)} ({url})"

    # -- content (shared across providers) ---------------------------------
    def deal_line(self, hit: dict, currency: str) -> str:
        return (
            f"• {_webhook_item_prefix(hit)}{self.link(hit['url'], hit['title'])}"
            f" — {_price(hit['price'], currency)}"
            f" (target {_price(hit['target'], currency)})"
        )

    def heading(self, hits: list[dict]) -> str:
        """The title line, already safe for this provider's markup."""
        monitor = hits[0].get("monitor") if hits else None
        base = f"Audible Deals — {self.text(monitor)}" if monitor else "Audible Deals"
        return f"{base} ({len(hits)})"

    def recap_title(self, payload: dict) -> str:
        return f"Audible Recap (last {payload.get('days', 0)} days)"

    def recap_footer(self, payload: dict) -> str:
        return f"Wishlist at target: {len(payload.get('wishlist_hits', []))}"

    # -- payloads ----------------------------------------------------------
    def deals(self, hits: list[dict], currency: str) -> Payload:
        raise NotImplementedError

    def events(self, events: list[dict], *, locale: str, currency: str) -> Payload:
        return self.deals(events, currency)

    def recap(self, payload: dict, currency: str) -> Payload:
        raise NotImplementedError

    def status(self, text: str, title: str) -> Payload:
        raise NotImplementedError


class _TextProvider(Provider):
    """Providers whose body is one text message (Slack, Discord, ntfy, Teams)."""

    def message(self, heading: str, lines: list[str], footer: str = "") -> str:
        """*heading* must already be safe (see :meth:`Provider.heading`)."""
        parts = [self.bold(heading), *lines]
        if footer:
            parts.append(footer)
        return "\n".join(parts)

    def recap_text(self, payload: dict, currency: str) -> str:
        lines = [
            f"• {self.text(title)} — {detail}"
            for title, detail in _recap_lines(payload, currency)
        ]
        atl = [
            f"• {self.text(title)} — {detail}"
            for title, detail in _recap_atl(payload, currency)
        ]
        body = self.message(
            self.recap_title(payload), lines, self.recap_footer(payload)
        )
        if atl:
            body += "\nAt all-time low:\n" + "\n".join(atl)
        return body


class GenericProvider(Provider):
    name = "generic"

    def deals(self, hits: list[dict], currency: str) -> Payload:
        return _strict_json_dumps({"deals": hits, "count": len(hits)}, indent=2), dict(
            _JSON_HEADERS
        )

    def events(self, events: list[dict], *, locale: str, currency: str) -> Payload:
        monitor = events[0].get("monitor", "") if events else ""
        body = _strict_json_dumps(
            {
                "monitor": monitor,
                "locale": locale,
                "events": events,
                "count": len(events),
            },
            indent=2,
        )
        return body, dict(_JSON_HEADERS)

    def recap(self, payload: dict, currency: str) -> Payload:
        return _strict_json_dumps(payload, indent=2), dict(_JSON_HEADERS)

    def status(self, text: str, title: str) -> Payload:
        return _strict_json_dumps({"message": text, "title": title}), dict(
            _JSON_HEADERS
        )


class SlackProvider(_TextProvider):
    name = "slack"

    def bold(self, text: str) -> str:
        return f"*{text}*"

    def text(self, value: object) -> str:
        return slack_text(value)

    def link(self, url: str, label: object) -> str:
        return slack_link(url, label)

    def deals(self, hits: list[dict], currency: str) -> Payload:
        text = self.message(
            self.heading(hits), [self.deal_line(h, currency) for h in hits]
        )
        return _strict_json_dumps({"text": text}), dict(_JSON_HEADERS)

    def recap(self, payload: dict, currency: str) -> Payload:
        return _strict_json_dumps({"text": self.recap_text(payload, currency)}), dict(
            _JSON_HEADERS
        )

    def status(self, text: str, title: str) -> Payload:
        body = f"{self.bold(self.text(title))}\n{self.text(text)}"
        return _strict_json_dumps({"text": body}), dict(_JSON_HEADERS)


class DiscordProvider(_TextProvider):
    name = "discord"

    #: Titles are untrusted: no @everyone, @here, user or role pings from content.
    _NO_PINGS: dict[str, list[str]] = {"parse": []}

    def bold(self, text: str) -> str:
        return f"**{text}**"

    def text(self, value: object) -> str:
        return markdown_text(value)

    def link(self, url: str, label: object) -> str:
        return f"[{markdown_text(label)}](<{markdown_url(url)}>)"

    def _content(self, content: str) -> Payload:
        body = _strict_json_dumps(
            {"content": content, "allowed_mentions": self._NO_PINGS}
        )
        return body, dict(_JSON_HEADERS)

    def deals(self, hits: list[dict], currency: str) -> Payload:
        return self._content(
            self.message(
                self.heading(hits), [self.deal_line(h, currency) for h in hits]
            )
        )

    def recap(self, payload: dict, currency: str) -> Payload:
        return self._content(self.recap_text(payload, currency))

    def status(self, text: str, title: str) -> Payload:
        return self._content(f"{self.bold(self.text(title))}\n{self.text(text)}")


class TeamsLegacyProvider(_TextProvider):
    """MessageCard, for Office 365 connector webhooks (retired by Microsoft)."""

    name = "teams-legacy"

    def text(self, value: object) -> str:
        return markdown_text(value)

    def link(self, url: str, label: object) -> str:
        return f"[{markdown_text(label)}]({markdown_url(url)})"

    def _card(self, title: str, text: str, color: str) -> Payload:
        body = _strict_json_dumps(
            {
                "@type": "MessageCard",
                "@context": "https://schema.org/extensions",
                "summary": title,
                "themeColor": color,
                "title": title,
                "sections": [{"text": text}],
            }
        )
        return body, dict(_JSON_HEADERS)

    def deals(self, hits: list[dict], currency: str) -> Payload:
        # MessageCard markdown needs two trailing spaces for a line break.
        text = "  \n".join(self.deal_line(h, currency) for h in hits)
        return self._card(self.heading(hits), text, "0078D7")

    def recap(self, payload: dict, currency: str) -> Payload:
        lines = [
            f"• {self.text(title)} — {detail}"
            for title, detail in _recap_lines(payload, currency)
        ]
        lines.append(self.recap_footer(payload))
        atl = [
            f"• {self.text(title)} — {detail}"
            for title, detail in _recap_atl(payload, currency)
        ]
        if atl:
            lines.extend(["At all-time low:", *atl])
        return self._card(self.recap_title(payload), "  \n".join(lines), "0078D7")

    def status(self, text: str, title: str) -> Payload:
        # MessageCard markdown needs two trailing spaces for a line break.
        return self._card(title, self.text(text).replace("\n", "  \n"), "D70000")


class TeamsProvider(TeamsLegacyProvider):
    """Adaptive Card, for Power Automate Workflows webhooks (the replacement)."""

    name = "teams"

    @staticmethod
    def _adaptive(blocks: list[dict]) -> Payload:
        body = _strict_json_dumps(
            {
                "type": "message",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "contentUrl": None,
                        "content": {
                            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                            "type": "AdaptiveCard",
                            "version": "1.2",
                            "body": blocks,
                            "msteams": {"width": "Full"},
                        },
                    }
                ],
            }
        )
        return body, dict(_JSON_HEADERS)

    @staticmethod
    def _heading_block(text: str, *, attention: bool = False) -> dict:
        """*text* must already be safe for Teams markdown."""
        block = {
            "type": "TextBlock",
            "text": text,
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        }
        if attention:
            block["color"] = "Attention"
        return block

    @staticmethod
    def _block(text: str) -> dict:
        return {"type": "TextBlock", "text": text, "wrap": True, "spacing": "Small"}

    def deals(self, hits: list[dict], currency: str) -> Payload:
        return self._adaptive(
            [
                self._heading_block(self.heading(hits)),
                *(self._block(self.deal_line(h, currency)) for h in hits),
            ]
        )

    def recap(self, payload: dict, currency: str) -> Payload:
        blocks = [self._heading_block(self.recap_title(payload))]
        blocks += [
            self._block(f"• {self.text(title)} — {detail}")
            for title, detail in _recap_lines(payload, currency)
        ]
        blocks.append(self._block(self.recap_footer(payload)))
        atl = _recap_atl(payload, currency)
        if atl:
            blocks.append(self._block("At all-time low:"))
            blocks += [
                self._block(f"• {self.text(title)} — {detail}") for title, detail in atl
            ]
        return self._adaptive(blocks)

    def status(self, text: str, title: str) -> Payload:
        return self._adaptive(
            [
                self._heading_block(self.text(title), attention=True),
                # Teams renders a single newline in a TextBlock as a space.
                self._block(self.text(text).replace("\n", "\n\n")),
            ]
        )


class NtfyProvider(Provider):
    name = "ntfy"

    def _headers(self, title: str, tags: str, priority: str) -> dict[str, str]:
        return {
            "Content-Type": "text/plain; charset=utf-8",
            "Title": _header_text(title),
            "Tags": tags,
            "Priority": priority,
        }

    def deal_line(self, hit: dict, currency: str) -> str:
        return (
            f"• {_webhook_item_prefix(hit)}{hit['title']}"
            f" — {_price(hit['price'], currency)}"
            f" (target {_price(hit['target'], currency)}) {hit['url']}"
        )

    def deals(self, hits: list[dict], currency: str) -> Payload:
        heading = self.heading(hits)
        lines = "\n".join(self.deal_line(h, currency) for h in hits)
        return f"{heading}\n{lines}", self._headers(heading, "book", "default")

    def recap(self, payload: dict, currency: str) -> Payload:
        title = self.recap_title(payload)
        lines = [f"• {t} — {d}" for t, d in _recap_lines(payload, currency)]
        body = "\n".join([title, *lines, self.recap_footer(payload)])
        atl = [f"• {t} — {d}" for t, d in _recap_atl(payload, currency)]
        if atl:
            body += "\nAt all-time low:\n" + "\n".join(atl)
        return body, self._headers(title, "book", "default")

    def status(self, text: str, title: str) -> Payload:
        return text, self._headers(title, "warning", "high")


_PROVIDERS: dict[str, Provider] = {
    provider.name: provider
    for provider in (
        GenericProvider(),
        SlackProvider(),
        DiscordProvider(),
        TeamsProvider(),
        TeamsLegacyProvider(),
        NtfyProvider(),
    )
}


def is_teams_connector_url(url: str | None) -> bool:
    """Whether *url* is an (retired) Office 365 connector webhook."""
    if not url:
        return False
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if not _TEAMS_CONNECTOR_HOST.search(host):
        return False
    # outlook.office.com also serves other things; only its /webhook paths count.
    return host.lower().endswith(".webhook.office.com") or parsed.path.startswith(
        "/webhook"
    )


def provider_for(fmt: str, url: str | None = None) -> Provider:
    """The provider for the configured format and, when known, the webhook URL."""
    if fmt == "teams" and is_teams_connector_url(url):
        fmt = "teams-legacy"
    provider = _PROVIDERS.get(fmt)
    if provider is None:
        raise ValueError(f"Unknown webhook format: {fmt!r}")
    return provider


def _encoded(payload: Payload) -> tuple[bytes, dict[str, str]]:
    body, headers = payload
    return body.encode("utf-8"), headers


def format_webhook_payload(
    hits: list[dict],
    fmt: str,
    currency: str = "$",
    *,
    template: str | None = None,
    extras: dict[str, dict] | None = None,
    url: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    """Format webhook payload for the given platform. Returns (body_bytes, headers)."""
    if template is not None:
        if fmt != "generic":
            raise ValueError("template is incompatible with non-generic fmt")
        return _render_webhook_template(hits, template, currency, extras)
    return _encoded(provider_for(fmt, url).deals(hits, currency))


def format_monitor_webhook_payload(
    events: list[dict],
    fmt: str,
    *,
    locale: str,
    currency: str = "$",
    url: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    """Format monitor events without changing the wishlist payload contract."""
    return _encoded(
        provider_for(fmt, url).events(events, locale=locale, currency=currency)
    )


def format_webhook_message(
    text: str, fmt: str, title: str = "Audible Deals", *, url: str | None = None
) -> tuple[bytes, dict[str, str]]:
    """Format a plain status message (e.g. a re-auth warning) for the given platform."""
    return _encoded(provider_for(fmt, url).status(text, title))


def format_recap_payload(
    payload: dict,
    fmt: str,
    currency: str = "$",
    *,
    url: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    """Format recap payload for the given platform. Returns (body_bytes, headers)."""
    return _encoded(provider_for(fmt, url).recap(payload, currency))
