"""Input validation helpers shared by CLI commands."""

from __future__ import annotations

import datetime
import math
import socket
import urllib.parse
from typing import TYPE_CHECKING

import click

from audible_deals.constants import _ASIN_RE

if TYPE_CHECKING:
    from audible_deals.webhook_guard import Resolver


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


class IsoDate(click.ParamType):
    """A ``YYYY-MM-DD`` date, normalized to canonical form; empty means unset."""

    name = "date"

    def get_metavar(self, param, ctx=None):
        return "YYYY-MM-DD"

    def convert(self, value, param, ctx):
        if value is None or value == "":
            return value
        try:
            return datetime.date.fromisoformat(str(value)).isoformat()
        except ValueError:
            self.fail(f"invalid date {value!r} (expected YYYY-MM-DD)", param, ctx)


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


def _config_allows_private_webhooks() -> bool:
    ctx = click.get_current_context(silent=True)
    config = ctx.obj.get("config") if ctx is not None and ctx.obj else None
    return isinstance(config, dict) and config.get("webhook_allow_private") is True


def validate_webhook_url(
    url: str,
    *,
    allow_private: bool | None = None,
    resolver: Resolver | None = None,
) -> None:
    """Validate a webhook URL: http(s), with a host that resolves to public addresses.

    *allow_private* lets targets on your own network through; left as ``None`` it
    follows the ``webhook-allow-private`` config setting of the running command.
    The same check runs again, on the address actually connected to, when the
    webhook is sent (see :mod:`audible_deals.webhook_guard`).
    """
    from audible_deals.webhook_guard import (
        AddressGuard,
        BlockedAddressError,
        system_resolver,
    )

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
        port = parsed.port  # ValueError for ":abc" and for numbers above 65535
    except ValueError:
        port = -1
    if port is not None and not 1 <= port <= 65535:
        raise click.BadParameter(
            "Webhook URL has an invalid port (use a number from 1 to 65535)",
            param_hint="'--webhook'",
        )
    if allow_private is None:
        allow_private = _config_allows_private_webhooks()
    try:
        AddressGuard(
            allow_private=allow_private, resolver=resolver or system_resolver
        ).addresses(hostname, port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as e:
        raise click.BadParameter(
            f"Cannot resolve webhook host {hostname!r}: {e}",
            param_hint="'--webhook'",
        ) from e
    except BlockedAddressError as e:
        raise click.BadParameter(str(e), param_hint="'--webhook'") from e
