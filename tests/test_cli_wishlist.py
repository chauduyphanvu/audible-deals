"""Wishlist CLI behavior."""

from __future__ import annotations

import contextlib
import json
import stat

import click
import pytest
from click.testing import CliRunner

import audible_deals.config_store as config_store_mod
import audible_deals.constants as constants_mod
import audible_deals.wishlist as wishlist_mod
import audible_deals.wishlist_service as wishlist_service_mod
from audible_deals.cli import cli
from audible_deals.serialization import (
    serialize_product as _serialize_product,
)
from tests.conftest import make_product


def _routes_run(runner, args, **kwargs):
    """Invoke the CLI and return the result; fail on unexpected errors."""
    result = runner.invoke(cli, args, catch_exceptions=False, **kwargs)
    return result


def _routes_seed_last_results(tmp_config, products):
    """Write a last_results.json cache file."""
    data = {
        "title": "Test Results",
        "results": [_serialize_product(p) for p in products],
    }
    (tmp_config / "last_results.json").write_text(json.dumps(data))


class TestWishlistCommands:
    def test_add_list_remove(self, mock_client, tmp_config):
        mock_client.get_product.return_value = make_product(
            asin="W1", title="Wish Book"
        )

        runner = CliRunner()

        # Add
        result = runner.invoke(cli, ["wishlist", "add", "W1", "--max-price", "5"])
        assert result.exit_code == 0, result.output
        assert "Wish Book" in result.output
        assert "1 added" in result.output

        # List
        result = runner.invoke(cli, ["wishlist", "list"])
        assert result.exit_code == 0, result.output
        assert "W1" in result.output
        assert "$5.00" in result.output

        # Duplicate
        result = runner.invoke(cli, ["wishlist", "add", "W1"])
        assert "already on wishlist" in result.output

        # Remove
        result = runner.invoke(cli, ["wishlist", "remove", "W1"])
        assert result.exit_code == 0
        assert "1 removed" in result.output

        # Empty list
        result = runner.invoke(cli, ["wishlist", "list"])
        assert "empty" in result.output

    def test_add_not_found(self, mock_client, tmp_config):
        mock_client.get_product.side_effect = ValueError("not found")
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "add", "BAD"])
        assert "Not found" in result.output

    def test_list_entry_with_neither_key_does_not_crash(self, mock_client, tmp_config):
        """wishlist list must not crash and must skip entries with neither asin nor author type."""

        # Write a malformed entry with no asin and no type="author"
        items = [
            {"title": "Orphan Entry", "max_price": 5.0},
            {"asin": "W2", "title": "Valid Book", "max_price": 3.0},
        ]
        constants_mod.WISHLIST_FILE.write_text(json.dumps(items))
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "list"])
        assert result.exit_code == 0, result.output
        # Valid book appears; orphan entry is silently skipped
        assert "W2" in result.output
        assert "Orphan Entry" not in result.output


class TestWishlistListExport:
    """Tests for wishlist list --json and -o FILE export."""

    def _seed(self):
        wishlist_mod.save_wishlist(
            [
                {
                    "asin": "B001",
                    "title": "A Book",
                    "max_price": 5.0,
                    "added": "2024-01-01",
                },
                {
                    "type": "author",
                    "author": "Terry Pratchett",
                    "max_price": 3.0,
                    "added": "2024-06-01",
                },
            ]
        )

    def test_json_flag_shape(self, tmp_config, mock_client):
        """--json outputs the expected top-level structure with both entry types."""
        self._seed()
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "list", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "items" in data
        assert "author_watches" in data
        assert len(data["items"]) == 1
        assert data["items"][0]["asin"] == "B001"
        assert len(data["author_watches"]) == 1
        assert data["author_watches"][0]["author"] == "Terry Pratchett"

    def test_json_flag_suppresses_table(self, tmp_config, mock_client):
        """--json suppresses the table; stdout is pure JSON."""
        self._seed()
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "list", "--json"])
        assert result.exit_code == 0, result.output
        # Must parse as JSON without error
        json.loads(result.output)
        # Rich table markers should not be in stdout
        assert "Author watches" not in result.output

    def test_json_empty_wishlist(self, tmp_config, mock_client):
        """--json on an empty wishlist prints the empty structure, not an error."""
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "list", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data == {"items": [], "author_watches": []}

    def test_output_json_file(self, tmp_config, mock_client, tmp_path):
        """'-o FILE.json' writes JSON with the correct shape."""
        self._seed()
        out = tmp_path / "wl.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "list", "-o", str(out)])
        assert result.exit_code == 0, result.output
        assert "Exported" in result.output
        data = json.loads(out.read_text())
        assert len(data["items"]) == 1
        assert len(data["author_watches"]) == 1

    def test_output_csv_file(self, tmp_config, mock_client, tmp_path):
        """'-o FILE.csv' writes CSV with expected header and rows."""
        self._seed()
        out = tmp_path / "wl.csv"
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "list", "-o", str(out)])
        assert result.exit_code == 0, result.output
        assert "Exported" in result.output
        lines = out.read_text().splitlines()
        assert lines[0] == "type,asin,title,author,max_price,added"
        # One item row + one author_watch row
        assert len(lines) == 3
        assert lines[1].startswith("item,B001,")
        assert lines[2].startswith("author_watch,,")
        assert "Terry Pratchett" in lines[2]

    def test_output_bad_extension(self, tmp_config, mock_client, tmp_path):
        """'-o FILE.txt' raises an error matching the style of other export commands."""
        self._seed()
        out = tmp_path / "wl.txt"
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "list", "-o", str(out)])
        assert result.exit_code != 0
        assert "Unsupported extension" in result.output

    def test_output_empty_wishlist_writes_file(self, tmp_config, mock_client, tmp_path):
        """'-o FILE.json' on empty wishlist writes the empty structure."""
        out = tmp_path / "wl.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "list", "-o", str(out)])
        assert result.exit_code == 0, result.output
        data = json.loads(out.read_text())
        assert data == {"items": [], "author_watches": []}


