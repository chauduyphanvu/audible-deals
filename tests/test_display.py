"""Tests for audible_deals.display — formatting helpers and table rendering."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from audible_deals.display import (
    _discount_color,
    _pph_str,
    discount_str,
    display_categories,
    display_comparison,
    display_product_detail,
    display_products,
    display_summary,
    price_str,
    rating_str,
)
from tests.conftest import make_product


# ===================================================================
# Formatting helpers
# ===================================================================

class TestPriceStr:
    def test_normal(self):
        assert price_str(9.99) == "$9.99"

    def test_none(self):
        assert price_str(None) == "-"

    def test_zero(self):
        assert price_str(0.0) == "$0.00"

    def test_rounding(self):
        assert price_str(1.999) == "$2.00"


class TestRatingStr:
    def test_normal(self):
        assert rating_str(4.5, 1000) == "4.5 (1,000)"

    def test_zero_rating(self):
        assert rating_str(0.0) == "-"

    def test_rounds_to_half(self):
        # 4.3 → rounds to 4.5 (nearest 0.5)
        assert rating_str(4.3) == "4.5"

    def test_no_num_ratings(self):
        assert rating_str(4.0, 0) == "4.0"


class TestDiscountStr:
    def test_normal(self):
        assert discount_str(75) == "-75%"

    def test_none(self):
        assert discount_str(None) == ""

    def test_zero(self):
        assert discount_str(0) == ""

    def test_negative(self):
        assert discount_str(-5) == ""


class TestDiscountColor:
    def test_high(self):
        assert _discount_color(85) == "bold green"

    def test_medium(self):
        assert _discount_color(50) == "yellow"

    def test_low(self):
        assert _discount_color(20) == "dim"

    def test_boundary_70(self):
        assert _discount_color(70) == "bold green"

    def test_boundary_40(self):
        assert _discount_color(40) == "yellow"

    def test_boundary_39(self):
        assert _discount_color(39) == "dim"


class TestPphStr:
    def test_normal(self):
        assert _pph_str(10.0, 5.0) == "$2.00"

    def test_none_price(self):
        assert _pph_str(None, 5.0) == "-"

    def test_zero_hours(self):
        assert _pph_str(10.0, 0.0) == "-"

    def test_cheap_per_hour(self):
        assert _pph_str(1.0, 20.0) == "$0.05"


# ===================================================================
# Table rendering (smoke tests — verify no crash and output contains key data)
# ===================================================================

def _capture(func, *args, **kwargs):
    """Run a display function and capture its Rich output as plain text."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=120)
    # Temporarily replace the module console
    import audible_deals.display as display_mod
    original = display_mod.console
    display_mod.console = console
    try:
        func(*args, **kwargs)
    finally:
        display_mod.console = original
    return buf.getvalue()


class TestDisplayProducts:
    def test_empty(self):
        out = _capture(display_products, [])
        assert "No products found" in out

    def test_renders_title(self):
        products = [make_product(asin="B001", title="My Book", price=3.99)]
        out = _capture(display_products, products, title="Test Results")
        assert "Test Results" in out
        assert "My Book" in out
        assert "B001" in out

    def test_price_coloring_with_max_price(self):
        products = [make_product(price=2.00)]
        out = _capture(display_products, products, max_price=5.0)
        assert "$2.00" in out

    def test_pph_column(self):
        products = [make_product(price=10.0, length_minutes=600)]
        out = _capture(display_products, products)
        assert "$1.00" in out  # 10 / 10hrs

    def test_discount_displayed(self):
        products = [make_product(price=5.0, list_price=20.0)]
        out = _capture(display_products, products)
        assert "-75%" in out

    def test_plus_indicator(self):
        products = [make_product(in_plus_catalog=True)]
        out = _capture(display_products, products)
        assert "[+]" in out


class TestDisplayCategories:
    def test_empty(self):
        out = _capture(display_categories, [])
        assert "No categories found" in out

    def test_renders(self):
        cats = [{"id": "123", "name": "Fantasy"}]
        out = _capture(display_categories, cats, title="Genres")
        assert "Genres" in out
        assert "123" in out
        assert "Fantasy" in out


class TestDisplayProductDetail:
    def test_renders_all_fields(self):
        p = make_product(
            asin="B00DETAIL",
            title="Detail Book",
            subtitle="A Subtitle",
            authors=["Alice"],
            narrators=["Bob"],
            publisher="Pub Co",
            price=5.0,
            list_price=20.0,
            length_minutes=360,
            rating=4.5,
            num_ratings=500,
            series_name="My Series",
            series_position="2",
            categories=["Fiction", "Mystery"],
            language="english",
            release_date="2024-01-01",
            in_plus_catalog=True,
        )
        out = _capture(display_product_detail, p)
        assert "Detail Book: A Subtitle" in out
        assert "Alice" in out
        assert "Bob" in out
        assert "$5.00" in out
        assert "$20.00" in out
        assert "-75% off" in out
        assert "6.0 hours" in out
        assert "My Series" in out
        assert "Book 2" in out
        assert "Fiction" in out
        assert "english" in out
        assert "2024-01-01" in out
        assert "Audible Plus" in out
        assert "B00DETAIL" in out


class TestDisplayComparison:
    def test_renders(self):
        p1 = make_product(asin="A1", title="Book A", price=5.0, length_minutes=600)
        p2 = make_product(asin="A2", title="Book B", price=10.0, length_minutes=600)
        out = _capture(display_comparison, [p1, p2])
        assert "Book A" in out
        assert "Book B" in out
        assert "A1" in out
        assert "A2" in out
        assert "Best value" in out

    def test_no_priced_items(self):
        p1 = make_product(asin="A1", price=None)
        p2 = make_product(asin="A2", price=None)
        out = _capture(display_comparison, [p1, p2])
        assert "Best value" not in out


class TestDisplaySummary:
    def test_basic(self):
        out = _capture(display_summary, 10, 5)
        assert "10" in out
        assert "5 filtered out" in out

    def test_with_max_price(self):
        out = _capture(display_summary, 10, 0, max_price=5.0)
        assert "$5.00" in out

    def test_editions_and_series(self):
        out = _capture(display_summary, 10, 0, editions_removed=3, series_collapsed=2)
        assert "3 duplicate editions removed" in out
        assert "2 series collapsed" in out
