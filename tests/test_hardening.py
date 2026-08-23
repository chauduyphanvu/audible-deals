from __future__ import annotations

import csv
import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from click.testing import CliRunner

from audible_deals import constants
from audible_deals.cli import cli
from audible_deals.config_store import (
    config_transaction,
    load_config,
    load_profiles,
    profiles_transaction,
    save_config,
)
from audible_deals.presentation.products import display_products
from audible_deals.presentation.terminal import safe_text
from audible_deals.price_history import load_price_history, record_prices
from audible_deals.product import Product, parse_product
from audible_deals.serialization import export_products
from audible_deals.track_service import append_run, run_history
from tests.conftest import make_product


def test_terminal_text_and_product_markup_are_rendered_literally(capsys):
    title = "[red]literal[/red]\x1b[2J\u202eright"
    assert safe_text(title) == "[red]literal[/red]\\x1b[2J\\u202eright"

    display_products([make_product(title=title)], title="[blue]query[/blue]")
    output = capsys.readouterr().out

    assert "[red]literal[/red]" in output
    assert "[blue]query[/blue]" in output
    assert "\x1b" not in output
    assert "\u202e" not in output
    assert "\\x1b" in output
    assert "\\u202e" in output


def test_profile_config_and_monitor_markup_is_literal(tmp_config):
    constants.CONFIG_FILE.write_text(json.dumps({"narrator": "[red]literal[/red]"}))
    constants.PROFILES_FILE.write_text(
        json.dumps({"[green]profile[/green]": {"genre": "[blue]genre[/blue]"}})
    )
    constants.MONITORS_FILE.write_text(
        json.dumps(
            {
                "[yellow]monitor[/yellow]": {
                    "version": 1,
                    "name": "[yellow]monitor[/yellow]",
                    "enabled": True,
                    "locale": "us",
                    "mode": "find",
                    "query": "",
                    "settings": {},
                }
            }
        )
    )
    runner = CliRunner()

    config_result = runner.invoke(cli, ["config", "list"])
    profile_result = runner.invoke(cli, ["profile", "list"])
    monitor_result = runner.invoke(cli, ["monitor", "list"])

    assert "[red]literal[/red]" in config_result.output
    assert "[green]profile[/green]" in profile_result.output
    assert "[blue]genre[/blue]" in profile_result.output
    assert "[yellow]monitor[/yellow]" in monitor_result.output


def test_product_csv_neutralizes_dangerous_text_cells(tmp_path):
    product = Product(
        asin="=asin",
        title=" =title",
        subtitle="+subtitle",
        authors=["-author"],
        narrators=["@narrator"],
        publisher="\tpublisher",
        categories=["\ncategory"],
        category_ids=[" =category-id"],
        series_name="+series",
        series_position="-position",
        series_asin="@series-asin",
        language=" =language",
        release_date="\rrelease",
    )
    path = tmp_path / "products.csv"

    export_products([product], path)

    with path.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    for key in (
        "asin",
        "title",
        "subtitle",
        "authors",
        "narrators",
        "publisher",
        "categories",
        "category_ids",
        "series_name",
        "series_position",
        "series_asin",
        "language",
        "release_date",
        "full_title",
    ):
        assert row[key].startswith("'"), key


def test_wishlist_csv_neutralizes_text_cells(tmp_config):
    constants.WISHLIST_FILE.write_text(
        json.dumps(
            [
                {
                    "asin": "B001",
                    "title": " =title",
                    "max_price": 5,
                    "added": "\tadded",
                },
                {
                    "type": "author",
                    "author": "+author",
                    "max_price": 5,
                    "added": "\nadded",
                },
            ]
        )
    )
    path = tmp_config / "wishlist.csv"

    result = CliRunner().invoke(cli, ["wishlist", "list", "-o", str(path)])

    assert result.exit_code == 0, result.output
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["title"].startswith("'")
    assert rows[0]["added"].startswith("'")
    assert rows[1]["author"].startswith("'")
    assert rows[1]["added"].startswith("'")


