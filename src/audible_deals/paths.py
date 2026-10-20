"""Where audible-deals keeps its files, resolved at call time.

The data directory is chosen on every call from the environment, never at
import time, so tests and the scheduler can point a process at an isolated
directory by setting ``DEALS_HOME``. Resolution order:

1. ``DEALS_HOME`` (an explicit override; used as given, created on demand).
2. An existing ``~/.config/audible-deals`` (the location every earlier release
   used, so nobody's data moves).
3. ``$XDG_CONFIG_HOME/audible-deals`` when ``XDG_CONFIG_HOME`` is set, else
   ``~/.config/audible-deals`` (macOS keeps using ``~/.config`` too).
4. On Windows, ``%APPDATA%\\audible-deals`` when the legacy directory does not
   exist.

This module is a dependency-free leaf (no package imports) so any module can
use it on the startup path.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIRNAME = "audible-deals"
ENV_HOME = "DEALS_HOME"


def _legacy_dir() -> Path:
    return Path.home() / ".config" / APP_DIRNAME


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


def data_dir_with_reason() -> tuple[Path, str]:
    """Return ``(directory, why)`` for the active data directory."""
    override = _env_path(ENV_HOME)
    if override is not None:
        return override, f"{ENV_HOME} is set"
    legacy = _legacy_dir()
    if legacy.is_dir():
        return (
            legacy,
            "existing ~/.config/audible-deals (kept so your data does not move)",
        )
    xdg = _env_path("XDG_CONFIG_HOME")
    if xdg is not None:
        return xdg / APP_DIRNAME, "XDG_CONFIG_HOME is set"
    if sys.platform == "win32":
        appdata = _env_path("APPDATA")
        if appdata is not None:
            return appdata / APP_DIRNAME, "%APPDATA% (Windows default)"
    return legacy, "default ~/.config/audible-deals"


def data_dir() -> Path:
    return data_dir_with_reason()[0]


# -- user-owned JSON files (config, profiles, wishlist, monitors, auth) -------


def config_file() -> Path:
    return data_dir() / "config.json"


def profiles_file() -> Path:
    return data_dir() / "profiles.json"


def wishlist_file() -> Path:
    return data_dir() / "wishlist.json"


def monitors_file() -> Path:
    return data_dir() / "monitors.json"


def auth_file() -> Path:
    return data_dir() / "auth.json"


# -- databases and operational files ------------------------------------------


def state_db_file() -> Path:
    """Records: price history, seen/dismissed, result session, tracking state."""
    return data_dir() / "state.db"


def cache_db_file() -> Path:
    """Disposable caches (categories, taste profile); safe to delete."""
    return data_dir() / "cache.db"


def lock_file() -> Path:
    return data_dir() / ".deals.lock"


def migration_lock_file() -> Path:
    return data_dir() / ".migrate.lock"


def track_log_file() -> Path:
    return data_dir() / "track.log"


# -- retired JSON stores, read only by the one-time migration -----------------


def legacy_history_dir() -> Path:
    return data_dir() / "history"


def legacy_category_cache_files() -> list[Path]:
    directory = data_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob("categories_cache*.json"))
