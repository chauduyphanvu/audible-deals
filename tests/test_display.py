"""Terminal presentation behavior."""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from audible_deals.presentation.common import (
    _buy_cell,
    _discount_color,
    _pph_str,
    discount_str,
    price_str,
    rating_str,
)
from audible_deals.presentation.products import (
    ProductDisplayOptions,
    ProductLayout,
    calculate_product_layout,
    display_comparison,
    display_product_detail,
    display_products,
)
from audible_deals.presentation.reports import (
    display_categories,
    display_library_stats,
    display_price_history,
    display_recap,
    display_summary,
    display_watch_table,
    display_wishlist,
)
from tests.conftest import make_product


def _capture(func, *args, width: int = 120, **kwargs):
    """Run a display function and capture its Rich output as plain text."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=width)
    # Temporarily replace the module console
    from audible_deals.presentation import terminal as display_mod

    original = display_mod.console
    display_mod.console = console
    try:
        func(*args, **kwargs)
    finally:
        display_mod.console = original
    return buf.getvalue()


class TestPriceStr:
    def test_normal(self):
        assert price_str(9.99) == "$9.99"

    def test_none(self):
        assert price_str(None) == "-"

    def test_zero(self):
        assert price_str(0.0) == "$0.00"

    def test_rounding(self):
        assert price_str(1.999) == "$2.00"

    def test_large_integer_is_exact(self):
        price = 10**400
        assert price_str(price) == f"${price}.00"


class TestRatingStr:
    def test_normal(self):
        assert rating_str(4.5, 1000) == "4.5 (1,000)"

    def test_zero_rating(self):
        assert rating_str(0.0) == "-"

    def test_preserves_tenths(self):
        assert rating_str(4.3) == "4.3"
        assert rating_str(4.7) == "4.7"

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

    def test_low(self):
        assert _discount_color(20) == "dim"

    def test_boundary_80(self):
        assert _discount_color(80) == "bold green"

    def test_boundary_79(self):
        assert _discount_color(79) == "yellow"

    def test_boundary_50(self):
        assert _discount_color(50) == "yellow"

    def test_boundary_49(self):
        assert _discount_color(49) == "dim"


class TestPphStr:
    def test_normal(self):
        assert _pph_str(make_product(price=10.0, length_minutes=300)) == "$2.00"

    def test_none_price(self):
        assert _pph_str(make_product(price=None, length_minutes=300)) == "-"

    def test_zero_hours(self):
        assert _pph_str(make_product(price=10.0, length_minutes=0)) == "-"

    def test_cheap_per_hour(self):
        assert _pph_str(make_product(price=1.0, length_minutes=1200)) == "$0.05"


def test_product_display_models_are_frozen():
    assert ProductDisplayOptions.__dataclass_params__.frozen
    assert ProductLayout.__dataclass_params__.frozen


def test_views_and_progress_share_terminal_console():
    from audible_deals.presentation import products, reports, terminal

    assert products.terminal is terminal
    assert reports.terminal is terminal
    assert terminal.create_scan_progress().console is terminal.console


def test_product_layout_is_pure_and_preserves_width_boundaries():
    product = make_product(
        asin="B00BOUND01",
        price=20.6,
        list_price=103.0,
        length_minutes=1_236,
        num_ratings=18_432,
    )
    options = ProductDisplayOptions(
        show_url=True,
        credit_price=11.25,
        hist_context={product.asin: -20},
        match_context={product.asin: "favorite narrator and exact author"},
    )

    layouts = {
        width: calculate_product_layout([product], options, width)
        for width in (60, 79, 80, 100, 119, 120, 160)
    }

    assert [layouts[width].mode for width in (60, 79)] == ["cards", "cards"]
    assert [layouts[width].mode for width in (80, 100, 119)] == [
        "compact",
        "compact",
        "compact",
    ]
    assert [layouts[width].mode for width in (120, 160)] == ["wide", "wide"]
    assert layouts[80].title_width == 32
    assert layouts[100].title_width == 52
    assert layouts[119].title_width == 71
    assert layouts[120].show_buy_column
    assert layouts[120].show_history_column
    assert not layouts[120].show_match_column
    assert layouts[160].show_match_column
    assert not layouts[160].show_url_column
    assert calculate_product_layout([product], options, 160) == layouts[160]


def test_product_layout_accepts_empty_match_context_at_wide_width():
    product = make_product()

    layout = calculate_product_layout(
        [product], ProductDisplayOptions(match_context={}), 120
    )

    assert layout.mode == "wide"
    assert layout.show_match_column
    assert layout.match_width == 12


def test_product_display_options_detach_and_freeze_mutable_inputs():
    product = make_product()
    atl_asins = {product.asin}
    hist_context = {product.asin: -20}
    match_context = {product.asin: "favorite narrator"}
    options = ProductDisplayOptions(
        atl_asins=atl_asins,
        hist_context=hist_context,
        match_context=match_context,
    )
    original_layout = calculate_product_layout([product], options, 160)

    atl_asins.add("B00OTHER")
    hist_context[product.asin] = 123_456_789
    match_context[product.asin] = "x" * 100

    assert options.atl_asins == frozenset({product.asin})
    assert options.hist_context == {product.asin: -20}
    assert options.match_context == {product.asin: "favorite narrator"}
    assert calculate_product_layout([product], options, 160) == original_layout

    with pytest.raises(AttributeError):
        options.atl_asins.add("B00BLOCKED")
    with pytest.raises(TypeError):
        options.hist_context[product.asin] = 0
    with pytest.raises(TypeError):
        options.match_context[product.asin] = "changed"


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

    def test_locale_aware_pph_header(self):
        products = [make_product(price=10.0, length_minutes=600)]
        out = _capture(display_products, products, currency="£")
        assert "£/hr" in out
        assert "$/hr" not in out


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

    def test_empty_list(self):
        """display_comparison with empty list should not crash."""
        out = _capture(display_comparison, [])
        assert "Comparison" in out

    def test_locale_aware_pph_row(self):
        """display_comparison uses the first product's currency for the $/hr row label."""
        p1 = make_product(asin="A1", title="Book A", price=5.0, length_minutes=600)
        p2 = make_product(asin="A2", title="Book B", price=10.0, length_minutes=600)
        # make_product defaults to locale="us" which gives "$"
        out = _capture(display_comparison, [p1, p2])
        assert "$/hr" in out

    def test_locale_aware_pph_row_uk(self):
        """display_comparison uses £/hr when products have uk locale."""
        p1 = make_product(
            asin="A1", title="Book A", price=5.0, length_minutes=600, locale="uk"
        )
        p2 = make_product(
            asin="A2", title="Book B", price=10.0, length_minutes=600, locale="uk"
        )
        out = _capture(display_comparison, [p1, p2])
        assert "£/hr" in out


