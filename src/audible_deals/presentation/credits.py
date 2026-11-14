"""Terminal view for the credit planner."""

from __future__ import annotations

from rich.table import Table

from audible_deals.credit_plan import CreditPlan
from audible_deals.presentation import terminal
from audible_deals.presentation.common import price_str
from audible_deals.presentation.terminal import safe_markup
from audible_deals.product import Product

_MAX_WAIT_ROWS = 10


def _noun(count: int, singular: str) -> str:
    return f"{count} {singular}" if count == 1 else f"{count} {singular}s"


def _label(product: Product) -> str:
    title = safe_markup(product.title)
    if product.authors_str:
        return f"{title} · {safe_markup(product.authors_str)}"
    return title


def _price(product: Product, currency: str) -> str:
    return price_str(product.price, product.currency or currency)


def display_credit_plan(plan: CreditPlan, currency: str = "$") -> None:
    header = f"Credit plan · {_noun(plan.credits, 'credit')}"
    if plan.credit_price is not None:
        header += f" · credit price {price_str(plan.credit_price, currency)}"
    terminal.console.print(f"[bold]{header}[/bold]\n")

    table = Table(box=None, show_header=False, padding=(0, 1))
    table.add_column(no_wrap=True)
    table.add_column(overflow="ellipsis")
    table.add_column(justify="right", no_wrap=True)
    table.add_column(no_wrap=True)
    for product in plan.credit:
        table.add_row(
            "[bold yellow]CREDIT[/bold yellow]",
            _label(product),
            _price(product, currency),
            "",
        )
    for product in plan.cash:
        table.add_row(
            "[bold green]CASH[/bold green]",
            _label(product),
            _price(product, currency),
            "[dim]cheaper than a credit[/dim]",
        )
    for product in plan.plus:
        table.add_row(
            "[bold magenta]PLUS[/bold magenta]",
            _label(product),
            "",
            "[dim]included with Plus[/dim]",
        )
    if len(plan.wait) <= _MAX_WAIT_ROWS:
        for product in plan.wait:
            table.add_row(
                "WAIT", _label(product), _price(product, currency), "", style="dim"
            )
    else:
        highest = _price(plan.wait[0], currency)
        lowest = _price(plan.wait[-1], currency)
        table.add_row(
            "WAIT",
            f"{len(plan.wait)} more, from {highest} down to {lowest}",
            "",
            "",
            style="dim",
        )
    if table.row_count:
        terminal.console.print(table)
        terminal.console.print()

    if plan.credit:
        spent = len(plan.credit)
        detail = f"{price_str(plan.value_per_credit, currency)} per credit"
        if plan.saved is not None:
            cost = "it costs" if spent == 1 else "they cost"
            detail += f", {price_str(plan.saved, currency)} more than {cost} you"
        terminal.console.print(
            f"{_noun(spent, 'credit')} {'buys' if spent == 1 else 'buy'} "
            f"{price_str(plan.credit_value, currency)} of books ({detail})."
        )
    if plan.cash:
        terminal.console.print(
            f"Cash today: {price_str(plan.cash_total, currency)} "
            f"for {_noun(len(plan.cash), 'book')}."
        )
    if plan.credits_unused:
        reason = (
            "no other priced books on your wishlist"
            if plan.credit_price is None
            else "nothing else on your wishlist costs more than a credit"
        )
        terminal.console.print(
            f"{_noun(plan.credits_unused, 'credit')} left over: {reason}."
        )
    if plan.unavailable:
        terminal.console.print(
            f"[dim]{_noun(plan.unavailable, 'book')} skipped: no price listed.[/dim]"
        )
    if plan.credit_price is None:
        terminal.console.print(
            "[dim]Set what you pay per credit with: "
            "deals config set credit-price 11.25[/dim]"
        )
