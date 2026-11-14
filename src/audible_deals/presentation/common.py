"""Shared presentation formatting."""

from __future__ import annotations

import datetime

from audible_deals.deal_grade import MIN_CHECKS, MIN_SPAN_DAYS, DealGrade
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


_GRADE_STYLE = {
    "lowest": "bold green",
    "good": "green",
    "typical": "dim",
    "high": "yellow",
}
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def grade_cell(grade: DealGrade | None) -> str:
    """Table word for a grade; no verdict renders as a dim dash."""
    if grade is None or grade.grade == "new":
        return "[dim]-[/dim]"
    style = _GRADE_STYLE[grade.grade]
    return f"[{style}]{grade.grade}[/{style}]"


def _short_date(day: datetime.date, reference: datetime.date) -> str:
    text = f"{day.day} {_MONTHS[day.month - 1]}"
    return text if day.year == reference.year else f"{text} {day.year}"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _times(count: int) -> str:
    return {1: "once", 2: "twice"}.get(count, f"{count} times")


def _ordinal(number: int) -> str:
    words = {2: "second", 3: "third", 4: "fourth", 5: "fifth"}
    if number in words:
        return words[number]
    suffix = (
        "th"
        if 10 <= number % 100 <= 20
        else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    )
    return f"{number}{suffix}"


def grade_verdict(grade: DealGrade, currency: str = "$") -> list[str]:
    """A headline naming the grade and its evidence, then one line of detail.

    The headline stands alone where a view has room for a single line.
    """
    observed_on = grade.observed_on
    checks = _plural(grade.checks, "earlier check")
    if grade.grade == "new":
        if grade.first_date is None:
            evidence = "no earlier checks"
        else:
            days = (observed_on - grade.first_date).days
            evidence = f"{checks} over {_plural(days, 'day')}"
        return [
            f"[dim]NEW[/dim]: not enough history yet ({evidence})",
            f"[dim]A verdict needs {MIN_CHECKS} checks over {MIN_SPAN_DAYS} days; "
            "deals track install builds them.[/dim]",
        ]

    basis = f"in {checks} since {_short_date(grade.first_date, observed_on)}"
    usual = price_str(grade.typical_price, currency)
    lowest = price_str(grade.lowest_price, currency)
    lowest_on = _short_date(grade.lowest_date, observed_on)
    drops = (
        f"it has dropped to {price_str(grade.good_price, currency)} or less "
        f"{_times(grade.good_times)}"
        if grade.good_price is not None
        else ""
    )
    if grade.grade == "lowest" and grade.price < grade.lowest_price:
        headline = f"below every price {basis}"
        detail = f"Previous low {lowest} on {lowest_on} · usual price {usual}"
    elif grade.grade == "lowest":
        headline = f"ties the lowest price {basis}"
        detail = (
            f"This low {_times(grade.times_at_or_below)} before, last on "
            f"{lowest_on} · usual price {usual}"
        )
    elif grade.grade == "good":
        headline = f"{_ordinal(grade.rank)}-lowest price {basis}"
        detail = (
            f"Lowest {lowest} on {lowest_on} · usual price {usual} · at or below "
            f"{price_str(grade.price, currency)} "
            f"{_times(grade.times_at_or_below)} before"
        )
    elif grade.grade == "typical":
        headline = f"usually {usual} {basis}"
        detail = f"Lowest {lowest} on {lowest_on}" + (f" · {drops}" if drops else "")
    else:
        headline = f"above the usual {usual} {basis}"
        detail = f"It was {lowest} on {lowest_on}" + (f"; {drops}." if drops else ".")
    style = "bold" if grade.grade == "typical" else _GRADE_STYLE[grade.grade]
    return [f"[{style}]{grade.grade.upper()}[/{style}]: {headline}", detail]