class TestWishlistSyncCommand:
    def test_sync_adds_new_items(self, mock_client, tmp_config):
        """Items from Audible wishlist not in local are added."""
        mock_client.get_wishlist.return_value = [
            make_product(asin="WS1", title="Sync Book One"),
            make_product(asin="WS2", title="Sync Book Two"),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "sync"])
        assert result.exit_code == 0, result.output
        assert "Sync Book One" in result.output
        assert "Sync Book Two" in result.output
        assert "2 synced" in result.output
        assert "0 already tracked" in result.output

    def test_sync_skips_existing(self, mock_client, tmp_config):
        """Items already in local wishlist are counted as skipped, not re-added."""

        wishlist_mod.save_wishlist(
            [
                {
                    "asin": "WS1",
                    "title": "Already Here",
                    "max_price": None,
                    "added": "",
                },
            ]
        )
        mock_client.get_wishlist.return_value = [
            make_product(asin="WS1", title="Already Here"),
            make_product(asin="WS2", title="New Book"),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "sync"])
        assert result.exit_code == 0, result.output
        assert "Already Here" not in result.output
        assert "New Book" in result.output
        assert "1 synced" in result.output
        assert "1 already tracked" in result.output

    def test_sync_empty_wishlist(self, mock_client, tmp_config):
        """Empty Audible wishlist syncs zero items."""
        mock_client.get_wishlist.return_value = []
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "sync"])
        assert result.exit_code == 0, result.output
        assert "0 synced" in result.output

    def test_sync_max_price_applied(self, mock_client, tmp_config):
        """--max-price sets the target price on all synced items."""
        mock_client.get_wishlist.return_value = [
            make_product(asin="WS3", title="Price Book"),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "sync", "--max-price", "7.99"])
        assert result.exit_code == 0, result.output

        # Verify the saved item has max_price set

        items = wishlist_mod.load_wishlist()
        assert len(items) == 1
        assert items[0]["asin"] == "WS3"
        assert items[0]["max_price"] == 7.99

    def test_sync_persists_to_wishlist_file(self, mock_client, tmp_config):
        """Synced items are persisted so wishlist list can show them."""
        mock_client.get_wishlist.return_value = [
            make_product(asin="WS4", title="Persistent Book"),
        ]
        runner = CliRunner()
        sync_result = runner.invoke(cli, ["wishlist", "sync"])
        assert sync_result.exit_code == 0, sync_result.output

        result = runner.invoke(cli, ["wishlist", "list"])
        assert result.exit_code == 0, result.output
        assert "WS4" in result.output

    def test_sync_update_changes_existing_price(self, mock_client, tmp_config):
        """--update with --max-price updates target price for existing items."""

        wishlist_mod.save_wishlist(
            [
                {
                    "asin": "WS1",
                    "title": "Old Price Book",
                    "max_price": 20.0,
                    "added": "",
                },
            ]
        )
        mock_client.get_wishlist.return_value = [
            make_product(asin="WS1", title="Old Price Book"),
        ]
        runner = CliRunner()
        result = runner.invoke(
            cli, ["wishlist", "sync", "--max-price", "5", "--update"]
        )
        assert result.exit_code == 0, result.output
        items = wishlist_mod.load_wishlist()
        assert items[0]["max_price"] == 5.0
        assert "1 updated" in result.output

    def test_sync_update_without_max_price_errors(self, mock_client, tmp_config):
        """--update without --max-price raises an error."""
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "sync", "--update"])
        assert result.exit_code == 2
        assert "requires --max-price" in result.output


