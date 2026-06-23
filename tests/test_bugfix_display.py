"""Regression tests for confirmed bugs in audible_deals.display."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from audible_deals.display import (
    display_price_history,
    display_recap,
    display_wishlist,
)


def _capture(func, *args, width: int = 120, **kwargs):
    """Run a display function and capture its Rich output as plain text."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=width)
    import audible_deals.display as display_mod

    original = display_mod.console
    display_mod.console = console
    try:
        func(*args, **kwargs)
    finally:
        display_mod.console = original
    return buf.getvalue()


class TestRecapEmptyAtlList:
    def test_no_nothing_to_report_when_atl_section_rendered(self):
        """An empty atl_hits list still renders the ATL section, so the
        contradictory 'Nothing to report.' line must not appear."""
        out = _capture(display_recap, [], [], [], 7, atl_hits=[], atl_all=False)
        assert "Wishlist items at all-time low:" in out
        assert "Nothing to report." not in out

    def test_nothing_to_report_when_atl_none(self):
        """With atl_hits=None the ATL section is skipped and the fallback shows."""
        out = _capture(display_recap, [], [], [], 7, atl_hits=None)
        assert "Nothing to report." in out


class TestWishlistZeroTarget:
    def test_zero_max_price_shows_target(self):
        """A stored max_price of 0.0 is a valid 'only if free' target and must
        render as $0.00, not '-'."""
        out = _capture(
            display_wishlist,
            [{"asin": "B00R6S1RCY", "title": "Free Book", "max_price": 0.0}],
            [],
        )
        assert "$0.00" in out

    def test_missing_max_price_shows_dash(self):
        out = _capture(
            display_wishlist,
            [{"asin": "B00R6S1RCY", "title": "No Target Book"}],
            [],
        )
        assert "$" not in out

    def test_author_zero_max_price_shows_target(self):
        out = _capture(
            display_wishlist,
            [],
            [{"author": "Some Author", "max_price": 0.0, "added": "2024-01-01"}],
        )
        assert "$0.00" in out


class TestPriceHistoryNonNumeric:
    def test_non_numeric_price_does_not_crash(self):
        """A corrupt/hand-edited entry with a string price must be skipped, not
        crash the CLI."""
        entries = [
            {"date": "2026-06-01", "price": "n/a", "title": "X"},
            {"date": "2026-06-02", "price": 5.0, "title": "X"},
        ]
        out = _capture(display_price_history, entries, "B00ASIN")
        assert "$5.00" in out

    def test_missing_price_key_does_not_crash(self):
        entries = [
            {"date": "2026-06-01", "title": "X"},
            {"date": "2026-06-02", "price": 3.5, "title": "X"},
        ]
        out = _capture(display_price_history, entries, "B00ASIN")
        assert "$3.50" in out

    def test_all_non_numeric_reports_no_prices(self):
        entries = [{"date": "2026-06-01", "price": "n/a", "title": "X"}]
        out = _capture(display_price_history, entries, "B00ASIN")
        assert "No numeric prices" in out