@pytest.mark.parametrize(
    "source",
    (
        "not json",
        "[]",
        '{"access_token": "at"}',
        '{"access_token": "at", "refresh_token": "rt", "locale_code": "xx"}',
    ),
)
def test_import_auth_errors_preserve_existing_auth(tmp_config, source):
    original = b'{"existing": true}\n'
    constants.AUTH_FILE.write_bytes(original)
    source_path = tmp_config / "source.json"
    source_path.write_text(source)

    result = CliRunner().invoke(cli, ["import-auth", str(source_path)])

    assert result.exit_code != 0
    assert "Could not import auth" in result.output
    assert constants.AUTH_FILE.read_bytes() == original


def test_product_parser_skips_bad_nested_values_and_nonfinite_numbers():
    product = parse_product(
        {
            "asin": "B001",
            "title": "Book",
            "authors": [None, "bad", {"name": "Good"}],
            "narrators": [{"name": 3}],
            "category_ladders": [None, {"ladder": [None, {"id": "c", "name": "C"}]}],
            "series": [None],
            "plans": [None, {"plan_name": 4}],
            "rating": {"overall_distribution": []},
            "runtime_length_min": math.inf,
            "price": {"lowest_price": {"base": "NaN"}},
            "list_price": math.inf,
        }
    )

    assert product.authors == ["Good"]
    assert product.narrators == []
    assert product.categories == ["C"]
    assert product.length_minutes == 0
    assert product.rating == 0
    assert product.num_ratings == 0
    assert product.price is None
    assert product.list_price is None
    with pytest.raises(ValueError, match="ASIN"):
        parse_product({"title": "Book"})
    with pytest.raises(ValueError, match="title"):
        parse_product({"asin": "B001"})


def test_product_parser_neutralizes_out_of_range_numbers():
    product = parse_product(
        {
            "asin": "B001",
            "title": "Book",
            "runtime_length_min": -1,
            "price": {
                "lowest_price": {"base": -2},
                "list_price": {"base": -3},
            },
            "rating": {
                "overall_distribution": {
                    "display_average_rating": 6,
                    "num_ratings": -4,
                }
            },
        }
    )

    assert product.price is None
    assert product.list_price is None
    assert product.length_minutes == 0
    assert product.rating == 0
    assert product.num_ratings == 0


def test_strict_state_writer_rejects_nonfinite_numbers(tmp_config):
    with pytest.raises(ValueError, match="JSON compliant"):
        save_config({"max_price": math.nan})


def test_nonfinite_history_is_ignored_without_rewriting(tmp_config):
    constants.HISTORY_DIR.mkdir()
    path = constants.HISTORY_DIR / "B001.json"
    original = (
        b'{"marketplaces":{"us":['
        b'{"date":"2026-01-01","price":NaN,"title":"Bad"},'
        b'{"date":"2026-01-02","price":2.0,"title":"Good"}]}}'
    )
    path.write_bytes(original)

    entries = load_price_history("B001")
    record_prices([make_product(asin="B002", price=math.inf)])

    assert entries == [{"date": "2026-01-02", "price": 2.0, "title": "Good"}]
    assert path.read_bytes() == original
    assert not (constants.HISTORY_DIR / "B002.json").exists()


def test_history_write_discards_poisoned_prices_in_other_marketplaces(tmp_config):
    constants.HISTORY_DIR.mkdir()
    path = constants.HISTORY_DIR / "B000TEST01.json"
    path.write_text(
        '{"marketplaces":{"uk":[{"date":"2026-01-01","price":NaN}],'
        '"us":[{"date":"2026-01-01","price":4.0}]}}'
    )

    record_prices(
        [make_product(asin="B000TEST01", price=3.0)],
        observation_date="2026-01-02",
    )

    saved = json.loads(path.read_text(), parse_constant=lambda value: 1 / 0)
    assert saved["marketplaces"]["uk"] == []
    assert [entry["price"] for entry in saved["marketplaces"]["us"]] == [4.0, 3.0]


def test_invalid_stored_locale_allows_recovery_commands(tmp_config):
    constants.CONFIG_FILE.write_text(json.dumps({"locale": "invalid"}))
    runner = CliRunner()

    blocked = runner.invoke(cli, ["find", "--dry-run"])
    listed = runner.invoke(cli, ["config", "list"])
    repaired = runner.invoke(cli, ["config", "set", "locale", "us"])

    assert blocked.exit_code != 0
    assert "Stored locale" in blocked.output
    assert listed.exit_code == 0
    assert repaired.exit_code == 0
    assert load_config()["locale"] == "us"


