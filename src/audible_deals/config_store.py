"""Persistence for global config, saved search profiles, and notify cooldown state."""

from __future__ import annotations

import click

from audible_deals import constants
from audible_deals.constants import (
    _CONFIG_SCHEMA,
    ALL_SORT_OPTIONS,
    LOCALE_DOMAIN,
    WEBHOOK_FORMATS,
)
from audible_deals.storage import load_json_file, save_json_file


def load_profiles() -> dict[str, dict]:
    return load_json_file(constants.PROFILES_FILE, dict, "profiles")


def save_profiles(profiles: dict[str, dict]) -> None:
    save_json_file(constants.PROFILES_FILE, profiles, "profiles")


def load_config() -> dict:
    return load_json_file(constants.CONFIG_FILE, dict, "config")


def save_config(cfg: dict) -> None:
    save_json_file(constants.CONFIG_FILE, cfg, "config")


def load_notify_state() -> dict:
    return load_json_file(constants.NOTIFY_STATE_FILE, dict, "notify state")


def save_notify_state(state: dict) -> None:
    save_json_file(constants.NOTIFY_STATE_FILE, state, "notify state")


def coerce_config_value(key: str, raw: str):
    """Coerce a raw string value to the type declared in _CONFIG_SCHEMA."""
    typ = _CONFIG_SCHEMA[key]
    if typ is bool:
        if raw.lower() in ("true", "1", "yes"):
            return True
        elif raw.lower() in ("false", "0", "no"):
            return False
        raise click.ClickException(
            f"Invalid boolean value for '{key}': {raw!r}. Use true/false."
        )
    if key == "sort":
        if raw not in ALL_SORT_OPTIONS:
            raise click.ClickException(
                f"Invalid sort value '{raw}'. Valid: {', '.join(sorted(ALL_SORT_OPTIONS))}"
            )
        return raw
    if key == "locale":
        if raw not in LOCALE_DOMAIN:
            raise click.ClickException(
                f"Invalid locale '{raw}'. Valid: {', '.join(sorted(LOCALE_DOMAIN))}"
            )
        return raw
    if key == "webhook_format":
        if raw not in WEBHOOK_FORMATS:
            raise click.ClickException(
                f"Invalid webhook format '{raw}'. Valid: {', '.join(WEBHOOK_FORMATS)}"
            )
        return raw
    if key == "webhook":
        from audible_deals.validation import validate_webhook_url

        validate_webhook_url(raw)
        return raw
    if key == "webhook_headers":
        from audible_deals.webhooks import parse_webhook_headers

        try:
            parse_webhook_headers((raw,), strict=True)
        except ValueError as e:
            raise click.ClickException(
                f"Invalid webhook header {raw!r}: {e}. Use 'Name: Value' format."
            )
        return [raw]
    try:
        return typ(raw)
    except (ValueError, TypeError) as e:
        raise click.ClickException(
            f"Invalid value for '{key}' (expected {typ.__name__}): {e}"
        )


def validate_config_key(key: str) -> str:
    """Normalize and validate a config key. Returns the snake_case key or raises."""
    norm = key.replace("-", "_")
    if norm not in _CONFIG_SCHEMA:
        valid = ", ".join(sorted(k.replace("_", "-") for k in _CONFIG_SCHEMA))
        raise click.ClickException(f"Unknown config key '{key}'. Valid keys: {valid}")
    return norm
