"""Persistence for global config, saved search profiles, and notify cooldown state."""

from __future__ import annotations

import contextlib
import logging
import math

import click

from audible_deals import constants
from audible_deals.constants import (
    _CONFIG_SCHEMA,
    ALL_SORT_OPTIONS,
    LOCALE_DOMAIN,
    WEBHOOK_FORMATS,
)
from audible_deals.storage import load_json_file, save_json_file
from audible_deals.locking import advisory_lock
from audible_deals.validation import validate_finite_number

logger = logging.getLogger(__name__)

# Mirror the ranges the equivalent CLI options enforce so config set and the
# flags agree. (lo, hi); None means unbounded on that side.
_NUMERIC_BOUNDS: dict[str, tuple[float | None, float | None]] = {
    "pages": (1, None),
    "min_discount": (0, 100),
    "limit": (0, None),
    "max_price": (0, None),
    "max_pph": (0, None),
    "credit_price": (0, None),
    "min_rating": (0, 5),
    "min_ratings": (0, None),
    "min_hours": (0, None),
}


def load_profiles() -> dict[str, dict]:
    return load_json_file(constants.PROFILES_FILE, dict, "profiles")


def save_profiles(profiles: dict[str, dict]) -> None:
    save_json_file(constants.PROFILES_FILE, profiles, "profiles")


def load_monitors() -> dict[str, dict]:
    return load_json_file(constants.MONITORS_FILE, dict, "monitors")


def save_monitors(monitors: dict[str, dict]) -> None:
    save_json_file(constants.MONITORS_FILE, monitors, "monitors")


def load_monitor_state() -> dict:
    state = load_json_file(constants.MONITOR_STATE_FILE, dict, "monitor state")
    if not state:
        return {"version": 1, "monitors": {}}
    if state.get("version") != 1:
        logger.warning(
            "monitor state has unsupported version %r; resetting", state.get("version")
        )
        return {"version": 1, "monitors": {}}
    if "monitors" not in state:
        logger.warning("monitor state is missing monitors; repairing")
        state["monitors"] = {}
    elif not isinstance(state["monitors"], dict):
        logger.warning("monitor state monitors is malformed; repairing")
        state["monitors"] = {}
    return state


def save_monitor_state(state: dict) -> None:
    state["version"] = 1
    state.setdefault("monitors", {})
    save_json_file(constants.MONITOR_STATE_FILE, state, "monitor state")


def load_config() -> dict:
    return load_json_file(constants.CONFIG_FILE, dict, "config")


def config_numeric_errors(config: dict) -> list[str]:
    """Return invalid persisted numeric config values without blocking recovery."""
    errors = []
    for key, (minimum, maximum) in _NUMERIC_BOUNDS.items():
        value = config.get(key)
        if value is None:
            continue
        try:
            validate_finite_number(
                key,
                value,
                minimum,
                maximum,
                integer=_CONFIG_SCHEMA[key] is int,
            )
        except ValueError as exc:
            errors.append(str(exc))
    return errors


def save_config(cfg: dict) -> None:
    save_json_file(constants.CONFIG_FILE, cfg, "config")


def _state_lock(path):
    return advisory_lock(path.with_name(f".{path.name}.lock"), wait=True)


@contextlib.contextmanager
def config_transaction():
    """Lock, load, mutate, and atomically save the global config."""
    with _state_lock(constants.CONFIG_FILE):
        config = load_config()
        yield config
        for key, value in list(config.items()):
            if isinstance(value, float) and not math.isfinite(value):
                logger.warning("Removing non-finite config value for %s", key)
                del config[key]
        save_config(config)


@contextlib.contextmanager
def profiles_transaction():
    """Lock, load, mutate, and atomically save saved profiles."""
    with _state_lock(constants.PROFILES_FILE):
        profiles = load_profiles()
        yield profiles
        save_profiles(profiles)


def load_notify_state() -> dict:
    return load_json_file(constants.NOTIFY_STATE_FILE, dict, "notify state")


def save_notify_state(state: dict) -> None:
    save_json_file(constants.NOTIFY_STATE_FILE, state, "notify state")


def load_track_state() -> dict:
    return load_json_file(constants.TRACK_STATE_FILE, dict, "track state")


def save_track_state(state: dict) -> None:
    save_json_file(constants.TRACK_STATE_FILE, state, "track state")


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
        result = typ(raw)
    except (ValueError, TypeError) as e:
        raise click.ClickException(
            f"Invalid value for '{key}' (expected {typ.__name__}): {e}"
        )
    bounds = _NUMERIC_BOUNDS.get(key)
    if bounds is not None:
        lo, hi = bounds
        try:
            validate_finite_number(key, result, lo, hi)
        except ValueError as exc:
            if "finite" in str(exc):
                raise click.ClickException(
                    f"Value for '{key}' must be a finite number."
                ) from None
            rng = f"{lo}" if hi is None else f"{lo}-{hi}"
            raise click.ClickException(
                f"Value for '{key}' out of range: {result} (expected >= {rng})"
                if hi is None
                else f"Value for '{key}' out of range: {result} (expected {rng})"
            )
    return result


def validate_config_key(key: str) -> str:
    """Normalize and validate a config key. Returns the snake_case key or raises."""
    norm = key.replace("-", "_")
    if norm == "max_price_per_hour":
        return "max_pph"
    if norm not in _CONFIG_SCHEMA:
        valid = ", ".join(sorted(k.replace("_", "-") for k in _CONFIG_SCHEMA))
        raise click.ClickException(f"Unknown config key '{key}'. Valid keys: {valid}")
    return norm
