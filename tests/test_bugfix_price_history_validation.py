"""Regression tests for price_history / validation bug fixes."""

from __future__ import annotations

import json

import click
import pytest

import audible_deals.constants as constants_mod
import audible_deals.price_history as price_history
import audible_deals.wishlist as wishlist_mod
from audible_deals.validation import validate_webhook_url


# ---------------------------------------------------------------------------
# Bug 16: find_wishlist_hits must skip non-numeric/None latest price or
# max_price instead of crashing.
# ---------------------------------------------------------------------------


class TestFindWishlistHitsNumericGuard:
    def _write_history(self, asin: str, entries: list[dict]) -> None:
        constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        (constants_mod.HISTORY_DIR / f"{asin}.json").write_text(json.dumps(entries))

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