class TestLoadWishlistTypeValidation:
    def test_dict_returns_empty_list(self, tmp_config):
        """A wishlist.json containing {} instead of [] returns empty list."""

        constants_mod.WISHLIST_FILE.write_text("{}")
        assert wishlist_mod.load_wishlist() == []

    def test_load_profiles_list_returns_empty_dict(self, tmp_config):
        """A profiles.json containing [] instead of {} returns empty dict."""

        constants_mod.PROFILES_FILE.write_text("[]")
        assert config_store_mod.load_profiles() == {}

    def test_load_config_array_returns_empty_dict(self, tmp_config):
        """A config.json containing a JSON array instead of {} returns {}."""
        from audible_deals.config_store import load_config

        (tmp_config / "config.json").write_text("[1, 2]")
        assert load_config() == {}


class TestWishlistRemoveLast:
    def _seed_cache(self, tmp_config, products):

        cache_obj = {
            "title": "Search: test",
            "results": [_serialize_product(p) for p in products],
        }
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))

    def test_remove_last_resolves_from_cache(self, tmp_config):
        """wishlist remove --last N resolves the ASIN from the last results cache."""

        p = make_product(asin="WRL1", title="Remove Me")
        self._seed_cache(tmp_config, [p])
        wishlist_mod.save_wishlist(
            [{"asin": "WRL1", "title": "Remove Me", "max_price": None, "added": ""}]
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "remove", "--last", "1"])
        assert result.exit_code == 0, result.output
        assert "Result #1" in result.output
        assert "1 removed" in result.output

    def test_remove_last_and_asin_combined(self, tmp_config):
        """wishlist remove supports mixing positional ASINs and --last refs."""

        p = make_product(asin="WRL2", title="Cache Book")
        self._seed_cache(tmp_config, [p])
        wishlist_mod.save_wishlist(
            [
                {"asin": "WRL2", "title": "Cache Book", "max_price": None, "added": ""},
                {
                    "asin": "WRL3",
                    "title": "Direct Book",
                    "max_price": None,
                    "added": "",
                },
            ]
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "remove", "WRL3", "--last", "1"])
        assert result.exit_code == 0, result.output
        assert "2 removed" in result.output

    def test_remove_no_args_raises_usage_error(self, tmp_config):
        """wishlist remove with no arguments and no --last raises a UsageError."""
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "remove"])
        assert result.exit_code != 0
        assert "ASIN" in result.output or "Usage" in result.output


