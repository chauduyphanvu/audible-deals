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


def whispersync_margin(p: Product, credit_price: float | None) -> float | None:
    """Cheapest Audible cost minus the Whispersync narration add-on.

    Positive means Kindle book + narration can beat Audible: by this much when
    the Kindle book is already owned, otherwise when the Kindle book costs less
    than this.
    """
    if p.whispersync_price is None:
        return None
    audible = effective_price(p, credit_price)
    if audible is None:
        return None
    return audible - p.whispersync_price


def whispersync_wins(p: Product, credit_price: float | None) -> bool:
    """Whether the Whispersync route is the cheapest way to get the audiobook.

    Without the Kindle book it must leave room for a Kindle price; with it, the
    add-on only has to match the cheapest Audible route.
    """
    margin = whispersync_margin(p, credit_price)
    if margin is None:
        return False
    return margin >= 0 if p.owns_kindle else margin > 0
