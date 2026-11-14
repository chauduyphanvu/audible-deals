"""Webhook payload formatting for deal and recap notifications.

Each platform has a formatter function returning (body_str, headers),
registered in ``_WEBHOOK_FORMATTERS`` / ``_RECAP_FORMATTERS``. Supporting a
new platform means adding a formatter and a registry entry.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Callable

from audible_deals.automation_models import CREDIT_PRICE_REASON
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

CREDIT_HIT_LIMIT = 20


def _cap_credit_hits(hits: list[dict]) -> tuple[list[dict], int]:
    """List every target hit and the 20 below-credit hits that save the most.

    Returns the hits to list and how many below-credit hits were left out.
    """
    credit = [h for h in hits if h.get("reason") == CREDIT_PRICE_REASON]
    if not credit:
        return hits, 0
    others = [h for h in hits if h.get("reason") != CREDIT_PRICE_REASON]
    credit.sort(key=lambda h: h["target"] - h["price"], reverse=True)
    return others + credit[:CREDIT_HIT_LIMIT], max(len(credit) - CREDIT_HIT_LIMIT, 0)


def _more_lines(more: int) -> list[str]:
    return [f"and {more} more below your credit price"] if more else []


def _webhook_heading(hits: list[dict]) -> str:
    """Return an event-aware heading while preserving ordinary deal payloads."""
    monitor = hits[0].get("monitor") if hits else None
    return f"Audible Deals — {monitor}" if monitor else "Audible Deals"


def _webhook_item_prefix(hit: dict) -> str:
    return f"[{hit['event']}] " if hit.get("event") else ""


def _webhook_why(hit: dict, currency: str) -> str:
    if hit.get("reason") == CREDIT_PRICE_REASON:
        return f"below your {price_str(hit['target'], currency)} credit"
    return f"target {price_str(hit['target'], currency)}"


def _template_number(value: object) -> float | Decimal:
    try:
        return float(value or 0.0)
    except OverflowError:
        return Decimal(value)


def _render_webhook_template(
    hits: list[dict],
    template: str,
    currency: str,
    extras: dict[str, dict] | None,
    more: int,
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
            )
        except (IndexError, ValueError) as e:
            raise ValueError(
                f"Template format error: {e}. Valid keys: {_TEMPLATE_KEYS}. "
                "Use {{ and }} for literal braces."
            )
    body = "\n".join([*rendered_parts, *_more_lines(more)])
    return body.encode("utf-8"), {"Content-Type": "text/plain; charset=utf-8"}


def _webhook_generic(
    hits: list[dict], currency: str, more: int
) -> tuple[str, dict[str, str]]:
    payload: dict = {"deals": hits, "count": len(hits)}
    if more:
        payload["more_below_credit_price"] = more
    body = _strict_json_dumps(payload, indent=2)
    return body, {"Content-Type": "application/json"}


def _webhook_slack(
    hits: list[dict], currency: str, more: int
) -> tuple[str, dict[str, str]]:
    lines = "\n".join(
        [
            *(
                f"• {_webhook_item_prefix(h)}<{h['url']}|{h['title']}> — {price_str(h['price'], currency)} ({_webhook_why(h, currency)})"
                for h in hits
            ),
            *_more_lines(more),
        ]
    )
    body = _strict_json_dumps(
        {"text": f"*{_webhook_heading(hits)} ({len(hits) + more})*\n{lines}"}
    )
    return body, {"Content-Type": "application/json"}


def _webhook_discord(
    hits: list[dict], currency: str, more: int
) -> tuple[str, dict[str, str]]:
    lines = "\n".join(
        [
            *(
                f"• {_webhook_item_prefix(h)}[{h['title']}](<{h['url']}>) — {price_str(h['price'], currency)} ({_webhook_why(h, currency)})"
                for h in hits
            ),
            *_more_lines(more),
        ]
    )
    body = _strict_json_dumps(
        {"content": f"**{_webhook_heading(hits)} ({len(hits) + more})**\n{lines}"}
    )
    return body, {"Content-Type": "application/json"}


def _webhook_teams(
    hits: list[dict], currency: str, more: int
) -> tuple[str, dict[str, str]]:
    text = "  \n".join(
        [
            *(
                f"• {_webhook_item_prefix(h)}[{h['title']}]({h['url']}) — {price_str(h['price'], currency)} ({_webhook_why(h, currency)})"
                for h in hits
            ),
            *_more_lines(more),
        ]
    )
    body = _strict_json_dumps(
        {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": _webhook_heading(hits),
            "themeColor": "0078D7",
            "title": f"{_webhook_heading(hits)} ({len(hits) + more})",
            "sections": [{"text": text}],
        }
    )
    return body, {"Content-Type": "application/json"}


def _webhook_ntfy(
    hits: list[dict], currency: str, more: int
) -> tuple[str, dict[str, str]]:
    def item(h: dict) -> str:
        why = (
            f", {_webhook_why(h, currency)}"
            if h.get("reason") == CREDIT_PRICE_REASON
            else ""
        )
        return f"• {_webhook_item_prefix(h)}{h['title']} — {currency}{h['price']:.2f}{why} ({h['url']})"

    n = len(hits) + more
    lines = "\n".join([*(item(h) for h in hits), *_more_lines(more)])
    heading = _webhook_heading(hits)
    body = f"{heading} ({n})\n{lines}"
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Title": f"{heading} ({n})",
        "Tags": "book",
        "Priority": "default",
    }
    return body, headers


_WEBHOOK_FORMATTERS: dict[
    str, Callable[[list[dict], str, int], tuple[str, dict[str, str]]]
] = {
    "generic": _webhook_generic,
    "slack": _webhook_slack,
    "discord": _webhook_discord,
    "teams": _webhook_teams,
    "ntfy": _webhook_ntfy,
}


def format_webhook_payload(
    hits: list[dict],
    fmt: str,
    currency: str = "$",
    *,
    template: str | None = None,
    extras: dict[str, dict] | None = None,
) -> tuple[bytes, dict[str, str]]:
    """Format webhook payload for the given platform. Returns (body_bytes, headers)."""
    if template is not None and fmt != "generic":
        raise ValueError("template is incompatible with non-generic fmt")
    formatter = _WEBHOOK_FORMATTERS.get(fmt)
    if formatter is None:
        raise ValueError(f"Unknown webhook format: {fmt!r}")
    hits, more = _cap_credit_hits(hits)
    if template is not None:
        return _render_webhook_template(hits, template, currency, extras, more)
    body, headers = formatter(hits, currency, more)
    return body.encode("utf-8"), headers


def format_monitor_webhook_payload(
    events: list[dict], fmt: str, *, locale: str, currency: str = "$"
) -> tuple[bytes, dict[str, str]]:
    """Format monitor events without changing the wishlist payload contract."""
    if fmt == "generic":
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
        return body.encode("utf-8"), {"Content-Type": "application/json"}
    return format_webhook_payload(events, fmt, currency=currency)


def format_webhook_message(
    text: str, fmt: str, title: str = "Audible Deals"
) -> tuple[bytes, dict[str, str]]:
    """Format a plain status message (e.g. a re-auth warning) for the given platform."""
    if fmt == "slack":
        body = _strict_json_dumps({"text": f"*{title}*\n{text}"})
        headers = {"Content-Type": "application/json"}
    elif fmt == "discord":
        body = _strict_json_dumps({"content": f"**{title}**\n{text}"})
        headers = {"Content-Type": "application/json"}
    elif fmt == "teams":
        body = _strict_json_dumps(
            {
                "@type": "MessageCard",
                "@context": "https://schema.org/extensions",
                "summary": title,
                "themeColor": "D70000",
                "title": title,
                "sections": [{"text": text}],
            }
        )
        headers = {"Content-Type": "application/json"}
    elif fmt == "ntfy":
        body = text
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Title": title,
            "Tags": "warning",
            "Priority": "high",
        }
    elif fmt == "generic":
        body = _strict_json_dumps({"message": text, "title": title})
        headers = {"Content-Type": "application/json"}
    else:
        raise ValueError(f"Unknown webhook format: {fmt!r}")
    return body.encode("utf-8"), headers


def _recap_title(payload: dict) -> str:
    return f"Audible Recap (last {payload.get('days', 0)} days)"


def _recap_drop_lines(payload: dict, currency: str, sep: str = "\n") -> str:
    return sep.join(
        f"• {d['title']} — {currency}{d['old_price']:.2f} -> {currency}{d['new_price']:.2f} (-{d['drop_pct']}%)"
        for d in payload.get("drops", [])[:10]
    )


def _recap_footer(payload: dict) -> str:
    return f"Wishlist at target: {len(payload.get('wishlist_hits', []))}"


def _recap_atl_section(payload: dict, currency: str, sep: str = "\n") -> str:
    atl_hits = payload.get("atl_hits") or []
    if not atl_hits:
        return ""
    atl_lines = sep.join(
        f"• {h['title']} — {currency}{h['price']:.2f}" for h in atl_hits[:10]
    )
    return f"{sep}At all-time low:{sep}{atl_lines}"


def _recap_generic(payload: dict, currency: str) -> tuple[str, dict[str, str]]:
    return _strict_json_dumps(payload, indent=2), {"Content-Type": "application/json"}


def _recap_slack(payload: dict, currency: str) -> tuple[str, dict[str, str]]:
    body = _strict_json_dumps(
        {
            "text": f"*{_recap_title(payload)}*\n{_recap_drop_lines(payload, currency)}\n"
            f"{_recap_footer(payload)}{_recap_atl_section(payload, currency)}"
        }
    )
    return body, {"Content-Type": "application/json"}


def _recap_discord(payload: dict, currency: str) -> tuple[str, dict[str, str]]:
    body = _strict_json_dumps(
        {
            "content": f"**{_recap_title(payload)}**\n{_recap_drop_lines(payload, currency)}\n"
            f"{_recap_footer(payload)}{_recap_atl_section(payload, currency)}"
        }
    )
    return body, {"Content-Type": "application/json"}


def _recap_teams(payload: dict, currency: str) -> tuple[str, dict[str, str]]:
    title_str = _recap_title(payload)
    text = _recap_drop_lines(payload, currency, "  \n")
    atl_section = _recap_atl_section(payload, currency, "  \n")
    body = _strict_json_dumps(
        {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": title_str,
            "themeColor": "0078D7",
            "title": title_str,
            "sections": [{"text": f"{text}  \n{_recap_footer(payload)}{atl_section}"}],
        }
    )
    return body, {"Content-Type": "application/json"}


def _recap_ntfy(payload: dict, currency: str) -> tuple[str, dict[str, str]]:
    title_str = _recap_title(payload)
    body = (
        f"{title_str}\n{_recap_drop_lines(payload, currency)}\n"
        f"{_recap_footer(payload)}{_recap_atl_section(payload, currency)}"
    )
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Title": title_str,
        "Tags": "book",
        "Priority": "default",
    }
    return body, headers


_RECAP_FORMATTERS: dict[str, Callable[[dict, str], tuple[str, dict[str, str]]]] = {
    "generic": _recap_generic,
    "slack": _recap_slack,
    "discord": _recap_discord,
    "teams": _recap_teams,
    "ntfy": _recap_ntfy,
}


def format_recap_payload(
    payload: dict,
    fmt: str,
    currency: str = "$",
) -> tuple[bytes, dict[str, str]]:
    """Format recap payload for the given platform. Returns (body_bytes, headers)."""
    formatter = _RECAP_FORMATTERS.get(fmt)
    if formatter is None:
        raise ValueError(f"Unknown webhook format: {fmt!r}")
    body_str, headers = formatter(payload, currency)
    return body_str.encode("utf-8"), headers