class TestWishlistUpdateCommand:
    """Tests for `deals wishlist update`."""

    def _seed_wishlist(self, items):

        wishlist_mod.save_wishlist(items)

    def _seed_cache(self, tmp_config, products):

        cache_obj = {
            "title": "Search: test",
            "results": [_serialize_product(p) for p in products],
        }
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))

    def test_set_target_price(self, tmp_config):
        """--max-price updates max_price for the matching entry."""

        self._seed_wishlist(
            [
                {
                    "asin": "WU01",
                    "title": "Update Me",
                    "max_price": 10.0,
                    "added": "2024-01-01",
                }
            ]
        )
        runner = CliRunner()
        result = runner.invoke(
            cli, ["wishlist", "update", "WU01", "--max-price", "3.99"]
        )
        assert result.exit_code == 0, result.output
        assert "Update Me" in result.output
        assert "3.99" in result.output
        assert "1 updated" in result.output
        items = wishlist_mod.load_wishlist()
        assert items[0]["max_price"] == 3.99

    def test_clear_target(self, tmp_config):
        """--clear-target sets max_price to None."""

        self._seed_wishlist(
            [
                {
                    "asin": "WU02",
                    "title": "Clear Me",
                    "max_price": 5.0,
                    "added": "2024-01-01",
                }
            ]
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "update", "WU02", "--clear-target"])
        assert result.exit_code == 0, result.output
        assert "Clear Me" in result.output
        assert "target cleared" in result.output
        assert "1 updated" in result.output
        items = wishlist_mod.load_wishlist()
        assert items[0]["max_price"] is None

    def test_both_flags_errors(self, tmp_config):
        """Providing both --max-price and --clear-target is a UsageError."""
        self._seed_wishlist(
            [{"asin": "WU03", "title": "Book", "max_price": 5.0, "added": "2024-01-01"}]
        )
        runner = CliRunner()
        result = runner.invoke(
            cli, ["wishlist", "update", "WU03", "--max-price", "2", "--clear-target"]
        )
        assert result.exit_code != 0
        assert "not both" in result.output or "Usage" in result.output

    def test_neither_flag_errors(self, tmp_config):
        """Providing neither --max-price nor --clear-target is a UsageError."""
        self._seed_wishlist(
            [{"asin": "WU04", "title": "Book", "max_price": 5.0, "added": "2024-01-01"}]
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "update", "WU04"])
        assert result.exit_code != 0
        assert "--max-price" in result.output or "Usage" in result.output

    def test_unknown_asin_reported_not_errored(self, tmp_config):
        """An ASIN not on the wishlist prints a message but does not error."""
        self._seed_wishlist([])
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "update", "WU99", "--max-price", "3"])
        assert result.exit_code == 0, result.output
        assert "Not on wishlist: WU99" in result.output
        assert "0 updated" in result.output
        assert "1 not found" in result.output

    def test_no_asins_raises_usage_error(self, tmp_config):
        """Passing no ASINs and no --last raises a UsageError."""
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "update", "--max-price", "5"])
        assert result.exit_code != 0
        assert "ASIN" in result.output or "Usage" in result.output

    def test_last_ref_resolves(self, tmp_config):
        """--last N resolves the ASIN from the last results cache."""

        p = make_product(asin="WU05", title="Last Ref Book")
        self._seed_cache(tmp_config, [p])
        self._seed_wishlist(
            [
                {
                    "asin": "WU05",
                    "title": "Last Ref Book",
                    "max_price": None,
                    "added": "2024-01-01",
                }
            ]
        )
        runner = CliRunner()
        result = runner.invoke(
            cli, ["wishlist", "update", "--last", "1", "--max-price", "4"]
        )
        assert result.exit_code == 0, result.output
        assert "Result #1" in result.output
        assert "Last Ref Book" in result.output
        assert "4.00" in result.output
        assert "1 updated" in result.output
        items = wishlist_mod.load_wishlist()
        assert items[0]["max_price"] == 4.0

    def test_added_date_preserved(self, tmp_config):
        """The 'added' date is not modified when updating the target price."""

        self._seed_wishlist(
            [
                {
                    "asin": "WU06",
                    "title": "Dated Book",
                    "max_price": 8.0,
                    "added": "2023-05-15",
                }
            ]
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "update", "WU06", "--max-price", "2"])
        assert result.exit_code == 0, result.output
        items = wishlist_mod.load_wishlist()
        assert items[0]["added"] == "2023-05-15"

    def test_multiple_asins_partial_not_found(self, tmp_config):
        """Mix of found and not-found ASINs: found updated, not-found reported."""

        self._seed_wishlist(
            [
                {
                    "asin": "WU07",
                    "title": "Present Book",
                    "max_price": 10.0,
                    "added": "2024-01-01",
                }
            ]
        )
        runner = CliRunner()
        result = runner.invoke(
            cli, ["wishlist", "update", "WU07", "WU99", "--max-price", "3"]
        )
        assert result.exit_code == 0, result.output
        assert "1 updated" in result.output
        assert "1 not found" in result.output
        assert "Not on wishlist: WU99" in result.output
        items = wishlist_mod.load_wishlist()
        assert items[0]["max_price"] == 3.0


