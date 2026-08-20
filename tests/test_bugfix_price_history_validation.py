"""Regression tests for price_history / validation bug fixes."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

import audible_deals.constants as constants_mod
import audible_deals.price_history as price_history
import audible_deals.wishlist as wishlist_mod
from audible_deals.cli import cli
from audible_deals.validation import validate_webhook_url
from tests.conftest import make_product


# ---------------------------------------------------------------------------
# Bug 16: find_wishlist_hits must skip non-numeric/None latest price or
# max_price instead of crashing.
# ---------------------------------------------------------------------------


class TestFindWishlistHitsNumericGuard:
    def _write_history(self, asin: str, entries: list[dict]) -> None:
        constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        (constants_mod.HISTORY_DIR / f"{asin}.json").write_text(
            json.dumps({"marketplaces": {"us": entries}})
        )

    def test_null_latest_price_is_skipped(self, tmp_config):
        wishlist_mod.save_wishlist(
            [{"asin": "B00NULL001", "title": "T", "max_price": 10.0, "added": ""}]
        )
        self._write_history(
            "B00NULL001",
            [
                {"date": "2024-01-01", "price": 12.0, "title": "T"},
                {"date": "2024-01-02", "price": None, "title": "T"},
            ],
        )
        assert price_history.find_wishlist_hits() == []

    def test_missing_price_key_is_skipped(self, tmp_config):
        wishlist_mod.save_wishlist(
            [{"asin": "B00MISS001", "title": "T", "max_price": 10.0, "added": ""}]
        )
        self._write_history(
            "B00MISS001",
            [{"date": "2024-01-02", "title": "T"}],
        )
        assert price_history.find_wishlist_hits() == []

    def test_string_max_price_is_skipped(self, tmp_config):
        wishlist_mod.save_wishlist(
            [{"asin": "B00STR0001", "title": "T", "max_price": "10", "added": ""}]
        )
        self._write_history(
            "B00STR0001",
            [{"date": "2024-01-02", "price": 7.5, "title": "T"}],
        )
        assert price_history.find_wishlist_hits() == []

    def test_valid_numeric_hit_still_matches(self, tmp_config):
        item = {"asin": "B00HIT0001", "title": "T", "max_price": 10.0, "added": ""}
        wishlist_mod.save_wishlist([item])
        self._write_history(
            "B00HIT0001",
            [{"date": "2024-01-02", "price": 7.5, "title": "T"}],
        )
        assert price_history.find_wishlist_hits() == [item]


class TestMarketplaceScopedHistory:
    def test_identical_asins_never_share_marketplace_prices(self, tmp_config):
        asin = "B00MARKET1"
        price_history.record_prices(
            [
                make_product(asin=asin, locale="us", price=10.0, title="US title"),
                make_product(asin=asin, locale="uk", price=5.0, title="UK title"),
            ]
        )

        assert price_history.load_price_history(asin, "us")[0]["price"] == 10.0
        assert price_history.load_price_history(asin, "uk")[0]["price"] == 5.0

    def test_legacy_history_is_not_assigned_to_a_marketplace(self, tmp_config):
        asin = "B00LEGACY1"
        constants_mod.HISTORY_DIR.mkdir(parents=True)
        (constants_mod.HISTORY_DIR / f"{asin}.json").write_text(
            json.dumps([{"date": "2024-01-01", "price": 10.0}])
        )

        assert price_history.load_price_history(asin, "us") == []


class TestLegacyHistoryMigration:
    def test_bulk_load_archives_once_with_collision_and_preserves_bytes(
        self, tmp_config, caplog
    ):
        constants_mod.HISTORY_DIR.mkdir(parents=True)
        first = constants_mod.HISTORY_DIR / "B00LEGACY1.json"
        second = constants_mod.HISTORY_DIR / "B00LEGACY2.json"
        current = constants_mod.HISTORY_DIR / "B00CURRENT1.json"
        first_bytes = b'[ {"date": "2024-01-01", "price": 10.0} ]\n'
        second_bytes = b'[{"date":"2024-02-02","price":7.5}]'
        first.write_bytes(first_bytes)
        second.write_bytes(second_bytes)
        (constants_mod.HISTORY_DIR / "B00LEGACY1.json.legacy").write_bytes(b"older")
        current.write_text(
            json.dumps({"marketplaces": {"us": [{"date": "2026-01-01", "price": 3.0}]}})
        )

        with caplog.at_level(logging.WARNING):
            loaded = price_history.load_all_price_histories("us")

        assert loaded == {"B00CURRENT1": [{"date": "2026-01-01", "price": 3.0}]}
        assert not first.exists()
        assert not second.exists()
        assert (
            constants_mod.HISTORY_DIR / "B00LEGACY1.json.legacy.1"
        ).read_bytes() == first_bytes
        assert (
            constants_mod.HISTORY_DIR / "B00LEGACY2.json.legacy"
        ).read_bytes() == second_bytes
        messages = [
            r.message for r in caplog.records if "Legacy history migration" in r.message
        ]
        assert len(messages) == 1
        assert "archived 2" in messages[0]
        assert (constants_mod.HISTORY_DIR / ".legacy-migration.lock").exists()
        assert (constants_mod.HISTORY_DIR / ".B00LEGACY1.json.lock").exists()

        caplog.clear()
        assert price_history.load_all_price_histories("us") == loaded
        assert not [
            r for r in caplog.records if "Legacy history migration" in r.message
        ]

    def test_failed_rename_is_untouched_and_retried(
        self, tmp_config, monkeypatch, caplog
    ):
        constants_mod.HISTORY_DIR.mkdir(parents=True)
        legacy = constants_mod.HISTORY_DIR / "B00LEGACY3.json"
        original_bytes = b'[{"date":"2024-01-01","price":4}]'
        legacy.write_bytes(original_bytes)
        original_link = price_history.os.link

        def fail_legacy(source, target):
            if Path(source) == legacy:
                raise OSError("read-only filesystem")
            return original_link(source, target)

        monkeypatch.setattr(price_history.os, "link", fail_legacy)
        with caplog.at_level(logging.WARNING):
            assert price_history.load_all_price_histories() == {}
        assert legacy.read_bytes() == original_bytes
        assert "1 failed" in caplog.text

        monkeypatch.setattr(price_history.os, "link", original_link)
        caplog.clear()
        assert price_history.load_all_price_histories() == {}
        assert not legacy.exists()
        assert legacy.with_name(f"{legacy.name}.legacy").read_bytes() == original_bytes
        assert "archived 1" in caplog.text

    def test_archive_retries_when_backup_appears_during_move(
        self, tmp_config, monkeypatch
    ):
        constants_mod.HISTORY_DIR.mkdir(parents=True)
        legacy = constants_mod.HISTORY_DIR / "B00LEGACY5.json"
        original_bytes = b'[{"date":"2024-01-01","price":4}]\n'
        legacy.write_bytes(original_bytes)
        first_archive = legacy.with_name(f"{legacy.name}.legacy")
        original_link = price_history.os.link
        collision_created = False

        def link_with_collision(source, target):
            nonlocal collision_created
            if not collision_created and Path(target) == first_archive:
                first_archive.write_bytes(b"created concurrently")
                collision_created = True
            return original_link(source, target)

        monkeypatch.setattr(price_history.os, "link", link_with_collision)

        assert price_history.load_all_price_histories() == {}
        assert collision_created
        assert not legacy.exists()
        assert first_archive.read_bytes() == b"created concurrently"
        assert (
            legacy.with_name(f"{legacy.name}.legacy.1").read_bytes() == original_bytes
        )

    def test_history_all_keeps_json_stdout_clean(self, tmp_config):
        constants_mod.HISTORY_DIR.mkdir(parents=True)
        (constants_mod.HISTORY_DIR / "B00LEGACY4.json").write_text("[]")
        result = CliRunner().invoke(cli, ["history", "--all", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == {}
        assert "Legacy history migration" in result.stderr


# ---------------------------------------------------------------------------
# Bug 31: webhook SSRF check must reject CGNAT shared space (100.64.0.0/10).
# ---------------------------------------------------------------------------


class TestWebhookCgnatSsrf:
    def test_rejects_cgnat_shared_space(self, monkeypatch):
        import socket

        monkeypatch.setattr(
            "audible_deals.validation.socket.getaddrinfo",
            lambda host, port: [(socket.AF_INET, 0, 0, "", ("100.64.0.1", 0))],
        )
        with pytest.raises(click.BadParameter, match="non-public"):
            validate_webhook_url("https://shared.nat/hook")

    def test_still_accepts_public_ip(self, monkeypatch):
        import socket

        monkeypatch.setattr(
            "audible_deals.validation.socket.getaddrinfo",
            lambda host, port: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
        )
        validate_webhook_url("https://example.com/hook")  # should not raise
