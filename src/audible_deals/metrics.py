"""Pure per-product value metrics shared by filtering, display, and export."""

from __future__ import annotations

from audible_deals.product import Product


def price_per_hour(p: Product) -> float:
    """Calculate price per hour of audio. Returns inf for missing data."""
    if p.price is None or p.hours <= 0:
        return float("inf")
    return p.price / p.hours


def value_score(p: Product) -> float:
    """Composite value score: (rating * hours) / price. Higher is better."""
    if p.price is None or p.hours <= 0 or p.rating <= 0:
        return 0.0
    if p.price <= 0:
        return float("inf")
    return (p.rating * p.hours) / p.price


def effective_price(p: Product, credit_price: float | None) -> float | None:
    """Real cost to acquire: the cheaper of cash price and one credit."""
    if p.price is None:
        return None
    if credit_price is None:
        return p.price
    return min(p.price, credit_price)


def buy_verdict(p: Product, credit_price: float) -> str | None:
    """How to acquire: 'plus' (free with membership), 'cash', or 'credit'."""
    if p.in_plus_catalog:
        return "plus"
    if p.price is None:
        return None
    return "cash" if p.price < credit_price else "credit"