class TestWishlistAddAuthor:
    def test_add_author_watch_requires_max_price(self, tmp_config, mock_client):
        """--author without --max-price is a UsageError."""
        runner = CliRunner()
        result = runner.invoke(
            cli, ["wishlist", "add", "--author", "Brandon Sanderson"]
        )
        assert result.exit_code != 0
        assert (
            "max-price" in result.output.lower()
            or "max-price" in str(result.exception).lower()
        )

    def test_add_author_watch_rejects_asin_combo(self, tmp_config, mock_client):
        """--author combined with ASIN positional arg is a UsageError."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "wishlist",
                "add",
                "B00TEST001",
                "--author",
                "Brandon Sanderson",
                "--max-price",
                "5",
            ],
        )
        assert result.exit_code != 0
        assert (
            "author" in result.output.lower()
            or "author" in str(result.exception).lower()
        )

    def test_add_author_watch_rejects_last_combo(self, tmp_config, mock_client):
        """--author combined with --last is a UsageError."""

        p = make_product(asin="B00TESTAA01", title="Book")
        from audible_deals.serialization import serialize_product

        cache_obj = {"title": "Test", "results": [serialize_product(p)]}
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "wishlist",
                "add",
                "--author",
                "Brandon Sanderson",
                "--max-price",
                "5",
                "--last",
                "1",
            ],
        )
        assert result.exit_code != 0

    def test_add_author_watch_appends_entry(self, tmp_config, mock_client):
        """add --author appends an author watch entry to the wishlist."""

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["wishlist", "add", "--author", "Brandon Sanderson", "--max-price", "5"],
        )
        assert result.exit_code == 0, result.output
        assert "Brandon Sanderson" in result.output
        items = wishlist_mod.load_wishlist()
        assert len(items) == 1
        assert items[0]["type"] == "author"
        assert items[0]["author"] == "Brandon Sanderson"
        assert items[0]["max_price"] == 5.0
        assert "asin" not in items[0]

    def test_add_author_watch_duplicate_ignored(self, tmp_config, mock_client):
        """Adding the same author twice (case-insensitive) prints 'already watching'."""

        runner = CliRunner()
        runner.invoke(
            cli,
            ["wishlist", "add", "--author", "Brandon Sanderson", "--max-price", "5"],
        )
        result = runner.invoke(
            cli,
            ["wishlist", "add", "--author", "brandon sanderson", "--max-price", "3"],
        )
        assert result.exit_code == 0, result.output
        assert "already watching" in result.output.lower()
        items = wishlist_mod.load_wishlist()
        assert len(items) == 1  # no second entry added

    def test_add_author_watch_no_network_call(self, tmp_config, mock_client):
        """Adding an author watch does not call get_product."""
        runner = CliRunner()
        runner.invoke(
            cli,
            ["wishlist", "add", "--author", "Brandon Sanderson", "--max-price", "5"],
        )
        mock_client.get_product.assert_not_called()


class TestWishlistListAuthor:
    def test_list_shows_author_section(self, tmp_config, mock_client):
        """wishlist list renders author watches in a separate section."""

        wishlist_mod.save_wishlist(
            [
                {
                    "asin": "B001",
                    "title": "A Book",
                    "max_price": 10.0,
                    "added": "2024-01-01",
                },
                {
                    "type": "author",
                    "author": "Brandon Sanderson",
                    "max_price": 5.0,
                    "added": "2024-06-01",
                },
            ]
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "list"])
        assert result.exit_code == 0, result.output
        assert "Author watches" in result.output
        assert "Brandon Sanderson" in result.output
        assert "B001" in result.output

    def test_list_author_only_shows_author_table(self, tmp_config, mock_client):
        """wishlist list with only author watches does not say 'empty'."""

        wishlist_mod.save_wishlist(
            [
                {
                    "type": "author",
                    "author": "Terry Pratchett",
                    "max_price": 4.0,
                    "added": "2024-06-01",
                }
            ]
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "list"])
        assert result.exit_code == 0, result.output
        assert "empty" not in result.output.lower()
        assert "Terry Pratchett" in result.output

    def test_list_both_empty_shows_empty_message(self, tmp_config, mock_client):
        """wishlist list with no entries (neither ASIN nor author) shows empty message."""
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "list"])
        assert result.exit_code == 0, result.output
        assert "empty" in result.output.lower()


class TestWishlistRemoveAuthor:
    def test_remove_author_by_name(self, tmp_config, mock_client):
        """wishlist remove --author removes a matching author watch."""

        wishlist_mod.save_wishlist(
            [
                {
                    "type": "author",
                    "author": "Brandon Sanderson",
                    "max_price": 5.0,
                    "added": "2024-01-01",
                }
            ]
        )
        runner = CliRunner()
        result = runner.invoke(
            cli, ["wishlist", "remove", "--author", "Brandon Sanderson"]
        )
        assert result.exit_code == 0, result.output
        assert "1 removed" in result.output
        items = wishlist_mod.load_wishlist()
        assert items == []

    def test_remove_author_case_insensitive(self, tmp_config, mock_client):
        """wishlist remove --author matches case-insensitively."""

        wishlist_mod.save_wishlist(
            [
                {
                    "type": "author",
                    "author": "Brandon Sanderson",
                    "max_price": 5.0,
                    "added": "2024-01-01",
                }
            ]
        )
        runner = CliRunner()
        result = runner.invoke(
            cli, ["wishlist", "remove", "--author", "brandon sanderson"]
        )
        assert result.exit_code == 0, result.output
        assert "1 removed" in result.output

    def test_remove_author_not_present_removes_zero(self, tmp_config, mock_client):
        """wishlist remove --author for an author not on the list removes 0."""

        wishlist_mod.save_wishlist(
            [
                {
                    "type": "author",
                    "author": "Brandon Sanderson",
                    "max_price": 5.0,
                    "added": "2024-01-01",
                }
            ]
        )
        runner = CliRunner()
        result = runner.invoke(
            cli, ["wishlist", "remove", "--author", "Terry Pratchett"]
        )
        assert result.exit_code == 0, result.output
        assert "0 removed" in result.output
        items = wishlist_mod.load_wishlist()
        assert len(items) == 1

    def test_remove_no_args_is_usage_error(self, tmp_config):
        """wishlist remove with no args and no --author is a UsageError."""
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "remove"])
        assert result.exit_code != 0


class TestWishlistSyncSkipsAuthorEntries:
    def test_sync_skips_author_entries(self, tmp_config, mock_client):
        """wishlist sync does not crash when author entries exist."""

        wishlist_mod.save_wishlist(
            [
                {
                    "type": "author",
                    "author": "Brandon Sanderson",
                    "max_price": 5.0,
                    "added": "2024-01-01",
                }
            ]
        )
        mock_client.get_wishlist.return_value = [
            make_product(asin="WS10", title="New Sync Book"),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "sync"])
        assert result.exit_code == 0, result.output
        assert "1 synced" in result.output
        items = wishlist_mod.load_wishlist()
        asins = [i.get("asin") for i in items if i.get("asin")]
        assert "WS10" in asins
        authors = [i for i in items if i.get("type") == "author"]
        assert len(authors) == 1


class TestWishlistUpdateSkipsAuthorEntries:
    def test_update_skips_author_entries(self, tmp_config, mock_client):
        """wishlist update does not crash when author entries exist."""

        wishlist_mod.save_wishlist(
            [
                {
                    "asin": "B00UPD001",
                    "title": "Update Book",
                    "max_price": 10.0,
                    "added": "",
                },
                {
                    "type": "author",
                    "author": "Brandon Sanderson",
                    "max_price": 5.0,
                    "added": "2024-01-01",
                },
            ]
        )
        runner = CliRunner()
        result = runner.invoke(
            cli, ["wishlist", "update", "B00UPD001", "--max-price", "3"]
        )
        assert result.exit_code == 0, result.output
        assert "1 updated" in result.output
        items = wishlist_mod.load_wishlist()
        asin_item = next(i for i in items if i.get("asin") == "B00UPD001")
        assert asin_item["max_price"] == 3.0


class TestWishlistPurge:
    def test_purge_removes_owned_keeps_unowned(self, mock_client, tmp_config):
        """Owned ASINs are removed; unowned and author watches survive."""

        mock_client.get_library_asins.return_value = {"OWN1", "OWN2"}
        constants_mod.WISHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        constants_mod.WISHLIST_FILE.write_text(
            json.dumps(
                [
                    {
                        "asin": "OWN1",
                        "title": "Owned One",
                        "max_price": None,
                        "added": "",
                    },
                    {
                        "asin": "OWN2",
                        "title": "Owned Two",
                        "max_price": None,
                        "added": "",
                    },
                    {
                        "asin": "KEEP",
                        "title": "Unowned",
                        "max_price": None,
                        "added": "",
                    },
                    {
                        "type": "author",
                        "author": "Sanderson",
                        "max_price": 5.0,
                        "added": "",
                    },
                ]
            )
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "purge", "--owned", "--yes"])
        assert result.exit_code == 0, result.output
        remaining = wishlist_mod.load_wishlist()
        asins = [i.get("asin") for i in remaining if i.get("asin")]
        assert "OWN1" not in asins
        assert "OWN2" not in asins
        assert "KEEP" in asins
        authors = [i for i in remaining if i.get("type") == "author"]
        assert len(authors) == 1
        assert "2 removed" in result.output
        assert "remaining" in result.output

    def test_purge_dry_run_does_not_modify(self, mock_client, tmp_config):
        """--dry-run lists candidates but does not write."""

        mock_client.get_library_asins.return_value = {"DRY1"}
        constants_mod.WISHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        original = [
            {"asin": "DRY1", "title": "Dry Book", "max_price": None, "added": ""},
        ]
        constants_mod.WISHLIST_FILE.write_text(json.dumps(original))
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "purge", "--owned", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "Dry Book" in result.output
        assert wishlist_mod.load_wishlist() == original

    def test_purge_missing_owned_raises_usage_error(self, mock_client, tmp_config):
        """Omitting --owned raises UsageError."""
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "purge"])
        assert result.exit_code == 2
        assert "--owned" in result.output

    def test_purge_nothing_to_purge(self, mock_client, tmp_config):
        """Prints a dim 'Nothing to purge' note when no owned items exist."""

        mock_client.get_library_asins.return_value = {"NOT_ON_LIST"}
        constants_mod.WISHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        constants_mod.WISHLIST_FILE.write_text(
            json.dumps(
                [{"asin": "KEEP", "title": "Unowned", "max_price": None, "added": ""}]
            )
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "purge", "--owned", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Nothing to purge" in result.output

    def test_purge_author_watches_never_removed(self, mock_client, tmp_config):
        """Author-watch entries are never removed even if their author matches nothing."""

        mock_client.get_library_asins.return_value = set()
        constants_mod.WISHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        constants_mod.WISHLIST_FILE.write_text(
            json.dumps(
                [
                    {
                        "type": "author",
                        "author": "Tolkien",
                        "max_price": 3.0,
                        "added": "",
                    },
                ]
            )
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["wishlist", "purge", "--owned", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Nothing to purge" in result.output
        remaining = wishlist_mod.load_wishlist()
        assert len(remaining) == 1


class TestRoutesWishlistCommands:
    def test_wishlist_add(self, tmp_config, mock_client):
        p = make_product(asin="W001", title="Wish Book")
        mock_client.get_product.return_value = p
        result = _routes_run(
            CliRunner(), ["wishlist", "add", "W001", "--max-price", "5"]
        )
        assert result.exit_code == 0
        assert "Wish Book" in result.output

    def test_wishlist_add_with_last(self, tmp_config, mock_client):
        products = [make_product(asin="W002", title="Last Wish")]
        _routes_seed_last_results(tmp_config, products)
        mock_client.get_product.return_value = products[0]
        result = _routes_run(CliRunner(), ["wishlist", "add", "--last", "1"])
        assert result.exit_code == 0

    def test_wishlist_list_empty(self, tmp_config, mock_client):
        result = _routes_run(CliRunner(), ["wishlist", "list"])
        assert result.exit_code == 0
        assert "empty" in result.output.lower()

    def test_wishlist_list_with_items(self, tmp_config, mock_client):
        wl = [{"asin": "W003", "title": "Listed Book", "max_price": 5.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        result = _routes_run(CliRunner(), ["wishlist", "list"])
        assert result.exit_code == 0
        assert "Listed Book" in result.output

    def test_wishlist_remove(self, tmp_config, mock_client):
        wl = [{"asin": "W004", "title": "Remove Me", "max_price": None, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        result = _routes_run(CliRunner(), ["wishlist", "remove", "W004"])
        assert result.exit_code == 0
        assert "1" in result.output  # "1 removed"

    def test_wishlist_sync(self, tmp_config, mock_client):
        mock_client.get_wishlist.return_value = [
            make_product(asin="W005", title="Synced Book"),
        ]
        result = _routes_run(CliRunner(), ["wishlist", "sync"])
        assert result.exit_code == 0
        assert "Synced Book" in result.output

    def test_wishlist_sync_with_max_price(self, tmp_config, mock_client):
        mock_client.get_wishlist.return_value = [
            make_product(asin="W006", title="Priced Sync"),
        ]
        result = _routes_run(CliRunner(), ["wishlist", "sync", "--max-price", "5"])
        assert result.exit_code == 0

    def test_wishlist_sync_update(self, tmp_config, mock_client):
        wl = [{"asin": "W007", "title": "Existing", "max_price": 10.0, "added": ""}]
        (tmp_config / "wishlist.json").write_text(json.dumps(wl))
        mock_client.get_wishlist.return_value = [
            make_product(asin="W007", title="Existing"),
        ]
        result = _routes_run(
            CliRunner(), ["wishlist", "sync", "--max-price", "5", "--update"]
        )
        assert result.exit_code == 0
        assert "1 updated" in result.output


def test_bugfixwishlist_update_entry_missing_title_does_not_crash(tmp_config):
    wishlist_mod.save_wishlist([{"asin": "B001", "max_price": 5}])
    runner = CliRunner()
    result = runner.invoke(cli, ["wishlist", "update", "B001", "--max-price", "3"])
    assert result.exit_code == 0, result.output
    assert "1 updated" in result.output
    items = wishlist_mod.load_wishlist()
    assert items[0]["max_price"] == 3.0


def test_bugfixwishlist_clear_target_entry_missing_title_does_not_crash(tmp_config):
    wishlist_mod.save_wishlist([{"asin": "B002", "max_price": 5}])
    runner = CliRunner()
    result = runner.invoke(cli, ["wishlist", "update", "B002", "--clear-target"])
    assert result.exit_code == 0, result.output
    assert "1 updated" in result.output
    items = wishlist_mod.load_wishlist()
    assert items[0]["max_price"] is None


def test_bugfixwishlist_sync_dedupes_repeated_asin_in_response(mock_client, tmp_config):
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


def test_bugfixwishlist_add_rejects_negative_max_price(mock_client, tmp_config):
    runner = CliRunner()
    result = runner.invoke(cli, ["wishlist", "add", "B00R6S1RCY", "--max-price=-5"])
    assert result.exit_code != 0
    assert "-5" in result.output or "Invalid value" in result.output
    assert wishlist_mod.load_wishlist() == []


def test_bugfixwishlist_sync_rejects_negative_max_price(mock_client, tmp_config):
    mock_client.get_wishlist.return_value = [make_product(asin="NEG1")]
    runner = CliRunner()
    result = runner.invoke(cli, ["wishlist", "sync", "--max-price=-5"])
    assert result.exit_code != 0
    assert wishlist_mod.load_wishlist() == []


def test_bugfixwishlist_add_author_rejects_negative_max_price(mock_client, tmp_config):
    runner = CliRunner()
    result = runner.invoke(
        cli, ["wishlist", "add", "--author", "Some Author", "--max-price=-5"]
    )
    assert result.exit_code != 0
    assert wishlist_mod.load_wishlist() == []


def test_bugfixwishlist_add_does_not_hold_wishlist_lock_during_api_call(
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

    monkeypatch.setattr(wishlist_service_mod, "wishlist_lock", tracked_lock)
    mock_client.get_product.side_effect = get_product

    result = CliRunner().invoke(cli, ["wishlist", "add", "B00R6S1RCY", "B00R6S1RCY"])

    assert result.exit_code == 0, result.output
    mock_client.get_product.assert_called_once_with("B00R6S1RCY")
    assert [item["asin"] for item in wishlist_mod.load_wishlist()] == ["B00R6S1RCY"]


def test_bugfixwishlist_purge_does_not_hold_wishlist_lock_during_api_or_prompt(
    mock_client, tmp_config, monkeypatch
):
    wishlist_mod.save_wishlist([{"asin": "OWN1", "title": "Owned", "max_price": None}])
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

    def get_library_asins():
        assert not lock_held
        return {"OWN1"}

    def confirm(*args, **kwargs):
        assert not lock_held
        return True

    monkeypatch.setattr(wishlist_service_mod, "wishlist_lock", tracked_lock)
    monkeypatch.setattr(click, "confirm", confirm)
    mock_client.get_library_asins.side_effect = get_library_asins

    result = CliRunner().invoke(cli, ["wishlist", "purge", "--owned"])

    assert result.exit_code == 0, result.output
    assert not lock_held
    assert wishlist_mod.load_wishlist() == []


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
def test_bugfixwishlist_wishlist_mutations_preserve_invalid_root(
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
def test_bugfixwishlist_cli_max_price_rejects_nonfinite_values(args, value, tmp_config):
    result = CliRunner().invoke(cli, [*args, value])

    assert result.exit_code != 0
    assert "finite" in result.output
    assert not constants_mod.WISHLIST_FILE.exists()


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_bugfixwishlist_interactive_nonfinite_target_is_not_saved(value, tmp_config):
    from audible_deals.cli.interactive import _interactive_browse

    @click.command()
    def command():
        _interactive_browse([make_product(asin="FINITE1")])

    result = CliRunner().invoke(command, input=f"w 1\n{value}\nq\n")

    assert result.exit_code == 0, result.output
    assert wishlist_mod.load_wishlist()[0]["max_price"] is None


def test_bugfixwishlist_interactive_preserves_invalid_wishlist_root(tmp_config):
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


def test_bugfixwishlist_watch_not_found_entry_without_title_does_not_crash(
    mock_client, tmp_config
):
    wishlist_mod.save_wishlist([{"asin": "GONE1", "max_price": 5}])
    mock_client.get_products_batch.return_value = []

    result = CliRunner().invoke(cli, ["watch"])

    assert result.exit_code == 0, result.output
    assert "Not found: GONE1" in result.output


def test_bugfixwishlist_wishlist_commands_skip_warn_once_and_preserve_invalid_entries(
    tmp_config,
):
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


def test_bugfixwishlist_repair_healthy_wishlist_does_not_write_or_create_backup(
    tmp_config,
):
    original = b'[{"asin":"GOOD1","max_price":null}]\n'
    constants_mod.WISHLIST_FILE.write_bytes(original)

    result = CliRunner().invoke(cli, ["wishlist", "repair", "--yes"])

    assert result.exit_code == 0, result.output
    assert "no invalid entries" in result.output
    assert constants_mod.WISHLIST_FILE.read_bytes() == original
    assert not (tmp_config / "wishlist.json.bak").exists()


def test_bugfixwishlist_repair_dry_run_reports_indexes_without_writing(tmp_config):
    original = b'[{"asin":"GOOD1","max_price":null},{"asin":"BAD","max_price":-1}]'
    constants_mod.WISHLIST_FILE.write_bytes(original)

    result = CliRunner().invoke(cli, ["wishlist", "repair", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "[1] max_price must be null or a finite non-negative number" in result.output
    assert "No files changed" in result.output
    assert constants_mod.WISHLIST_FILE.read_bytes() == original
    assert not (tmp_config / "wishlist.json.bak").exists()


def test_bugfixwishlist_repair_cancellation_leaves_source_untouched(tmp_config):
    original = b'[{"asin":"GOOD1"},{"asin":"BAD","max_price":-1}]'
    constants_mod.WISHLIST_FILE.write_bytes(original)

    result = CliRunner().invoke(cli, ["wishlist", "repair"], input="n\n")

    assert result.exit_code == 1
    assert constants_mod.WISHLIST_FILE.read_bytes() == original
    assert not (tmp_config / "wishlist.json.bak").exists()


def test_bugfixwishlist_confirmed_repair_preserves_valid_data_order_and_exact_backup(
    tmp_config,
):
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


def test_bugfixwishlist_repair_uses_next_available_backup_name(tmp_config):
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
def test_bugfixwishlist_repair_refuses_malformed_or_non_list_roots(
    tmp_config, contents
):
    constants_mod.WISHLIST_FILE.write_bytes(contents)

    result = CliRunner().invoke(cli, ["wishlist", "repair", "--yes"])

    assert result.exit_code != 0
    assert "Cannot repair wishlist" in result.output
    assert constants_mod.WISHLIST_FILE.read_bytes() == contents
    assert not (tmp_config / "wishlist.json.bak").exists()


def test_bugfixwishlist_repair_aborts_if_wishlist_changes_during_confirmation(
    tmp_config, monkeypatch
):
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