class TestDisplayProductsShowUrl:
    def test_show_url_adds_column(self):
        products = [make_product(asin="B001", title="URL Book", price=3.99)]
        out = _capture(display_products, products, width=200, show_url=True)
        assert "URL" in out
        assert "https://www.audible.com/pd/B001" in out

    def test_show_url_false_no_column(self):
        products = [make_product(asin="B001", title="No URL Book", price=3.99)]
        out = _capture(display_products, products, show_url=False)
        assert "/pd/B001" not in out

    def test_narrow_output_keeps_table_and_lists_each_full_url_once(self):
        products = [
            make_product(asin="B001", title="First URL Book", price=3.99),
            make_product(asin="B002", title="Second URL Book", price=4.99),
        ]

        out = _capture(display_products, products, width=80, show_url=True)

        assert "Title / Author" in out
        assert "Price" in out
        assert out.index("URLs") > out.index("Rating")
        for product in products:
            assert out.count(product.url) == 1
        assert "1. https://" in out

    def test_180_columns_preserves_full_url_with_optional_columns(self):
        product = make_product(
            asin="B00R6S1RCY1234",
            title="A deliberately long boundary title " * 4,
            price=3.0,
            list_price=5.0,
            locale="au",
        )

        out = _capture(
            display_products,
            [product],
            width=180,
            show_url=True,
            credit_price=11.25,
            hist_context={product.asin: -10},
            match_context={product.asin: "exact title match"},
        )

        assert out.count(product.url) == 1
        assert "URLs" not in out


