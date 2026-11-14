"""Tests for the cost of finishing a series."""

from __future__ import annotations

import dataclasses

import pytest

from audible_deals.series_cost import FinishCost, finish_cost, sum_finish_costs
from tests.conftest import make_product


def _gaps(*prices, **overrides):
    return [
        make_product(asin=f"G{i}", price=p, **overrides) for i, p in enumerate(prices)
    ]


class TestFinishCost:
    def test_without_credit_price_only_cash_fields_are_set(self):
        cost = finish_cost(_gaps(9.99, 4.99, 20.0), None)

        assert cost == FinishCost(
            priced=3, unavailable_count=0, plus_count=0, cash_total=34.98
        )
        assert cost.credit_price is None
        assert cost.credits_needed is None
        assert cost.cash_part is None
        assert cost.best_total is None
        assert cost.saved is None

    def test_mix_of_cash_cheaper_and_credit_cheaper_books(self):
        cost = finish_cost(_gaps(4.99, 20.0, 24.99, 34.98), 11.25)

        assert cost.priced == 4
        assert cost.cash_total == 84.96
        assert cost.credits_needed == 3
        assert cost.cash_part == 4.99
        assert cost.best_total == 38.74
        assert cost.saved == 46.22
        assert cost.credit_price == 11.25

    def test_book_priced_exactly_at_credit_price_goes_on_credit(self):
        cost = finish_cost(_gaps(11.25), 11.25)

        assert cost.credits_needed == 1
        assert cost.cash_part == 0.0
        assert cost.best_total == 11.25
        assert cost.saved == 0.0

    def test_just_under_credit_price_stays_cash(self):
        cost = finish_cost(_gaps(11.24), 11.25)

        assert cost.credits_needed == 0
        assert cost.cash_part == 11.24
        assert cost.saved == 0.0

    def test_all_cash(self):
        cost = finish_cost(_gaps(5.99, 7.0, 10.99), 11.25)

        assert cost.credits_needed == 0
        assert cost.cash_part == cost.cash_total == cost.best_total == 23.98
        assert cost.saved == 0.0

    def test_all_credit(self):
        cost = finish_cost(_gaps(14.99, 20.0, 11.25, 30.0), 11.25)

        assert cost.credits_needed == 4
        assert cost.cash_part == 0.0
        assert cost.best_total == 45.0
        assert cost.cash_total == 76.24
        assert cost.saved == 31.24

    def test_unavailable_and_plus_counts(self):
        gaps = [
            make_product(asin="P1", price=8.0, in_plus_catalog=True),
            make_product(asin="P2", price=15.0, in_plus_catalog=True),
            make_product(asin="C1", price=12.0),
            make_product(asin="U1", price=None),
            make_product(asin="U2", price=None, in_plus_catalog=True),
        ]

        cost = finish_cost(gaps, None)

        assert cost.priced == 3
        assert cost.unavailable_count == 2
        # Plus is borrowing, so those books still count toward purchase cost.
        assert cost.plus_count == 2
        assert cost.cash_total == 35.0

    @pytest.mark.parametrize("price", [float("nan"), float("inf"), True])
    def test_non_finite_or_non_numeric_prices_are_unavailable(self, price):
        cost = finish_cost(_gaps(price, 5.0), 11.25)

        assert cost.priced == 1
        assert cost.unavailable_count == 1
        assert cost.cash_total == 5.0

    def test_free_book_is_priced(self):
        cost = finish_cost(_gaps(0.0), 11.25)

        assert cost.priced == 1
        assert cost.unavailable_count == 0
        assert cost.credits_needed == 0

    @pytest.mark.parametrize("credit_price", [None, 11.25])
    def test_empty_input(self, credit_price):
        cost = finish_cost([], credit_price)

        assert cost.priced == cost.unavailable_count == cost.plus_count == 0
        assert cost.cash_total == 0.0
        if credit_price is None:
            assert cost.best_total is None
        else:
            assert cost.credits_needed == 0
            assert cost.cash_part == cost.best_total == cost.saved == 0.0

    def test_money_is_rounded_to_cents(self):
        cost = finish_cost(_gaps(0.1, 0.2, 0.3, 10.3, 20.7), 11.25)

        assert cost.cash_total == 31.6
        assert cost.cash_part == 10.9
        assert cost.best_total == 22.15
        assert cost.saved == 9.45

    def test_accepts_any_iterable(self):
        assert finish_cost(iter(_gaps(5.0, 6.0)), None).cash_total == 11.0

    def test_result_is_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            finish_cost([], None).priced = 1


class TestFinishCostToDict:
    def test_without_credit_price(self):
        assert finish_cost(_gaps(5.0, None), None).to_dict() == {
            "books": 1,
            "cash_total": 5.0,
            "unavailable": 1,
            "plus": 0,
        }

    def test_with_credit_price(self):
        data = finish_cost(_gaps(5.0, 20.0), 11.25).to_dict()

        assert data == {
            "books": 2,
            "cash_total": 25.0,
            "unavailable": 0,
            "plus": 0,
            "credits": 1,
            "cash_part": 5.0,
            "best_total": 16.25,
            "saved": 8.75,
            "credit_price": 11.25,
        }


class TestSumFinishCosts:
    def test_sums_without_credit_price(self):
        total = sum_finish_costs(
            [
                finish_cost(_gaps(5.0, 6.0, None), None),
                finish_cost(
                    [make_product(asin="P", price=7.5, in_plus_catalog=True)], None
                ),
            ]
        )

        assert total == FinishCost(
            priced=3, unavailable_count=1, plus_count=1, cash_total=18.5
        )

    def test_sums_with_credit_price(self):
        first = finish_cost(_gaps(4.99, 20.0, 24.99), 11.25)
        second = finish_cost(_gaps(3.0, 15.0, None), 11.25)

        total = sum_finish_costs([first, second])

        assert total.priced == 5
        assert total.unavailable_count == 1
        assert total.cash_total == 67.98
        assert total.credits_needed == 3
        assert total.cash_part == 7.99
        assert total.best_total == 41.74
        assert total.saved == 26.24
        assert total.credit_price == 11.25

    def test_sum_of_rounded_series_stays_rounded(self):
        parts = [finish_cost(_gaps(0.1), 11.25) for _ in range(3)]

        total = sum_finish_costs(parts)

        assert total.cash_total == 0.3
        assert total.best_total == 0.3
        assert total.saved == 0.0

    def test_empty_sum_is_zero(self):
        total = sum_finish_costs([])

        assert total == FinishCost(
            priced=0, unavailable_count=0, plus_count=0, cash_total=0.0
        )

    def test_single_entry_is_unchanged(self):
        cost = finish_cost(_gaps(4.99, 20.0), 11.25)

        assert sum_finish_costs([cost]) == cost
