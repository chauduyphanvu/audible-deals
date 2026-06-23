"""Regression tests for wishlist CLI bug fixes."""

from __future__ import annotations

from click.testing import CliRunner

from audible_deals.cli import cli
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