def test_malformed_profiles_and_monitors_are_marked_and_rejected(tmp_config):
    constants.PROFILES_FILE.write_text(json.dumps({"broken": []}))
    constants.MONITORS_FILE.write_text(json.dumps({"broken": []}))
    runner = CliRunner()

    profile_list = runner.invoke(cli, ["profile", "list"])
    profile_use = runner.invoke(cli, ["find", "--profile", "broken", "--dry-run"])
    monitor_list = runner.invoke(cli, ["monitor", "list"])
    monitor_use = runner.invoke(cli, ["monitor", "show", "broken"])

    assert "malformed profile" in profile_list.output
    assert "is malformed" in profile_use.output
    assert "malformed definition" in monitor_list.output
    assert "is malformed" in monitor_use.output


def test_malformed_monitor_field_types_are_rejected_cleanly(tmp_config):
    constants.MONITORS_FILE.write_text(
        json.dumps(
            {
                "bad-locale": {
                    "mode": "find",
                    "locale": [],
                    "settings": {},
                },
                "bad-name": {
                    "name": 123,
                    "mode": "find",
                    "locale": "us",
                    "settings": {},
                },
            }
        )
    )
    runner = CliRunner()

    listed = runner.invoke(cli, ["monitor", "list"])
    selected = runner.invoke(cli, ["monitor", "show", "bad-locale"])
    doctor = runner.invoke(cli, ["doctor"])

    assert listed.exit_code == 0
    assert listed.output.count("malformed definition") == 2
    assert selected.exit_code != 0
    assert "is malformed" in selected.output
    assert doctor.exit_code == 1
    assert "Saved-search monitors" in doctor.output


def test_malformed_track_records_warn_instead_of_crashing(caplog):
    state = {"run_history": ["bad", {"at": "ok"}]}

    assert run_history(state) == [{"at": "ok"}]
    append_run(state, {"at": "new"})

    assert state["run_history"] == [{"at": "new"}, {"at": "ok"}]
    assert "malformed" in caplog.text


def test_doctor_fails_on_raw_profile_and_monitor_corruption(tmp_config, mock_client):
    constants.PROFILES_FILE.write_text("[]")
    constants.MONITORS_FILE.write_text(json.dumps({"bad": []}))

    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 1
    assert "Profiles parseable" in result.output
    assert "Monitors parseable" in result.output
    assert "Malformed entries" in result.output


def test_doctor_rejects_malformed_profile_field_shapes(tmp_config, mock_client):
    constants.PROFILES_FILE.write_text(json.dumps({"bad": {"genre": []}}))

    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 1
    assert "Profile settings valid" in result.output
    assert "genre must be text" in result.output


def test_tolerant_runtime_loader_recovers_from_invalid_utf8(tmp_config):
    constants.CONFIG_FILE.write_bytes(b"\xff")
    runner = CliRunner()

    recovered = runner.invoke(cli, ["config", "list"])
    doctor = runner.invoke(cli, ["doctor"])

    assert recovered.exit_code == 0
    assert "No global defaults" in recovered.output
    assert doctor.exit_code == 1
    assert "Config file valid" in doctor.output


def test_config_secrets_are_redacted_unless_explicitly_requested(
    tmp_config, monkeypatch
):
    monkeypatch.setattr(
        "audible_deals.validation.validate_webhook_url", lambda _value: None
    )
    constants.CONFIG_FILE.write_text(
        json.dumps(
            {
                "webhook": "https://example.com/secret",
                "webhook_headers": ["Authorization: Bearer secret", "X-Key: token"],
                "max_price": 5,
            }
        )
    )
    runner = CliRunner()

    hidden = runner.invoke(cli, ["config", "list"])
    shown = runner.invoke(cli, ["config", "list", "--show-secrets"])
    get_hidden = runner.invoke(cli, ["config", "get", "webhook"])
    set_hidden = runner.invoke(
        cli, ["config", "set", "webhook", "https://example.com/new-secret"]
    )

    assert "example.com/secret" not in hidden.output
    assert "Bearer secret" not in hidden.output
    assert "Authorization: <redacted>" in hidden.output
    assert "max_price = 5" in hidden.output
    assert "example.com/secret" in shown.output
    assert "Bearer secret" in shown.output
    assert "<redacted>" in get_hidden.output
    assert "new-secret" not in set_hidden.output
    assert "<redacted>" in set_hidden.output


