"""Split a wishlist between credits, cash, Audible Plus, and waiting."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from audible_deals.product import Product


@dataclass(frozen=True)
class CreditPlan:
    credits: int
    credit_price: float | None
    credit: tuple[Product, ...]
    cash: tuple[Product, ...]
    plus: tuple[Product, ...]
    wait: tuple[Product, ...]
    unavailable: int
    cash_total: float
    credit_value: float
    value_per_credit: float | None
    credits_unused: int
    saved: float | None


def plan_credits(
    products: Iterable[Product], credits: int, credit_price: float | None
) -> CreditPlan:
    """Choose which books to spend credits on.

    A book costs one credit or its cash price, so spending credits on the most
    expensive books that are not cheaper than a credit is optimal. Books priced
    below the credit price are better bought with cash.
    """
    books = list(products)
    priced = [p for p in books if p.price is not None]
    unavailable = len(books) - len(priced)

    plus = sorted(
        (p for p in priced if p.in_plus_catalog), key=lambda p: (p.title, p.asin)
    )
    candidates = [p for p in priced if not p.in_plus_catalog]

    if credit_price is None:
        cash: list[Product] = []
        pool = candidates
    else:
        cash = sorted(
            (p for p in candidates if p.price < credit_price),
            key=lambda p: (p.price, p.title, p.asin),
        )
        pool = [p for p in candidates if p.price >= credit_price]
    pool.sort(key=lambda p: (-p.price, p.title, p.asin))
    credit, wait = pool[:credits], pool[credits:]

    credit_value = round(sum(p.price for p in credit), 2)
    return CreditPlan(
        credits=credits,
        credit_price=credit_price,
        credit=tuple(credit),
        cash=tuple(cash),
        plus=tuple(plus),
        wait=tuple(wait),
        unavailable=unavailable,
        cash_total=round(sum(p.price for p in cash), 2),
        credit_value=credit_value,
        value_per_credit=round(credit_value / len(credit), 2) if credit else None,
        credits_unused=credits - len(credit),
        saved=(
            round(credit_value - len(credit) * credit_price, 2)
            if credit_price is not None
            else None
        ),
    )
