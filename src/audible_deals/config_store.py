"""Persistence for global config, saved search profiles, and notify cooldown state."""

from __future__ import annotations

import contextlib
import logging
import math

import click

from audible_deals import paths
from audible_deals.constants import LOCALE_DOMAIN, WEBHOOK_FORMATS
from audible_deals.filter_specs import (
    CONFIG_FILTER_SCHEMA,
    SPEC_BY_KEY,
    config_bounds,
    config_key_aliases,
)
from audible_deals.locking import advisory_lock
from audible_deals.storage import ensure_updatable, load_json_file, save_json_file
from audible_deals.validation import validate_finite_number

logger = logging.getLogger(__name__)

# Config keys that are not search filters. The filter keys (and their types)
# come from the filter registry so the CLI flags and the config cannot drift.
_EXTRA_CONFIG_SCHEMA: dict[str, type] = {
    "locale": str,
    "credit_price": float,
    "webhook": str,
    "webhook_format": str,
    "webhook_headers": list,
}
CONFIG_SCHEMA: dict[str, type] = {**CONFIG_FILTER_SCHEMA, **_EXTRA_CONFIG_SCHEMA}

# Mirror the ranges the equivalent CLI options enforce so config set and the
# flags agree. (lo, hi); None means unbounded on that side.
_NUMERIC_BOUNDS: dict[str, tuple[float | None, float | None]] = {
    **config_bounds(),
    "credit_price": (0, None),
}


def load_profiles() -> dict[str, dict]:
    return load_json_file(paths.profiles_file(), dict, "profiles")


def save_profiles(profiles: dict[str, dict]) -> None:
    save_json_file(paths.profiles_file(), profiles, "profiles")


def load_monitors() -> dict[str, dict]:
    return load_json_file(paths.monitors_file(), dict, "monitors")


def load_monitors_for_update() -> dict[str, dict]:
    """Load monitors for a read-modify-write, refusing to replace a corrupt file."""
    ensure_updatable(paths.monitors_file(), dict, "monitors")
    return load_monitors()


def save_monitors(monitors: dict[str, dict]) -> None:
    save_json_file(paths.monitors_file(), monitors, "monitors")


def load_monitor_state() -> dict:
    state = load_json_file(paths.monitor_state_file(), dict, "monitor state")
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
    save_json_file(paths.monitor_state_file(), state, "monitor state")


def load_config() -> dict:
    return load_json_file(paths.config_file(), dict, "config")


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
                integer=CONFIG_SCHEMA[key] is int,
            )
        except ValueError as exc:
            errors.append(str(exc))
    return errors


def save_config(cfg: dict) -> None:
    save_json_file(paths.config_file(), cfg, "config")


def _state_lock(path):
    return advisory_lock(path.with_name(f".{path.name}.lock"), wait=True)


@contextlib.contextmanager
def config_transaction():
    """Lock, load, mutate, and atomically save the global config."""
    with _state_lock(paths.config_file()):
        ensure_updatable(paths.config_file(), dict, "config")
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
    with _state_lock(paths.profiles_file()):
        ensure_updatable(paths.profiles_file(), dict, "profiles")
        profiles = load_profiles()
        yield profiles
        save_profiles(profiles)


def load_notify_state() -> dict:
    return load_json_file(paths.notify_state_file(), dict, "notify state")


def save_notify_state(state: dict) -> None:
    save_json_file(paths.notify_state_file(), state, "notify state")


def load_track_state() -> dict:
    return load_json_file(paths.track_state_file(), dict, "track state")


def save_track_state(state: dict) -> None:
    save_json_file(paths.track_state_file(), state, "track state")


def coerce_config_value(key: str, raw: str):
    """Coerce a raw string value to the type declared in CONFIG_SCHEMA."""
    typ = CONFIG_SCHEMA[key]
    if typ is bool:
        if raw.lower() in ("true", "1", "yes"):
            return True
        elif raw.lower() in ("false", "0", "no"):
            return False
        raise click.ClickException(
            f"Invalid boolean value for '{key}': {raw!r}. Use true/false."
        )
    spec = SPEC_BY_KEY.get(key)
    if spec is not None and spec.valid is not None:
        if raw not in spec.valid:
            raise click.ClickException(
                f"Invalid {key} value '{raw}'. Valid: {', '.join(sorted(spec.valid))}"
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
            ) from e
        return [raw]
    try:
        result = typ(raw)
    except (ValueError, TypeError) as e:
        raise click.ClickException(
            f"Invalid value for '{key}' (expected {typ.__name__}): {e}"
        ) from e
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
            ) from exc
    return result


def validate_config_key(key: str) -> str:
    """Normalize and validate a config key. Returns the snake_case key or raises."""
    norm = key.replace("-", "_")
    norm = config_key_aliases().get(norm, norm)
    if norm not in CONFIG_SCHEMA:
        valid = ", ".join(sorted(k.replace("_", "-") for k in CONFIG_SCHEMA))
        raise click.ClickException(f"Unknown config key '{key}'. Valid keys: {valid}")
    return norm