def test_config_and_profile_transactions_preserve_concurrent_updates(
    tmp_config, monkeypatch
):
    import audible_deals.config_store as store

    entered = threading.Event()
    release = threading.Event()
    real_load_config = store.load_config
    calls = 0

    def delayed_load_config():
        nonlocal calls
        result = real_load_config()
        calls += 1
        if calls == 1:
            entered.set()
            release.wait(timeout=2)
        return result

    monkeypatch.setattr(store, "load_config", delayed_load_config)

    def set_config(key):
        with config_transaction() as config:
            config[key] = True

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(set_config, "skip_owned")
        assert entered.wait(timeout=2)
        second = pool.submit(set_config, "deep")
        release.set()
        first.result()
        second.result()

    def set_profile(name):
        with profiles_transaction() as profiles:
            profiles[name] = {"genre": name}

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(set_profile, ("one", "two")))

    assert load_config()["skip_owned"] is True
    assert load_config()["deep"] is True
    assert set(load_profiles()) == {"one", "two"}


@pytest.mark.parametrize(
    ("command", "pages"),
    (("find", 60), ("search", 59)),
)
def test_catalog_scan_limit_allows_exactly_sixty_calls(
    tmp_config, mock_client, command, pages
):
    args = [command]
    if command == "search":
        args.append("query")
    args.extend(["--pages", str(pages), "--all-languages", "--quiet"])

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    ("command", "pages"),
    (("find", 61), ("search", 60)),
)
def test_catalog_scan_limit_rejects_sixty_one_calls(
    tmp_config, mock_client, command, pages
):
    args = [command]
    if command == "search":
        args.append("query")
    args.extend(["--pages", str(pages)])

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 2
    assert "61 catalog calls" in result.output
    assert "--allow-large-scan" in result.output
    mock_client.search_segments.assert_not_called()


def test_large_scan_override_and_subcategory_enforcement(tmp_config, mock_client):
    runner = CliRunner()
    allowed = runner.invoke(
        cli,
        [
            "find",
            "--pages",
            "61",
            "--allow-large-scan",
            "--all-languages",
            "--quiet",
        ],
    )
    assert allowed.exit_code == 0, allowed.output

    mock_client.reset_mock()
    mock_client.resolve_genre.return_value = ("parent", "Genre")
    mock_client.get_categories.return_value = [
        {"id": "one", "name": "One"},
        {"id": "two", "name": "Two"},
    ]
    blocked = runner.invoke(
        cli,
        [
            "find",
            "--genre",
            "genre",
            "--subcategories",
            "--pages",
            "31",
        ],
    )

    assert blocked.exit_code == 2
    assert "62 catalog calls" in blocked.output
    mock_client.search_segments.assert_not_called()


def test_large_dry_run_is_nonblocking_and_reports_override(tmp_config):
    runner = CliRunner()
    text_result = runner.invoke(cli, ["find", "--pages", "61", "--dry-run"])
    json_result = runner.invoke(
        cli, ["search", "query", "--pages", "60", "--dry-run", "--json"]
    )

    assert text_result.exit_code == 0
    assert "requires --allow-large-scan" in text_result.output
    payload = json.loads(json_result.output)
    assert payload["api_calls"] == 61
    assert payload["catalog_call_limit"] == 60
    assert payload["requires_large_scan_override"] is True


def test_wishlist_json_and_output_can_be_combined_and_extension_is_validated(
    tmp_config,
):
    constants.WISHLIST_FILE.write_text(
        json.dumps([{"asin": "B001", "title": "Book", "max_price": 5}])
    )
    runner = CliRunner()
    path = tmp_config / "wishlist.json"

    combined = runner.invoke(cli, ["wishlist", "list", "--json", "--output", str(path)])
    invalid = runner.invoke(
        cli,
        ["wishlist", "list", "--json", "--output", str(tmp_config / "bad.txt")],
    )

    assert combined.exit_code == 0, combined.output
    assert json.loads(combined.stdout)["items"][0]["asin"] == "B001"
    assert json.loads(path.read_text())["items"][0]["asin"] == "B001"
    assert "Exported" in combined.stderr
    assert invalid.exit_code != 0
    assert "Unsupported extension" in invalid.output