class TestDisplayProductsFullUrl:
    def test_show_url_shows_full_url(self):
        products = [make_product(asin="B001TEST", title="URL Book", price=3.99)]
        out = _capture(display_products, products, width=200, show_url=True)
        assert "https://www.audible.com/pd/B001TEST" in out


class TestDisplaySummary:
    def test_basic_breakdown(self):
        out = _capture(display_summary, 10, {"language": 3, "narrator": 2})
        assert "10" in out
        assert "5 filtered out: 3 by language, 2 by narrator" in out

    def test_single_filter_breakdown(self):
        out = _capture(display_summary, 10, {"language": 5})
        assert "5 filtered out: 5 by language" in out

    def test_empty_breakdown(self):
        out = _capture(display_summary, 10, {})
        assert "filtered out" not in out

    def test_with_max_price(self):
        out = _capture(display_summary, 10, {}, max_price=5.0)
        assert "$5.00" in out

    def test_editions_and_series(self):
        out = _capture(display_summary, 10, {}, editions_removed=3, series_collapsed=2)
        assert "3 duplicate editions removed" in out
        assert "2 series collapsed" in out


class TestATLBadge:
    def test_star_shown_for_atl_asin(self):
        products = [make_product(asin="BXXX", title="ATL Book", price=2.99)]
        out = _capture(display_products, products, atl_asins={"BXXX"})
        assert "★" in out

    def test_star_not_shown_without_atl_asins(self):
        products = [make_product(asin="BXXX", title="Normal Book", price=5.99)]
        out = _capture(display_products, products)
        assert "★" not in out

    def test_star_not_shown_for_non_atl_asin(self):
        products = [
            make_product(asin="BXXX", title="ATL Book", price=2.99),
            make_product(asin="BYYY", title="Not ATL Book", price=4.99),
        ]
        out_atl = _capture(display_products, products, atl_asins={"BXXX"})
        assert "★" in out_atl

    def test_star_not_shown_for_none_atl_asins(self):
        products = [make_product(asin="BXXX", title="Normal Book", price=5.99)]
        out = _capture(display_products, products, atl_asins=None)
        assert "★" not in out

    def test_star_not_shown_for_empty_atl_set(self):
        products = [make_product(asin="BXXX", title="Normal Book", price=5.99)]
        out = _capture(display_products, products, atl_asins=set())
        assert "★" not in out


class TestDisplayWatchTableZeroTarget:
    def test_zero_target_shows_price_and_buy(self):
        """$0.00 target should display as '$0.00' and trigger BUY."""
        p = make_product(asin="W1", title="Free Book", price=0.0, list_price=10.0)
        out = _capture(display_watch_table, [p], {"W1": 0.0})
        assert "$0.00" in out
        assert "BUY" in out

    def test_none_target_shows_dash(self):
        """None target should display as '-' and not trigger BUY."""
        p = make_product(asin="W1", title="Some Book", price=5.0, list_price=5.0)
        out = _capture(display_watch_table, [p], {"W1": None})
        assert "BUY" not in out
        assert "-" in out

    def test_large_integer_target_renders_without_overflow(self):
        target = 10**400
        p = make_product(asin="HUGE1", title="Huge Target", price=5.0)

        out = _capture(display_watch_table, [p], {"HUGE1": target})

        assert "$10000000" in out


class TestDisplayWatchTableShowUrl:
    def test_narrow_output_lists_urls_by_asin_after_normal_table(self):
        products = [
            make_product(asin="W1", title="First", price=2.0),
            make_product(asin="W2", title="Second", price=3.0),
        ]

        out = _capture(
            display_watch_table,
            products,
            {"W1": 5.0, "W2": 5.0},
            width=80,
            show_url=True,
        )

        assert "Title" in out
        assert "Status" in out
        assert out.index("URLs") > out.index("Status")
        for product in products:
            assert out.count(product.url) == 1
            assert f"{product.asin}: https://" in out

    def test_wide_output_retains_url_column_without_duplicate_list(self):
        product = make_product(asin="WIDE1", title="Wide", price=2.0)

        out = _capture(
            display_watch_table,
            [product],
            {"WIDE1": 5.0},
            width=200,
            show_url=True,
        )

        assert "URL" in out
        assert out.count(product.url) == 1
        assert "URLs" not in out

    def test_180_columns_preserves_full_url_with_buy_column(self):
        product = make_product(asin="B00R6S1RCY", title="Boundary", price=2.0)

        out = _capture(
            display_watch_table,
            [product],
            {product.asin: 5.0},
            width=180,
            show_url=True,
            credit_price=11.25,
        )

        assert out.count(product.url) == 1
        assert "URLs" not in out


