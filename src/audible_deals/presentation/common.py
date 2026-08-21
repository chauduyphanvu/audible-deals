"""Shared presentation formatting."""

from __future__ import annotations

from audible_deals.metrics import buy_verdict, price_per_hour
from audible_deals.product import Product


def price_str(price: int | float | None, currency: str = "$") -> str:
    if price is None:
        return "-"
    if isinstance(price, int) and not isinstance(price, bool):
        return f"{currency}{price}.00"
    return f"{currency}{price:.2f}"


def rating_str(rating: float, num_ratings: int = 0) -> str:
    if rating == 0:
        return "-"
    suffix = f" ({num_ratings:,})" if num_ratings else ""
    return f"{rating:.1f}{suffix}"


def discount_str(pct: int | None) -> str:
    if pct is None or pct <= 0:
        return ""
    return f"-{pct}%"


def _discount_color(pct: int) -> str:
    if pct >= 80:
        return "bold green"
    if pct >= 50:
        return "yellow"
    return "dim"


_VERDICT_MARKUP = {
    "cash": "[green]cash[/green]",
    "credit": "[yellow]credit[/yellow]",
    "plus": "[magenta]plus[/magenta]",
}


def _buy_cell(product: Product, credit_price: float) -> str:
    verdict = buy_verdict(product, credit_price)
    if verdict is None:
        return "[dim]-[/dim]"
    return _VERDICT_MARKUP[verdict]


def _pph_str(product: Product, currency: str = "$") -> str:
    pph = price_per_hour(product)
    if pph == float("inf"):
        return "-"
    return f"{currency}{pph:.2f}"
