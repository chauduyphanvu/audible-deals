"""Every filter ``find`` accepts can be saved in a profile and frozen in a monitor.

Before the filter registry, ``profile save`` lacked --max-effective-price,
--hist-below, --min-price-drop, --require-history and --released-after/-before,
and ``monitor add`` lacked several more, so monitors built from profiles could
not use them. These tests drive the behavior end to end.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from audible_deals import monitor_service, paths
from audible_deals.automation_models import MonitorDefinition
from audible_deals.cli import cli
from audible_deals.config_store import load_monitors, load_profiles, save_profiles
from audible_deals.monitor_service import MonitorRuntime, scan_monitor
from audible_deals.results_cache import load_result_session
from tests.conftest import make_product


def _catalog(mock_client, *products):
    mock_client.search_pages.return_value = iter([(list(products), 1, len(products))])


def _find(*args):
    return CliRunner().invoke(
        cli,
        ["find", "--pages", "1", "--all-languages", "--max-price", "20", *args],
    )


def _asins(result):
    assert result.exit_code == 0, result.output
    return [item["asin"] for item in json.loads(result.output)]


def _products():
    return [
        make_product(
            asin="OLDBOOK",
            price=3.0,
            release_date="2020-01-01",
            series_name="",
            series_position="",
        ),
        make_product(
            asin="NEWCHEAP",
            price=3.0,
            release_date="2024-06-01",
            series_name="",
            series_position="",
        ),
        make_product(
            asin="NEWPRICEY",
            price=9.0,
            list_price=20.0,
            release_date="2024-06-01",
            series_name="",
            series_position="",
        ),
    ]


@pytest.fixture
def credit_config(tmp_config):
    paths.config_file().write_text(json.dumps({"credit_price": 6.0}))
    return tmp_config


# ---------------------------------------------------------------------------
# Profiles carry the formerly missing filters into find / search
# ---------------------------------------------------------------------------


def test_profile_saves_and_applies_release_date_and_effective_price(
    mock_client, credit_config
):
    save = CliRunner().invoke(
        cli,
        [
            "profile",
            "save",
            "recent",
            "--released-after",
            "20240101",
            "--max-effective-price",
            "4",
        ],
    )
    assert save.exit_code == 0, save.output
    assert load_profiles()["recent"] == {
        "released_after": "2024-01-01",  # normalized when parsed
        "max_effective_price": 4.0,
    }
    _catalog(mock_client, *_products())

    result = _find("--profile", "recent", "--json", "-n", "0")

    # OLDBOOK predates the window; NEWPRICEY's cheaper-of-cash-or-credit is 6.
    assert _asins(result) == ["NEWCHEAP"]


def test_profile_applies_to_search_too(mock_client, credit_config):
    save_profiles({"recent": {"released_before": "2021-01-01"}})
    _catalog(mock_client, *_products())

    result = CliRunner().invoke(
        cli,
        [
            "search",
            "x",
            "--pages",
            "1",
            "--all-languages",
            "--profile",
            "recent",
            "--json",
            "-n",
            "0",
        ],
    )

    assert _asins(result) == ["OLDBOOK"]


def test_cli_flag_beats_the_profile_value(mock_client, credit_config):
    save_profiles({"p": {"released_after": "2024-01-01"}})
    _catalog(mock_client, *_products())

    result = _find(
        "--profile", "p", "--released-after", "2019-01-01", "--json", "-n", "0"
    )

    assert set(_asins(result)) == {"OLDBOOK", "NEWCHEAP", "NEWPRICEY"}


def test_profile_history_filters_show_in_dry_run_and_can_be_switched_off(tmp_config):
    save_profiles(
        {"hist": {"hist_below": 30, "min_price_drop": 20.0, "require_history": True}}
    )

    shown = CliRunner().invoke(cli, ["find", "--profile", "hist", "--dry-run"])
    assert shown.exit_code == 0, shown.output
    assert "hist-below=30" in shown.output
    assert "min-price-drop=20.0" in shown.output
    assert "require-history=yes" in shown.output

    off = CliRunner().invoke(
        cli,
        ["find", "--profile", "hist", "--no-require-history", "--dry-run"],
    )
    assert off.exit_code == 0, off.output
    assert "require-history" not in off.output
    assert "hist-below=30" in off.output


def test_profile_require_history_without_a_history_filter_is_a_usage_error(
    tmp_config,
):
    save_profiles({"lonely": {"require_history": True}})

    result = CliRunner().invoke(cli, ["find", "--profile", "lonely", "--dry-run"])
    assert result.exit_code == 2
    assert "--require-history requires" in result.output

    # A flag on the command line can complete the profile.
    fixed = CliRunner().invoke(
        cli, ["find", "--profile", "lonely", "--hist-below", "40", "--dry-run"]
    )
    assert fixed.exit_code == 0, fixed.output


def test_profile_inverted_release_window_is_a_usage_error(tmp_config):
    # Hand-edited profile: each date is valid, the pair is not.
    save_profiles(
        {"bad": {"released_after": "2025-02-01", "released_before": "2025-01-01"}}
    )

    result = CliRunner().invoke(cli, ["search", "x", "--profile", "bad", "--dry-run"])

    assert result.exit_code == 2
    assert "cannot be later" in result.output


def test_profile_exclude_seen_applies_and_can_be_switched_off(mock_client, tmp_config):
    save_profiles({"fresh": {"exclude_seen": True}})
    paths.seen_asins_file().write_text(json.dumps(["NEWCHEAP"]))
    _catalog(mock_client, *_products())
    seen_hidden = _find("--profile", "fresh", "--json", "-n", "0")
    assert "NEWCHEAP" not in _asins(seen_hidden)

    _catalog(mock_client, *_products())
    shown = _find("--profile", "fresh", "--no-exclude-seen", "--json", "-n", "0")
    assert "NEWCHEAP" in _asins(shown)


def test_new_filters_are_recorded_in_the_result_recipe(mock_client, credit_config):
    _catalog(mock_client, *_products())

    result = _find(
        "--hist-below",
        "30",
        "--min-price-drop",
        "5",
        "--released-after",
        "2024-01-01",
        "--max-effective-price",
        "7",
        "--exclude-seen",
        "--json",
    )

    assert result.exit_code == 0, result.output
    recipe = load_result_session().current_recipe
    assert recipe.hist_below == 30
    assert recipe.min_price_drop == 5.0
    assert recipe.released_after == "2024-01-01"
    assert recipe.max_effective_price == 7.0
    assert recipe.exclude_seen is True
    # ... which `deals last` can refine without any API call.
    again = CliRunner().invoke(
        cli, ["last", "--released-after", "2024-01-01", "--json", "-n", "0"]
    )
    assert "OLDBOOK" not in _asins(again)


def test_config_default_for_max_effective_price_applies(tmp_config):
    CliRunner().invoke(cli, ["config", "set", "max-effective-price", "6"])

    shown = CliRunner().invoke(cli, ["find", "--dry-run"])
    assert "max-effective-price=6.0" in shown.output

    overridden = CliRunner().invoke(
        cli, ["find", "--max-effective-price", "3", "--dry-run"]
    )
    assert "max-effective-price=3.0" in overridden.output


def test_release_dates_have_no_global_default(tmp_config):
    result = CliRunner().invoke(cli, ["config", "set", "released-after", "2024-01-01"])

    assert result.exit_code == 1
    assert "Unknown config key" in result.output


# ---------------------------------------------------------------------------
# Monitors
# ---------------------------------------------------------------------------


@pytest.fixture
def monitor_env(tmp_config):
    return tmp_config


def test_monitor_from_a_profile_freezes_the_new_filters(monitor_env):
    save_profiles(
        {
            "rich": {
                "hist_below": 30,
                "max_effective_price": 4.0,
                "released_after": "2024-01-01",
                "exclude_seen": True,
            }
        }
    )

    result = CliRunner().invoke(cli, ["monitor", "add", "m", "--profile", "rich"])

    assert result.exit_code == 0, result.output
    assert "exclude-seen does not apply to monitors" in result.output
    settings = load_monitors()["m"]["settings"]
    assert settings["hist_below"] == 30
    assert settings["max_effective_price"] == 4.0
    assert settings["released_after"] == "2024-01-01"
    assert settings["exclude_seen"] is False


def test_monitor_add_accepts_the_formerly_missing_flags(monitor_env):
    result = CliRunner().invoke(
        cli,
        [
            "monitor",
            "add",
            "m",
            "--query",
            "dune",
            "--max-price-per-hour",
            "1.5",
            "--max-effective-price",
            "5",
            "--hist-below",
            "40",
            "--min-price-drop",
            "10",
            "--require-history",
            "--released-after",
            "20240101",
            "--released-before",
            "2025-01-01",
            "--exclude-genre",
            "erotica",
            "--series",
            "Dune",
            "--publisher",
            "Macmillan",
            "--all-languages",
        ],
    )

    assert result.exit_code == 0, result.output
    settings = load_monitors()["m"]["settings"]
    assert settings["max_pph"] == 1.5
    assert settings["max_effective_price"] == 5.0
    assert settings["hist_below"] == 40
    assert settings["min_price_drop"] == 10.0
    assert settings["require_history"] is True
    assert settings["released_after"] == "2024-01-01"
    assert settings["released_before"] == "2025-01-01"
    assert settings["exclude_genre"] == ["erotica"]
    assert settings["series"] == "Dune"
    assert settings["publisher"] == "Macmillan"
    assert settings["all_languages"] is True
    assert settings["language"] == ""


def test_monitor_add_rejects_incomplete_history_filters(monitor_env):
    result = CliRunner().invoke(
        cli, ["monitor", "add", "m", "--query", "x", "--require-history"]
    )

    assert result.exit_code == 2
    assert "--require-history requires" in result.output
    assert load_monitors() == {}


def test_monitor_add_rejects_a_bad_date_at_parse_time(monitor_env):
    result = CliRunner().invoke(
        cli, ["monitor", "add", "m", "--query", "x", "--released-after", "soon"]
    )

    assert result.exit_code == 2
    assert "invalid date" in result.output


class _Client:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _scan(settings, monkeypatch, *, credit_price=None):
    monkeypatch.setattr(
        monitor_service, "execute_catalog_scan", lambda *a, **kw: _products()
    )
    runtime = MonitorRuntime(
        get_client=lambda locale: _Client(),
        resolve_categories=lambda *args: ("", "", set()),
        resolve_skip_asins=lambda *args: set(),
        progress=lambda *args: _noop(),
        credit_price=credit_price,
    )
    definition = MonitorDefinition.from_dict(
        {
            "version": 1,
            "name": "m",
            "enabled": True,
            "locale": "us",
            "mode": "find",
            "query": "",
            "settings": {
                "pages": 1,
                "limit": None,
                "sort": "price",
                "language": "english",
                "max_price": 20.0,
                **settings,
            },
        }
    )
    return [item["asin"] for item in scan_monitor(definition, runtime)]


class _noop:
    def __enter__(self):
        return lambda *args: None

    def __exit__(self, *args):
        return False


def test_scan_applies_frozen_release_and_effective_price_filters(monkeypatch):
    assert _scan({}, monkeypatch) == ["OLDBOOK", "NEWCHEAP", "NEWPRICEY"]
    assert _scan({"released_after": "2024-01-01"}, monkeypatch) == [
        "NEWCHEAP",
        "NEWPRICEY",
    ]
    assert _scan({"released_before": "2021-01-01"}, monkeypatch) == ["OLDBOOK"]
    # Only the configured credit price lets the $9 book qualify as <= 4 effective.
    assert _scan({"max_effective_price": 4.0}, monkeypatch) == ["OLDBOOK", "NEWCHEAP"]
    assert _scan({"max_effective_price": 4.0}, monkeypatch, credit_price=3.0) == [
        "OLDBOOK",
        "NEWCHEAP",
        "NEWPRICEY",
    ]


def test_legacy_monitor_settings_without_new_keys_still_load(monitor_env):
    legacy = {
        "version": 1,
        "name": "old",
        "enabled": True,
        "locale": "us",
        "mode": "search",
        "query": "dune",
        "settings": {"pages": 1, "sort": "relevance", "max_price": 5.0},
    }

    definition = monitor_service.validated_monitor_definition(legacy)

    settings = monitor_service.settings_from_dict(definition.settings)
    assert settings.hist_below is None and settings.released_after == ""


def test_doctor_flags_an_invalid_stored_release_date(tmp_config, mock_client):
    save_profiles({"broken": {"released_after": "soon"}, "fine": {"hist_below": 20}})

    result = CliRunner().invoke(cli, ["doctor"])

    assert "Profile settings valid" in result.output
    assert "broken" in result.output and "released_after" in result.output
    assert "Unknown profile keys" in result.output


def test_monitor_test_uses_the_configured_credit_price(monitor_env, monkeypatch):
    import audible_deals.cli.monitor as monitor_module

    paths.config_file().write_text(json.dumps({"credit_price": 3.0}))
    added = CliRunner().invoke(
        cli,
        ["monitor", "add", "m", "--query", "x", "--max-effective-price", "4"],
    )
    assert added.exit_code == 0, added.output
    monkeypatch.setattr(monitor_module, "_get_client", lambda locale: _Client())
    monkeypatch.setattr(
        monitor_module, "_resolve_categories", lambda *args: ("", "", set())
    )
    monkeypatch.setattr(monitor_module, "_resolve_skip_asins", lambda *args: set())
    monkeypatch.setattr(
        monitor_service, "execute_catalog_scan", lambda *a, **kw: _products()
    )

    result = CliRunner().invoke(cli, ["monitor", "test", "m"])

    assert result.exit_code == 0, result.output
    # With a 3.00 credit the $9 book costs 3.00 effective, so it qualifies.
    assert "NEWPRICEY" in result.output
