"""Input validation helpers shared by CLI commands."""

from __future__ import annotations

import ipaddress
import math
import socket
import urllib.parse

import click

from audible_deals.constants import _ASIN_RE


class FiniteFloatRange(click.FloatRange):
    """A float option that rejects NaN and infinity before range checks."""

    name = "float"

    def __init__(self, minimum: float | None = None, maximum: float | None = None):
        super().__init__(min=minimum, max=maximum)

    def convert(self, value, param, ctx):
        try:
            result = float(value)
        except (TypeError, ValueError):
            self.fail(f"{value!r} is not a valid floating-point value", param, ctx)
        if not math.isfinite(result):
            self.fail(f"{value!r} must be a finite number", param, ctx)
        return super().convert(result, param, ctx)


NONNEGATIVE_FLOAT = FiniteFloatRange(0)
RATING_FLOAT = FiniteFloatRange(0, 5)
NONNEGATIVE_INT = click.IntRange(min=0)


def validate_finite_number(
    name: str,
    value: object,
    minimum: float | None = None,
    maximum: float | None = None,
    *,
    integer: bool = False,
) -> None:
    """Reject malformed persisted numeric settings before a scan uses them."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Invalid stored setting '{name}': expected a number")
    if integer and not isinstance(value, int):
        raise ValueError(f"Invalid stored setting '{name}': expected an integer")
    if not math.isfinite(value):
        raise ValueError(f"Invalid stored setting '{name}': value must be finite")
    if minimum is not None and value < minimum:
        raise ValueError(
            f"Invalid stored setting '{name}': expected at least {minimum}"
        )
    if maximum is not None and value > maximum:
        raise ValueError(f"Invalid stored setting '{name}': expected at most {maximum}")


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
        if not ip.is_global:
            raise click.BadParameter(
                f"Webhook URL resolves to non-public address {ip}",
                param_hint="'--webhook'",
            )
