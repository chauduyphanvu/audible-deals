"""Pure per-product value metrics shared by filtering, display, and export."""

from __future__ import annotations

from audible_deals.client import Product


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
