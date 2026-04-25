"""Validation and parsing utilities for audible-deals.

General-purpose helpers that are used by CLI commands but have no dependency
on the click command tree itself.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.parse

import click

from audible_deals.constants import _ASIN_RE


def validate_asin(asin: str) -> None:
    """Validate that an ASIN is alphanumeric and won't cause path traversal."""
    if not _ASIN_RE.fullmatch(asin):
        raise click.BadParameter(f"Invalid ASIN format: {asin!r}")


def validate_webhook_url(url: str) -> None:
    """Validate webhook URL: must be http(s) and must not resolve to private IPs."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise click.BadParameter(
            f"Webhook URL must use http:// or https://, got {parsed.scheme!r}",
            param_hint="'--webhook'",
        )
    hostname = parsed.hostname
    if not hostname:
        raise click.BadParameter(
            "Webhook URL must include a host",
            param_hint="'--webhook'",
        )
    try:
        addrinfos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise click.BadParameter(
            f"Cannot resolve webhook host {hostname!r}: {e}",
            param_hint="'--webhook'",
        )
    for _family, _type, _proto, _canonname, sockaddr in addrinfos:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise click.BadParameter(
                f"Webhook URL resolves to non-public address {ip}",
                param_hint="'--webhook'",
            )


_NAME_STOPWORDS = frozenset({
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "my",
    "no", "not", "how", "why", "what", "all", "new", "old", "red", "dark",
})


def looks_like_person_name(query: str) -> bool:
    """Return True if query looks like a 2-3 word person name (each word Title-cased)."""
    words = query.strip().split()
    if len(words) < 2 or len(words) > 3:
        return False
    if any(w.lower() in _NAME_STOPWORDS for w in words):
        return False
    return all(w[0].isupper() for w in words)


def parse_interval(value: str) -> int:
    """Parse an interval string into seconds. Accepts '30m', '2h', '1h30m', '90s', or a plain number (minutes)."""
    raw = value
    value = value.strip().lower()
    if value.isdigit():
        total = int(value) * 60
    else:
        total = 0
        for match in re.finditer(r"(\d+)\s*(h|m|s)", value):
            n, unit = int(match.group(1)), match.group(2)
            if unit == "h":
                total += n * 3600
            elif unit == "m":
                total += n * 60
            else:
                total += n
        # Reject input with unrecognized characters
        remainder = re.sub(r"\d+\s*(h|m|s)", "", value).strip()
        if remainder:
            raise click.BadParameter(f"Cannot parse interval '{raw}'. Use e.g. '30m', '2h', '1h30m'.")
    if total <= 0:
        raise click.BadParameter(f"Interval must be positive. Use e.g. '30m', '2h', '1h30m'.")
    return total
