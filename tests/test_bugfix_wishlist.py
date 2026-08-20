"""Regression tests for wishlist CLI bug fixes."""

from __future__ import annotations

import copy
import contextlib
import json
import math
import stat
import time
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from audible_deals.cli import cli
import audible_deals.cli.wishlist as wishlist_cli_mod
import audible_deals.constants as constants_mod
import audible_deals.wishlist as wishlist_mod

from tests.conftest import make_product


# Bug 10: wishlist update must not KeyError on an entry missing 'title'.
def test_update_entry_missing_title_does_not_crash(tmp_config):
    wishlist_mod.save_wishlist([{"asin": "B001", "max_price": 5}])
    runner = CliRunner()
    result = runner.invoke(cli, ["wishlist", "update", "B001", "--max-price", "3"])
    assert result.exit_code == 0, result.output
    assert "1 updated" in result.output
    items = wishlist_mod.load_wishlist()
    assert items[0]["max_price"] == 3.0


def test_clear_target_entry_missing_title_does_not_crash(tmp_config):
    wishlist_mod.save_wishlist([{"asin": "B002", "max_price": 5}])
    runner = CliRunner()
    result = runner.invoke(cli, ["wishlist", "update", "B002", "--clear-target"])
    assert result.exit_code == 0, result.output
    assert "1 updated" in result.output
    items = wishlist_mod.load_wishlist()
    assert items[0]["max_price"] is None


# Bug 11: wishlist sync must not create duplicate entries for a repeated ASIN.
def test_sync_dedupes_repeated_asin_in_response(mock_client, tmp_config):
    mock_client.get_wishlist.return_value = [
        make_product(asin="DUP1", title="Duplicated Book"),
        make_product(asin="DUP1", title="Duplicated Book"),
    ]
    runner = CliRunner()
    result = runner.invoke(cli, ["wishlist", "sync"])
    assert result.exit_code == 0, result.output
    items = wishlist_mod.load_wishlist()
    assert [i["asin"] for i in items] == ["DUP1"]
    assert "1 synced" in result.output


# Bug 12: --max-price must reject negative targets on add (and sync), matching update.
def test_add_rejects_negative_max_price(mock_client, tmp_config):
    runner = CliRunner()
    result = runner.invoke(cli, ["wishlist", "add", "B00R6S1RCY", "--max-price=-5"])
    assert result.exit_code != 0
    assert "-5" in result.output or "Invalid value" in result.output
    assert wishlist_mod.load_wishlist() == []


def test_sync_rejects_negative_max_price(mock_client, tmp_config):
    mock_client.get_wishlist.return_value = [make_product(asin="NEG1")]
    runner = CliRunner()
    result = runner.invoke(cli, ["wishlist", "sync", "--max-price=-5"])
    assert result.exit_code != 0
    assert wishlist_mod.load_wishlist() == []


def test_add_author_rejects_negative_max_price(mock_client, tmp_config):
    runner = CliRunner()
    result = runner.invoke(
        cli, ["wishlist", "add", "--author", "Some Author", "--max-price=-5"]
    )
    assert result.exit_code != 0
    assert wishlist_mod.load_wishlist() == []


def test_add_does_not_hold_wishlist_lock_during_api_call(
    mock_client, tmp_config, monkeypatch
):
    lock_held = False

    @contextlib.contextmanager
    def tracked_lock():
        nonlocal lock_held
        assert not lock_held
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    def get_product(asin):
        assert not lock_held
        return make_product(asin=asin, title="Fetched Book")

    monkeypatch.setattr(wishlist_cli_mod, "wishlist_lock", tracked_lock)
    mock_client.get_product.side_effect = get_product

    result = CliRunner().invoke(cli, ["wishlist", "add", "B00R6S1RCY", "B00R6S1RCY"])

    assert result.exit_code == 0, result.output
    mock_client.get_product.assert_called_once_with("B00R6S1RCY")
    assert [item["asin"] for item in wishlist_mod.load_wishlist()] == ["B00R6S1RCY"]


