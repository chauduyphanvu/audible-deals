"""Webhook payload formatting for deal and recap notifications.

Each platform has a formatter function returning (body_str, headers),
registered in ``_WEBHOOK_FORMATTERS`` / ``_RECAP_FORMATTERS``. Supporting a
new platform means adding a formatter and a registry entry.
"""

from __future__ import annotations

import json
from typing import Callable

_TEMPLATE_KEYS = "title, price, target, url, currency, asin, discount_pct"


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
            "target": float(h.get("target") or 0.0),
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
    body = "\n".join(rendered_parts)
    return body.encode("utf-8"), {"Content-Type": "text/plain; charset=utf-8"}


def _webhook_generic(hits: list[dict], currency: str) -> tuple[str, dict[str, str]]:
    body = json.dumps({"deals": hits, "count": len(hits)}, indent=2)
    return body, {"Content-Type": "application/json"}


def _webhook_slack(hits: list[dict], currency: str) -> tuple[str, dict[str, str]]:
    lines = "\n".join(
        f"• <{h['url']}|{h['title']}> — {currency}{h['price']:.2f} (target {currency}{h['target']:.2f})"
        for h in hits
    )
    body = json.dumps({"text": f"*Audible Deals ({len(hits)})*\n{lines}"})
    return body, {"Content-Type": "application/json"}


def _webhook_discord(hits: list[dict], currency: str) -> tuple[str, dict[str, str]]:
    lines = "\n".join(
        f"• [{h['title']}](<{h['url']}>) — {currency}{h['price']:.2f} (target {currency}{h['target']:.2f})"
        for h in hits
    )
    body = json.dumps({"content": f"**Audible Deals ({len(hits)})**\n{lines}"})
    return body, {"Content-Type": "application/json"}


def _webhook_teams(hits: list[dict], currency: str) -> tuple[str, dict[str, str]]:
    text = "  \n".join(
        f"• [{h['title']}]({h['url']}) — {currency}{h['price']:.2f} (target {currency}{h['target']:.2f})"
        for h in hits
    )
    body = json.dumps(
        {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": "Audible Deals",
            "themeColor": "0078D7",
            "title": f"Audible Deals ({len(hits)})",
            "sections": [{"text": text}],
        }
    )
    return body, {"Content-Type": "application/json"}


def _webhook_ntfy(hits: list[dict], currency: str) -> tuple[str, dict[str, str]]:
    n = len(hits)
    lines = "\n".join(
        f"• {h['title']} — {currency}{h['price']:.2f} ({h['url']})" for h in hits
    )
    body = f"Audible Deals ({n})\n{lines}"
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Title": f"Audible Deals ({n})",
        "Tags": "book",
        "Priority": "default",
    }
    return body, headers


_WEBHOOK_FORMATTERS: dict[
    str, Callable[[list[dict], str], tuple[str, dict[str, str]]]
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
    if template is not None:
        if fmt != "generic":
            raise ValueError("template is incompatible with non-generic fmt")
        return _render_webhook_template(hits, template, currency, extras)
    formatter = _WEBHOOK_FORMATTERS.get(fmt)
    if formatter is None:
        raise ValueError(f"Unknown webhook format: {fmt!r}")
    body, headers = formatter(hits, currency)
    return body.encode("utf-8"), headers


def format_webhook_message(
    text: str, fmt: str, title: str = "Audible Deals"
) -> tuple[bytes, dict[str, str]]:
    """Format a plain status message (e.g. a re-auth warning) for the given platform."""
    if fmt == "slack":
        body = json.dumps({"text": f"*{title}*\n{text}"})
        headers = {"Content-Type": "application/json"}
    elif fmt == "discord":
        body = json.dumps({"content": f"**{title}**\n{text}"})
        headers = {"Content-Type": "application/json"}
    elif fmt == "teams":
        body = json.dumps(
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
        body = json.dumps({"message": text, "title": title})
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
    return json.dumps(payload, indent=2), {"Content-Type": "application/json"}


def _recap_slack(payload: dict, currency: str) -> tuple[str, dict[str, str]]:
    body = json.dumps(
        {
            "text": f"*{_recap_title(payload)}*\n{_recap_drop_lines(payload, currency)}\n"
            f"{_recap_footer(payload)}{_recap_atl_section(payload, currency)}"
        }
    )
    return body, {"Content-Type": "application/json"}


def _recap_discord(payload: dict, currency: str) -> tuple[str, dict[str, str]]:
    body = json.dumps(
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
    body = json.dumps(
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
