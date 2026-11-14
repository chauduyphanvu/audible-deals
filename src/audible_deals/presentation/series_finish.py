"""Rich markup lines for the cost of finishing a series."""

from __future__ import annotations

from collections.abc import Sequence

from audible_deals.presentation.common import price_str
from audible_deals.series_cost import FinishCost, sum_finish_costs


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _credit_mix(cost: FinishCost, currency: str) -> str:
    credits = _count(cost.credits_needed, "credit")
    if not cost.cash_part:
        return credits
    return f"{credits} + {price_str(cost.cash_part, currency)} cash"


def finish_lines(cost: FinishCost, currency: str) -> list[str]:
    """Footer for one series: what finishing costs, and the cheapest way to pay."""
    first = (
        f"  [dim]Finish:[/dim] {_count(cost.priced, 'book')} · "
        f"{price_str(cost.cash_total, currency)} cash"
    )
    notes = []
    if cost.unavailable_count:
        notes.append(f"{cost.unavailable_count} unavailable not included")
    if cost.plus_count:
        notes.append(f"{cost.plus_count} included with Plus")
    if notes:
        first += f"[dim] · {' · '.join(notes)}[/dim]"
    lines = [first]

    if cost.credit_price is None:
        return lines
    credit = price_str(cost.credit_price, currency)
    if cost.credits_needed == 0:
        subject = "every book is" if cost.priced > 1 else "the book is"
        lines.append(
            f"  [dim]Cheapest:[/dim] all cash "
            f"({price_str(cost.cash_total, currency)}); {subject} under your "
            f"{credit} credit"
        )
    else:
        line = (
            f"  [dim]Cheapest:[/dim] {_credit_mix(cost, currency)} "
            f"≈ {price_str(cost.best_total, currency)}"
            f"[dim] at {credit} per credit[/dim]"
        )
        if cost.saved > 0:
            line += f" [green](saves {price_str(cost.saved, currency)})[/green]"
        lines.append(line)
    return lines


def finish_total_line(costs: Sequence[FinishCost], currency: str) -> str:
    """One line summing every series that has a footer."""
    total = sum_finish_costs(costs)
    line = (
        f"[bold]All {len(costs)} series:[/bold] {_count(total.priced, 'book')} · "
        f"{price_str(total.cash_total, currency)} cash"
    )
    if total.credit_price is None:
        return line
    if total.credits_needed == 0:
        cheapest = "all cash"
    else:
        cheapest = (
            f"{_credit_mix(total, currency)} ≈ {price_str(total.best_total, currency)}"
        )
    return f"{line}[dim] · cheapest {cheapest}[/dim]"