def test_semantic_inspector_rejects_bad_targets_and_entries_without_mutation():
    raw = [
        {"asin": "GOOD1", "max_price": None},
        {"asin": "GOOD2", "max_price": 0},
        {"type": "author", "author": "Good Author", "max_price": 0},
        {"asin": "NEG1", "max_price": -1},
        {"asin": "NAN1", "max_price": math.nan},
        {"asin": "INF1", "max_price": math.inf},
        {"asin": "STR1", "max_price": "5"},
        {"asin": "BOOL1", "max_price": True},
        {"asin": "bad/path", "max_price": None},
        {"type": "author", "author": " ", "max_price": 5},
        {"type": "author", "author": "No Target", "max_price": None},
        "not an object",
    ]
    before = copy.deepcopy(raw)

    inspection = wishlist_mod.inspect_wishlist(raw)

    assert [item["asin"] for item in inspection.asin_items] == ["GOOD1", "GOOD2"]
    assert [item["author"] for item in inspection.author_items] == ["Good Author"]
    assert [issue.index for issue in inspection.issues] == list(range(3, 12))
    assert raw == before


def test_semantic_inspector_accepts_large_nonnegative_integer_target():
    target = 10**400

    inspection = wishlist_mod.inspect_wishlist([{"asin": "HUGE1", "max_price": target}])

    assert inspection.asin_items == [{"asin": "HUGE1", "max_price": target}]
    assert inspection.issues == []


def test_mutation_loader_refuses_unreadable_wishlist(tmp_config, monkeypatch):
    constants_mod.WISHLIST_FILE.write_text("[]")
    original_read_text = Path.read_text

    def fail_read(path, *args, **kwargs):
        if path == constants_mod.WISHLIST_FILE:
            raise OSError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_read)

    with pytest.raises(click.ClickException, match="Cannot modify wishlist"):
        wishlist_mod.load_wishlist_for_mutation()


@pytest.mark.parametrize(
    "args",
    [
        ["wishlist", "add", "B001"],
        ["wishlist", "add", "--author", "Author", "--max-price", "1"],
        ["wishlist", "remove", "B001"],
        ["wishlist", "update", "B001", "--max-price", "1"],
        ["wishlist", "sync"],
        ["wishlist", "purge", "--owned", "--yes"],
    ],
)
@pytest.mark.parametrize("contents", [b"{", b'{"notes":"keep"}\n'])
def test_wishlist_mutations_preserve_invalid_root(
    args, contents, mock_client, tmp_config
):
    constants_mod.WISHLIST_FILE.write_bytes(contents)

    result = CliRunner().invoke(cli, args)

    assert result.exit_code != 0
    assert "Cannot modify wishlist" in result.output
    assert constants_mod.WISHLIST_FILE.read_bytes() == contents


@pytest.mark.parametrize("value", ["nan", "inf"])
@pytest.mark.parametrize(
    "args",
    [
        ["wishlist", "add", "B001", "--max-price"],
        ["wishlist", "update", "B001", "--max-price"],
        ["wishlist", "sync", "--max-price"],
    ],
)
def test_cli_max_price_rejects_nonfinite_values(args, value, tmp_config):
    result = CliRunner().invoke(cli, [*args, value])

    assert result.exit_code != 0
    assert "finite" in result.output
    assert not constants_mod.WISHLIST_FILE.exists()


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_interactive_nonfinite_target_is_not_saved(value, tmp_config):
    from audible_deals.cli.interactive import _interactive_browse

    @click.command()
    def command():
        _interactive_browse([make_product(asin="FINITE1")])

    result = CliRunner().invoke(command, input=f"w 1\n{value}\nq\n")

    assert result.exit_code == 0, result.output
    assert wishlist_mod.load_wishlist()[0]["max_price"] is None


def test_interactive_preserves_invalid_wishlist_root(tmp_config):
    from audible_deals.cli.interactive import _interactive_browse

    contents = b'{"notes":"keep"}\n'
    constants_mod.WISHLIST_FILE.write_bytes(contents)

    @click.command()
    def command():
        _interactive_browse([make_product(asin="SAFE1")])

    result = CliRunner().invoke(command, input="w 1\n\n")

    assert result.exit_code != 0
    assert "Cannot modify wishlist" in result.output
    assert constants_mod.WISHLIST_FILE.read_bytes() == contents


def test_watch_not_found_entry_without_title_does_not_crash(mock_client, tmp_config):
    wishlist_mod.save_wishlist([{"asin": "GONE1", "max_price": 5}])
    mock_client.get_products_batch.return_value = []

    result = CliRunner().invoke(cli, ["watch"])

    assert result.exit_code == 0, result.output
    assert "Not found: GONE1" in result.output


