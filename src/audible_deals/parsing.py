"""Parsers for user-supplied value strings."""

from __future__ import annotations

import re

import click


def parse_series_position(position: str) -> float:
    """Parse a series position string into a float for sorting.

    Handles plain numbers ("2"), decimals ("2.5"), ranges ("1-3"), and
    prefixed strings ("Book 2"). Returns float('inf') for unparseable input
    so those items sort last.
    """
    m = re.search(r"\d+(?:\.\d+)?", position or "")
    return float(m.group()) if m else float("inf")


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
            raise click.BadParameter(
                f"Cannot parse interval '{raw}'. Use e.g. '30m', '2h', '1h30m'."
            )
    if total <= 0:
        raise click.BadParameter(
            "Interval must be positive. Use e.g. '30m', '2h', '1h30m'."
        )
    return total
