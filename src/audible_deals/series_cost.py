"""Cost of buying every missing book in a series."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

from audible_deals.product import Product


@dataclass(frozen=True)
class FinishCost:
    """What it takes to buy the given gaps; money is rounded to cents."""

    priced: int
    unavailable_count: int
    plus_count: int
    cash_total: float
    credit_price: float | None = None
    credits_needed: int | None = None
    cash_part: float | None = None
    best_total: float | None = None
    saved: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "books": self.priced,
            "cash_total": self.cash_total,
            "unavailable": self.unavailable_count,
            "plus": self.plus_count,
        }
        if self.credit_price is not None:
            data.update(
                credits=self.credits_needed,
                cash_part=self.cash_part,
                best_total=self.best_total,
                saved=self.saved,
                credit_price=round(self.credit_price, 2),
            )
        return data


def _is_priced(product: Product) -> bool:
    price = product.price
    return (
        isinstance(price, (int, float))
        and not isinstance(price, bool)
        and math.isfinite(price)
    )


def finish_cost(
    gap_products: Iterable[Product], credit_price: float | None
) -> FinishCost:
    """Cost of every priced gap, each bought the cheaper way when a credit price is set."""
    prices: list[float] = []
    unavailable = 0
    plus = 0
    for product in gap_products:
        if not _is_priced(product):
            unavailable += 1
            continue
        prices.append(float(product.price))
        plus += bool(product.in_plus_catalog)

    cash_total = round(sum(prices, 0.0), 2)
    if credit_price is None:
        return FinishCost(len(prices), unavailable, plus, cash_total)

    # A price equal to the credit price goes on credit, as in metrics.buy_verdict.
    cash_prices = [price for price in prices if price < credit_price]
    credits_needed = len(prices) - len(cash_prices)
    cash_part = round(sum(cash_prices, 0.0), 2)
    best_total = round(credits_needed * credit_price + sum(cash_prices, 0.0), 2)
    return FinishCost(
        len(prices),
        unavailable,
        plus,
        cash_total,
        credit_price=credit_price,
        credits_needed=credits_needed,
        cash_part=cash_part,
        best_total=best_total,
        saved=round(cash_total - best_total, 2),
    )


def sum_finish_costs(costs: Iterable[FinishCost]) -> FinishCost:
    """Grand total across series; the credit figures follow the first entry's credit price."""
    costs = list(costs)
    credit_price = costs[0].credit_price if costs else None
    totals = FinishCost(
        priced=sum(c.priced for c in costs),
        unavailable_count=sum(c.unavailable_count for c in costs),
        plus_count=sum(c.plus_count for c in costs),
        cash_total=round(sum((c.cash_total for c in costs), 0.0), 2),
    )
    if credit_price is None:
        return totals
    best_total = round(sum((c.best_total or 0.0 for c in costs), 0.0), 2)
    return replace(
        totals,
        credit_price=credit_price,
        credits_needed=sum(c.credits_needed or 0 for c in costs),
        cash_part=round(sum((c.cash_part or 0.0 for c in costs), 0.0), 2),
        best_total=best_total,
        saved=round(totals.cash_total - best_total, 2),
    )