@pytest.mark.parametrize("fmt", ["slack", "discord", "teams"])
def test_webhook_formats_render_large_integer_target(fmt):
    from audible_deals.webhooks import format_webhook_payload

    target = 10**400
    body, _ = format_webhook_payload(
        [
            {
                "asin": "HUGE1",
                "title": "Huge Target",
                "price": 1.0,
                "target": target,
                "url": "https://example.com/HUGE1",
            }
        ],
        fmt,
    )

    assert f"target ${target}.00" in str(json.loads(body))


def test_webhook_template_formats_large_integer_target():
    from audible_deals.webhooks import format_webhook_payload

    target = 10**400
    body, _ = format_webhook_payload(
        [
            {
                "asin": "HUGE1",
                "title": "Huge Target",
                "price": 1.0,
                "target": target,
                "url": "https://example.com/HUGE1",
            }
        ],
        "generic",
        template="{target:.2f}",
    )

    assert body.decode() == f"{target}.00"


def test_webhook_template_preserves_ordinary_integer_target_formatting():
    from audible_deals.webhooks import format_webhook_payload

    body, _ = format_webhook_payload(
        [
            {
                "asin": "INT1",
                "title": "Integer Target",
                "price": 1.0,
                "target": 5,
                "url": "https://example.com/INT1",
            }
        ],
        "generic",
        template="{target}|{target:.2f}",
    )

    assert body.decode() == "5.0|5.00"


