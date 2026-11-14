"""Credit planner: the pure planning function."""

from __future__ import annotations

import dataclasses

import pytest

from audible_deals.credit_plan import plan_credits
from tests.conftest import make_product


def _book(asin: str, price: float | None, **overrides):
    return make_product(asin=asin, title=f"Book {asin}", price=price, **overrides)


def _asins(products) -> list[str]:
    return [p.asin for p in products]


class TestWithCreditPrice:
    def test_cash_for_cheap_books_credits_for_the_priciest(self):
        books = [
            _book("A", 38.49),
            _book("B", 4.99),
            _book("C", 28.99),
            _book("D", 26.95),
            _book("E", 3.50),
            _book("F", 12.99),
        ]

        plan = plan_credits(books, 2, 11.25)

        assert _asins(plan.credit) == ["A", "C"]
        assert _asins(plan.cash) == ["E", "B"]
        assert _asins(plan.wait) == ["D", "F"]

    def test_book_priced_at_credit_price_is_not_cash(self):
        books = [_book("EQ", 11.25), _book("BELOW", 11.24), _book("OVER", 20.0)]

        plan = plan_credits(books, 1, 11.25)

        assert _asins(plan.cash) == ["BELOW"]
        assert _asins(plan.credit) == ["OVER"]
        assert _asins(plan.wait) == ["EQ"]

    def test_book_priced_at_credit_price_can_take_a_credit(self):
        plan = plan_credits([_book("EQ", 11.25)], 1, 11.25)

        assert _asins(plan.credit) == ["EQ"]
        assert plan.cash == ()
        assert plan.saved == 0

    def test_totals(self):
        books = [
            _book("A", 38.49),
            _book("B", 28.99),
            _book("C", 26.95),
            _book("CASH1", 4.99),
            _book("CASH2", 3.01),
            _book("W", 12.99),
        ]

        plan = plan_credits(books, 3, 11.25)

        assert plan.credits == 3
        assert plan.credit_price == 11.25
        assert plan.cash_total == pytest.approx(8.00)
        assert plan.credit_value == pytest.approx(94.43)
        assert plan.value_per_credit == pytest.approx(31.48)
        assert plan.credits_unused == 0
        assert plan.saved == pytest.approx(94.43 - 3 * 11.25)

    def test_fewer_eligible_books_than_credits(self):
        books = [_book("A", 20.0), _book("CASH", 5.0)]

        plan = plan_credits(books, 3, 11.25)

        assert _asins(plan.credit) == ["A"]
        assert plan.wait == ()
        assert plan.credits_unused == 2
        assert plan.credit_value == 20.0
        assert plan.value_per_credit == 20.0
        assert plan.saved == pytest.approx(20.0 - 11.25)

    def test_only_cheap_books_leaves_every_credit_unused(self):
        plan = plan_credits([_book("A", 5.0), _book("B", 6.0)], 2, 11.25)

        assert plan.credit == ()
        assert _asins(plan.cash) == ["A", "B"]
        assert plan.credits_unused == 2
        assert plan.credit_value == 0
        assert plan.value_per_credit is None
        assert plan.saved == 0


class TestWithoutCreditPrice:
    def test_no_cash_section_and_priciest_books_take_credits(self):
        books = [_book("A", 4.99), _book("B", 30.0), _book("C", 12.0), _book("D", 1.0)]

        plan = plan_credits(books, 2, None)

        assert _asins(plan.credit) == ["B", "C"]
        assert plan.cash == ()
        assert _asins(plan.wait) == ["A", "D"]
        assert plan.cash_total == 0
        assert plan.saved is None
        assert plan.credit_price is None

    def test_totals(self):
        plan = plan_credits([_book("A", 30.0), _book("B", 20.0)], 5, None)

        assert plan.credit_value == 50.0
        assert plan.value_per_credit == 25.0
        assert plan.credits_unused == 3

    def test_no_books_at_all(self):
        plan = plan_credits([], 2, None)

        assert plan.credit == plan.cash == plan.plus == plan.wait == ()
        assert plan.value_per_credit is None
        assert plan.credits_unused == 2


class TestPlusAndUnpriced:
    def test_plus_titles_take_no_credit_and_no_cash(self):
        books = [
            _book("PLUSCHEAP", 2.0, in_plus_catalog=True),
            _book("PLUSDEAR", 40.0, in_plus_catalog=True),
            _book("A", 20.0),
        ]

        plan = plan_credits(books, 1, 11.25)

        assert _asins(plan.plus) == ["PLUSCHEAP", "PLUSDEAR"]
        assert _asins(plan.credit) == ["A"]
        assert plan.cash == ()
        assert plan.wait == ()
        assert plan.credit_value == 20.0

    def test_unpriced_titles_are_counted_and_dropped(self):
        books = [_book("NONE1", None), _book("NONE2", None), _book("A", 20.0)]

        plan = plan_credits(books, 2, None)

        assert plan.unavailable == 2
        assert _asins(plan.credit) == ["A"]
        assert plan.credits_unused == 1

    def test_unpriced_plus_title_is_unavailable_not_plus(self):
        plan = plan_credits([_book("P", None, in_plus_catalog=True)], 1, None)

        assert plan.unavailable == 1
        assert plan.plus == ()


class TestDeterminism:
    def test_equal_prices_break_ties_by_title_then_asin(self):
        books = [
            make_product(asin="Z1", title="Beta", price=20.0),
            make_product(asin="B2", title="Alpha", price=20.0),
            make_product(asin="A1", title="Alpha", price=20.0),
            make_product(asin="Y9", title="Gamma", price=20.0),
        ]

        plan = plan_credits(books, 2, None)

        assert _asins(plan.credit) == ["A1", "B2"]
        assert _asins(plan.wait) == ["Z1", "Y9"]

    def test_input_order_does_not_change_the_plan(self):
        books = [
            _book("A", 20.0),
            _book("B", 20.0),
            _book("C", 5.0),
            _book("D", 5.0),
            _book("E", 1.0, in_plus_catalog=True),
            _book("F", 1.0, in_plus_catalog=True),
        ]

        forward = plan_credits(books, 1, 11.25)
        backward = plan_credits(list(reversed(books)), 1, 11.25)

        assert forward == backward

    def test_cash_sorted_ascending_with_ties_by_title_then_asin(self):
        books = [
            make_product(asin="B", title="Same", price=3.0),
            make_product(asin="A", title="Same", price=3.0),
            make_product(asin="C", title="Other", price=2.0),
        ]

        plan = plan_credits(books, 1, 11.25)

        assert _asins(plan.cash) == ["C", "A", "B"]


def test_plan_is_immutable():
    plan = plan_credits([], 1, None)

    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.credits = 2  # type: ignore[misc]


def test_accepts_any_iterable():
    plan = plan_credits((p for p in [_book("A", 20.0)]), 1, None)

    assert _asins(plan.credit) == ["A"]