class TestWishlistAndRecapTargets:
    def test_large_integer_target_renders_in_wishlist_and_recap(self):
        target = 10**400

        wishlist = _capture(
            display_wishlist,
            [{"asin": "HUGE1", "title": "Huge Target", "max_price": target}],
            [],
        )
        recap = _capture(
            display_recap,
            [],
            [],
            [],
            7,
            atl_hits=[
                {
                    "asin": "HUGE1",
                    "title": "Huge Target",
                    "price": 1.0,
                    "target": target,
                }
            ],
            width=1000,
        )

        assert "$10000000" in wishlist
        assert f"target ${target}.00" in recap


class TestDisplayLibraryStats:
    def test_empty(self):
        out = _capture(display_library_stats, [])
        assert "empty" in out.lower()

    def test_total_books_and_hours(self):
        products = [
            make_product(asin="S1", length_minutes=600, rating=4.0),
            make_product(asin="S2", length_minutes=300, rating=4.5),
        ]
        out = _capture(display_library_stats, products)
        assert "2" in out
        assert "15" in out  # (600+300)/60 = 15 h

    def test_top_author_appears(self):
        products = [
            make_product(asin="S3", authors=["Big Author"], length_minutes=300),
            make_product(asin="S4", authors=["Big Author"], length_minutes=300),
            make_product(asin="S5", authors=["Other"], length_minutes=300),
        ]
        out = _capture(display_library_stats, products)
        assert "Big Author" in out

    def test_locale_currency_unused(self):
        products = [make_product(asin="S6", length_minutes=300, rating=0)]
        out = _capture(display_library_stats, products, currency="£")
        assert "empty" not in out.lower()


class TestBuyCell:
    def test_cash(self):
        assert "cash" in _buy_cell(make_product(price=3.99), 11.25)

    def test_credit(self):
        assert "credit" in _buy_cell(make_product(price=24.99), 11.25)

    def test_plus(self):
        assert "plus" in _buy_cell(make_product(in_plus_catalog=True), 11.25)

    def test_missing_price(self):
        assert "-" in _buy_cell(make_product(price=None), 11.25)


class TestCreditAwareDisplay:
    def test_products_buy_column_with_credit_price(self):
        products = [
            make_product(asin="B001", price=3.99),
            make_product(asin="B002", price=24.99),
        ]
        out = _capture(display_products, products, credit_price=11.25)
        assert "Buy" in out
        assert "cash" in out
        assert "credit" in out

    def test_products_no_buy_column_without_credit_price(self):
        products = [make_product(asin="B003", price=3.99)]
        out = _capture(display_products, products)
        assert "Buy" not in out

    def test_detail_buy_line(self):
        out = _capture(
            display_product_detail, make_product(price=24.99), credit_price=11.25
        )
        assert "Buy with" in out
        assert "credit" in out

    def test_detail_no_buy_line_without_credit_price(self):
        out = _capture(display_product_detail, make_product(price=24.99))
        assert "Buy with" not in out

    def test_comparison_buy_row(self):
        products = [
            make_product(asin="B004", price=3.99),
            make_product(asin="B005", price=24.99),
        ]
        out = _capture(display_comparison, products, credit_price=11.25)
        assert "Buy" in out
        assert "cash" in out
        assert "credit" in out

    def test_watch_table_buy_column(self):
        products = [make_product(asin="B006", price=24.99)]
        out = _capture(display_watch_table, products, {"B006": 5.0}, credit_price=11.25)
        assert "Buy" in out
        assert "credit" in out


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
