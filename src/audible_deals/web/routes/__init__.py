"""Route blueprints for the web UI."""

from __future__ import annotations

from flask import current_app

from audible_deals.client import LOCALE_CURRENCY


def get_locale() -> str:
    """Return the active Audible marketplace locale."""
    return current_app.config.get("LOCALE", "us")


def currency() -> str:
    """Return the currency symbol for the active locale."""
    return LOCALE_CURRENCY.get(get_locale(), "$")