def test_wishlist_commands_skip_warn_once_and_preserve_invalid_entries(tmp_config):
    raw = [
        {"asin": "GOOD1", "title": "Good", "max_price": 5},
        {"asin": "BADNEG", "title": "Keep me", "max_price": -1},
        {"type": "author", "author": "", "max_price": 3},
    ]
    wishlist_mod.save_wishlist(raw)
    runner = CliRunner()

    updated = runner.invoke(cli, ["wishlist", "update", "GOOD1", "--max-price", "4"])
    assert updated.exit_code == 0, updated.output
    assert updated.stderr.count("Warning: skipped 2 invalid wishlist entries") == 1
    saved = wishlist_mod.load_wishlist()
    assert saved[0]["max_price"] == 4
    assert saved[1:] == raw[1:]

    listed = runner.invoke(cli, ["wishlist", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    assert [item["asin"] for item in json.loads(listed.stdout)["items"]] == ["GOOD1"]
    assert listed.stderr.count("Warning: skipped 2 invalid wishlist entries") == 1
    assert wishlist_mod.load_wishlist() == saved


def test_doctor_reports_indexed_wishlist_semantic_issues(tmp_config, mock_client):
    constants_mod.AUTH_FILE.write_text(
        json.dumps(
            {
                "access_token": "at",
                "refresh_token": "rt",
                "locale_code": "us",
                "expires": time.time() + 86400,
            }
        )
    )
    wishlist_mod.save_wishlist([{"asin": f"BAD{i}", "max_price": -1} for i in range(6)])

    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "Wishlist health" in result.output
    assert "WARN" in result.output
    assert "[0]" in result.output
    assert "+1 more" in result.output
    assert "wishlist repair --dry-run" in result.output


def test_doctor_fails_for_non_list_wishlist(tmp_config, mock_client):
    constants_mod.AUTH_FILE.write_text(
        json.dumps(
            {
                "access_token": "at",
                "refresh_token": "rt",
                "locale_code": "us",
                "expires": time.time() + 86400,
            }
        )
    )
    constants_mod.WISHLIST_FILE.write_text("{}")

    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 1
    assert "Wishlist health" in result.output
    assert "Expected a list" in result.output


def test_repair_healthy_wishlist_does_not_write_or_create_backup(tmp_config):
    original = b'[{"asin":"GOOD1","max_price":null}]\n'
    constants_mod.WISHLIST_FILE.write_bytes(original)

    result = CliRunner().invoke(cli, ["wishlist", "repair", "--yes"])

    assert result.exit_code == 0, result.output
    assert "no invalid entries" in result.output
    assert constants_mod.WISHLIST_FILE.read_bytes() == original
    assert not (tmp_config / "wishlist.json.bak").exists()


def test_repair_dry_run_reports_indexes_without_writing(tmp_config):
    original = b'[{"asin":"GOOD1","max_price":null},{"asin":"BAD","max_price":-1}]'
    constants_mod.WISHLIST_FILE.write_bytes(original)

    result = CliRunner().invoke(cli, ["wishlist", "repair", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "[1] max_price must be null or a finite non-negative number" in result.output
    assert "No files changed" in result.output
    assert constants_mod.WISHLIST_FILE.read_bytes() == original
    assert not (tmp_config / "wishlist.json.bak").exists()


def test_repair_cancellation_leaves_source_untouched(tmp_config):
    original = b'[{"asin":"GOOD1"},{"asin":"BAD","max_price":-1}]'
    constants_mod.WISHLIST_FILE.write_bytes(original)

    result = CliRunner().invoke(cli, ["wishlist", "repair"], input="n\n")

    assert result.exit_code == 1
    assert constants_mod.WISHLIST_FILE.read_bytes() == original
    assert not (tmp_config / "wishlist.json.bak").exists()


def test_confirmed_repair_preserves_valid_data_order_and_exact_backup(tmp_config):
    valid_book = {
        "title": "Café Book",
        "asin": "GOOD1",
        "max_price": 5,
        "metadata": {"labels": ["one", "two"]},
    }
    valid_author = {
        "type": "author",
        "author": "Good Author",
        "max_price": 0,
        "added": "2026-08-20",
    }
    original_data = [valid_book, "invalid", valid_author]
    original = json.dumps(
        original_data, ensure_ascii=False, separators=(",", ":")
    ).encode()
    constants_mod.WISHLIST_FILE.write_bytes(original)

    result = CliRunner().invoke(cli, ["wishlist", "repair", "--yes"])

    assert result.exit_code == 0, result.output
    assert wishlist_mod.load_wishlist() == [valid_book, valid_author]
    backup = tmp_config / "wishlist.json.bak"
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_repair_uses_next_available_backup_name(tmp_config):
    original = b'[{"asin":"GOOD1"},{"asin":"BAD","max_price":-1}]'
    constants_mod.WISHLIST_FILE.write_bytes(original)
    first_backup = tmp_config / "wishlist.json.bak"
    first_backup.write_bytes(b"existing backup")

    result = CliRunner().invoke(cli, ["wishlist", "repair", "--yes"])

    assert result.exit_code == 0, result.output
    assert first_backup.read_bytes() == b"existing backup"
    assert (tmp_config / "wishlist.json.bak.1").read_bytes() == original


@pytest.mark.parametrize(
    "contents",
    [
        b"{",
        b'{"notes":"keep"}\n',
        b"[NaN]",
        b"[Infinity]",
        b"[-Infinity]",
        b'[{"asin":"GOOD1","metadata":1e400},{"asin":"BAD","max_price":-1}]',
    ],
)
def test_repair_refuses_malformed_or_non_list_roots(tmp_config, contents):
    constants_mod.WISHLIST_FILE.write_bytes(contents)

    result = CliRunner().invoke(cli, ["wishlist", "repair", "--yes"])

    assert result.exit_code != 0
    assert "Cannot repair wishlist" in result.output
    assert constants_mod.WISHLIST_FILE.read_bytes() == contents
    assert not (tmp_config / "wishlist.json.bak").exists()


def test_repair_aborts_if_wishlist_changes_during_confirmation(tmp_config, monkeypatch):
    original = b'[{"asin":"GOOD1"},{"asin":"BAD","max_price":-1}]'
    replacement = b'[{"asin":"GOOD1"},{"asin":"NEW1"},{"asin":"BAD","max_price":-1}]'
    constants_mod.WISHLIST_FILE.write_bytes(original)

    def concurrent_update(*args, **kwargs):
        constants_mod.WISHLIST_FILE.write_bytes(replacement)
        return True

    monkeypatch.setattr(click, "confirm", concurrent_update)

    result = CliRunner().invoke(cli, ["wishlist", "repair"])

    assert result.exit_code != 0
    assert "changed while awaiting confirmation" in result.output
    assert constants_mod.WISHLIST_FILE.read_bytes() == replacement
    assert not (tmp_config / "wishlist.json.bak").exists()
