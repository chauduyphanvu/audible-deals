"""`deals cache`: where data lives, what is stored, and clearing the disposable part."""

from __future__ import annotations

import json

from click.testing import CliRunner

from audible_deals import cache_store, paths
from audible_deals.cli import cli
from audible_deals.price_history import load_price_history
from audible_deals.results_cache import save_dismissed_asins, save_seen_asins
from tests.conftest import make_product, seed_history


def run(*args):
    return CliRunner().invoke(cli, ["cache", *args])


def test_info_names_the_data_directory_and_both_databases(tmp_config):
    result = run("info")

    assert result.exit_code == 0, result.output
    assert str(tmp_config) in result.output
    assert "DEALS_HOME is set" in result.output
    assert "state.db" in result.output
    assert "cache.db" in result.output
    assert "never cleared" in result.output


def test_bare_cache_is_info(tmp_config):
    assert run().output == run("info").output


def test_info_counts_what_is_stored(tmp_config):
    seed_history("B00CACHE01", [("2026-01-01", 3.0), ("2026-01-02", 2.0)])
    save_seen_asins({"A1", "A2", "A3"})
    save_dismissed_asins({"D1"})
    cache_store.put("categories", "us", [{"id": "1", "name": "A"}])

    result = run("info", "--json")
    report = json.loads(result.stdout)

    assert report["data_dir"] == str(tmp_config)
    assert report["state_db"]["rows"]["price_history"] == 2
    assert report["state_db"]["rows"]["seen"] == 3
    assert report["state_db"]["rows"]["dismissed"] == 1
    assert report["state_db"]["schema"] >= 1
    assert report["state_db"]["bytes"] > 0
    assert report["cache_db"]["namespaces"]["categories"]["entries"] == 1
    assert report["state_db"]["json_import"]["renames_pending"] == []


def test_info_json_keeps_stdout_clean_when_a_migration_runs(tmp_config):
    (tmp_config / "seen_asins.json").write_text(json.dumps(["A1"]))

    result = run("info", "--json")

    report = json.loads(result.stdout)  # parses: the notice went to stderr
    assert report["state_db"]["rows"]["seen"] == 1
    assert "Moved your saved data" in result.stderr


def test_info_text_lists_the_cached_namespaces(tmp_config):
    cache_store.put("taste", "profile", {"x": 1})
    result = run("info")
    assert "taste" in result.output
    assert "newest" in result.output


def test_clear_empties_only_the_cache(tmp_config):
    seed_history("B00CACHE02", [("2026-01-01", 3.0)])
    save_seen_asins({"A1"})
    cache_store.put("categories", "us", [1])
    cache_store.put("taste", "profile", {"x": 1})

    result = run("clear")

    assert result.exit_code == 0, result.output
    assert "Cleared 2 cached item(s)" in result.output
    assert cache_store.get("categories", "us") is None
    assert cache_store.get("taste", "profile") is None
    assert load_price_history("B00CACHE02")  # records untouched
    from audible_deals.results_cache import load_seen_asins

    assert load_seen_asins() == {"A1"}


def test_clearing_an_empty_cache_says_so(tmp_config):
    assert "already empty" in run("clear").output


def test_a_damaged_cache_database_is_replaced_by_clear(tmp_config):
    paths.cache_db_file().write_bytes(b"garbage" * 600)

    result = run("clear")

    assert result.exit_code == 0, result.output
    assert "already empty" in result.output
    cache_store.put("ns", "k", 1)
    assert cache_store.get("ns", "k") == 1


def test_cache_is_listed_in_help_under_setup(tmp_config):
    result = CliRunner().invoke(cli, ["--help"])
    assert "cache" in result.output
    assert "Inspect and clear local databases" in result.output


def test_record_prices_still_work_after_a_clear(tmp_config):
    from audible_deals.price_history import record_prices

    cache_store.put("ns", "k", 1)
    run("clear")
    record_prices([make_product(asin="B00AFTER01", price=2.0)])
    assert load_price_history("B00AFTER01")
