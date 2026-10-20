"""Where files live: the data directory is decided at call time."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from audible_deals import paths


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake home directory with no data directory and no overrides."""
    monkeypatch.delenv("DEALS_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def test_deals_home_wins_over_everything(home, monkeypatch):
    (home / ".config" / "audible-deals").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    monkeypatch.setenv("DEALS_HOME", str(home / "elsewhere"))

    directory, why = paths.data_dir_with_reason()

    assert directory == home / "elsewhere"
    assert "DEALS_HOME" in why


def test_deals_home_is_expanded_and_read_at_call_time(home, monkeypatch):
    monkeypatch.setenv("DEALS_HOME", "~/deals-data")
    assert paths.data_dir() == home / "deals-data"

    monkeypatch.setenv("DEALS_HOME", str(home / "second"))
    assert paths.data_dir() == home / "second"

    monkeypatch.delenv("DEALS_HOME")
    assert paths.data_dir() == home / ".config" / "audible-deals"


def test_blank_deals_home_is_ignored(home, monkeypatch):
    monkeypatch.setenv("DEALS_HOME", "   ")
    assert paths.data_dir() == home / ".config" / "audible-deals"


def test_an_existing_legacy_directory_beats_xdg(home, monkeypatch):
    legacy = home / ".config" / "audible-deals"
    legacy.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))

    directory, why = paths.data_dir_with_reason()

    assert directory == legacy
    assert "existing" in why


def test_xdg_config_home_is_used_when_there_is_no_legacy_directory(home, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))

    directory, why = paths.data_dir_with_reason()

    assert directory == home / "xdg" / "audible-deals"
    assert "XDG_CONFIG_HOME" in why


def test_default_is_dot_config_even_when_nothing_exists(home):
    directory, why = paths.data_dir_with_reason()

    assert directory == home / ".config" / "audible-deals"
    assert "default" in why


def test_a_legacy_file_is_not_a_legacy_directory(home, monkeypatch):
    (home / ".config").mkdir()
    (home / ".config" / "audible-deals").write_text("not a directory")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))

    assert paths.data_dir() == home / "xdg" / "audible-deals"


def test_appdata_is_the_windows_default(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))

    directory, why = paths.data_dir_with_reason()

    assert directory == home / "AppData" / "Roaming" / "audible-deals"
    assert "APPDATA" in why


def test_windows_keeps_an_existing_legacy_directory(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    (home / ".config" / "audible-deals").mkdir(parents=True)

    assert paths.data_dir() == home / ".config" / "audible-deals"


def test_macos_stays_on_dot_config(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert paths.data_dir() == home / ".config" / "audible-deals"


def test_every_file_lives_in_the_data_directory(tmp_path):
    root = Path(tmp_path)
    assert paths.config_file() == root / "config.json"
    assert paths.profiles_file() == root / "profiles.json"
    assert paths.wishlist_file() == root / "wishlist.json"
    assert paths.monitors_file() == root / "monitors.json"
    assert paths.auth_file() == root / "auth.json"
    assert paths.state_db_file() == root / "state.db"
    assert paths.cache_db_file() == root / "cache.db"
    assert paths.lock_file() == root / ".deals.lock"
    assert paths.migration_lock_file() == root / ".migrate.lock"
    assert paths.track_log_file() == root / "track.log"
    assert paths.legacy_history_dir() == root / "history"


def test_legacy_category_caches_are_found_per_marketplace(tmp_path):
    (tmp_path / "categories_cache.us.json").write_text("{}")
    (tmp_path / "categories_cache.uk.json").write_text("{}")
    (tmp_path / "categories_cache.us.json.migrated").write_text("{}")

    assert [p.name for p in paths.legacy_category_cache_files()] == [
        "categories_cache.uk.json",
        "categories_cache.us.json",
    ]


def test_no_legacy_category_caches_without_a_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("DEALS_HOME", str(tmp_path / "missing"))
    assert paths.legacy_category_cache_files() == []


def test_the_paths_module_imports_nothing_from_the_package():
    source = Path(paths.__file__).read_text()
    assert "audible_deals" not in source.replace("audible-deals", "")


def test_a_legacy_directory_that_is_also_the_default_is_just_the_default(home):
    """On a fresh install the directory exists after the first run; nothing was
    "kept" for anyone: it is where the default would put it anyway."""
    legacy = home / ".config" / "audible-deals"
    legacy.mkdir(parents=True)

    directory, why = paths.data_dir_with_reason()

    assert directory == legacy
    assert why == "default ~/.config/audible-deals"


def test_a_legacy_directory_is_kept_only_when_the_platform_default_differs(
    home, monkeypatch
):
    legacy = home / ".config" / "audible-deals"
    legacy.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))

    directory, why = paths.data_dir_with_reason()

    assert directory == legacy
    assert why == "existing ~/.config/audible-deals, kept so your data does not move"


def test_windows_keeps_a_legacy_directory_with_the_kept_wording(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    (home / ".config" / "audible-deals").mkdir(parents=True)

    _directory, why = paths.data_dir_with_reason()

    assert "kept so your data does not move" in why


def test_xdg_pointing_at_the_legacy_place_is_xdg_not_kept(home, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    (home / ".config" / "audible-deals").mkdir(parents=True)

    _directory, why = paths.data_dir_with_reason()

    assert why == "XDG_CONFIG_HOME is set"


@pytest.mark.parametrize(
    "setup",
    ["override", "legacy_kept", "legacy_default", "xdg", "appdata", "plain"],
)
def test_no_reason_contains_parentheses(home, monkeypatch, setup):
    """The reason is printed inside parentheses: it must not need its own."""
    legacy = home / ".config" / "audible-deals"
    if setup == "override":
        monkeypatch.setenv("DEALS_HOME", str(home / "elsewhere"))
    elif setup == "legacy_kept":
        legacy.mkdir(parents=True)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    elif setup == "legacy_default":
        legacy.mkdir(parents=True)
    elif setup == "xdg":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    elif setup == "appdata":
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))

    _directory, why = paths.data_dir_with_reason()

    assert "(" not in why and ")" not in why
