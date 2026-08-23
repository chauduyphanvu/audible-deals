"""Product layouts and terminal views."""

from __future__ import annotations

from collections.abc import Mapping, Set
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from rich.panel import Panel
from rich.table import Table

from audible_deals.metrics import buy_verdict, price_per_hour
from audible_deals.presentation import terminal
from audible_deals.presentation.terminal import safe_markup
from audible_deals.presentation.common import (
    _VERDICT_MARKUP,
    _buy_cell,
    _discount_color,
    _pph_str,
    discount_str,
    price_str,
    rating_str,
)
from audible_deals.product import Product


@dataclass(frozen=True)
class ProductDisplayOptions:
    max_price: float | None = None
    title: str = "Results"
    currency: str = "$"
    show_url: bool = False
    atl_asins: Set[str] | None = None
    hist_context: Mapping[str, int] | None = None
    credit_price: float | None = None
    match_context: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "atl_asins",
            None if self.atl_asins is None else frozenset(self.atl_asins),
        )
        object.__setattr__(
            self,
            "hist_context",
            None
            if self.hist_context is None
            else MappingProxyType(dict(self.hist_context)),
        )
        object.__setattr__(
            self,
            "match_context",
            None
            if self.match_context is None
            else MappingProxyType(dict(self.match_context)),
        )


@dataclass(frozen=True)
class ProductLayout:
    mode: Literal["cards", "compact", "wide"]
    terminal_width: int
    ref_width: int
    title_width: int
    price_width: int
    hours_width: int
    pph_width: int
    rating_width: int
    history_width: int
    match_width: int
    url_width: int
    show_buy_column: bool
    show_history_column: bool
    show_match_column: bool
    show_url_column: bool

    @property
    def wide(self) -> bool:
        return self.mode == "wide"


def calculate_product_layout(
    products: list[Product],
    options: ProductDisplayOptions,
    terminal_width: int,
) -> ProductLayout:
    """Calculate product presentation without reading terminal state."""
    if not products:
        return ProductLayout(
            mode="cards"
            if terminal_width < 80
            else ("compact" if terminal_width < 120 else "wide"),
            terminal_width=terminal_width,
            ref_width=4,
            title_width=0,
            price_width=10,
            hours_width=3,
            pph_width=7,
            rating_width=10,
            history_width=7,
            match_width=0,
            url_width=0,
            show_buy_column=False,
            show_history_column=False,
            show_match_column=False,
            show_url_column=False,
        )

    url_width = max(len(product.url) for product in products)
    show_history = options.hist_context is not None and any(
        product.asin in options.hist_context for product in products
    )
    price_width = max(
        10,
        max(
            len(price_str(product.price, product.currency))
            + (
                len(price_str(product.list_price, product.currency)) + 6
                if product.discount_pct and product.list_price
                else 0
            )
            for product in products
        ),
    )
    rating_width = max(
        10,
        max(
            len(rating_str(product.rating, product.num_ratings)) for product in products
        ),
    )
    ref_width = max(4, len(f"@{len(products)}"))
    hours_width = max(
        3,
        max(len(str(product.hours)) if product.hours else 1 for product in products),
    )
    pph_width = max(
        7, max(len(_pph_str(product, product.currency)) for product in products)
    )
    history_width = max(
        7,
        max(
            (
                len(f"{options.hist_context.get(product.asin):+d}%")
                if options.hist_context
                and options.hist_context.get(product.asin) is not None
                else 1
            )
            for product in products
        ),
    )

    if terminal_width < 80:
        return ProductLayout(
            mode="cards",
            terminal_width=terminal_width,
            ref_width=ref_width,
            title_width=0,
            price_width=price_width,
            hours_width=hours_width,
            pph_width=pph_width,
            rating_width=rating_width,
            history_width=history_width,
            match_width=0,
            url_width=url_width,
            show_buy_column=False,
            show_history_column=False,
            show_match_column=False,
            show_url_column=False,
        )

    if terminal_width < 120:
        return ProductLayout(
            mode="compact",
            terminal_width=terminal_width,
            ref_width=ref_width,
            title_width=max(24, terminal_width - price_width - rating_width - 17),
            price_width=price_width,
            hours_width=hours_width,
            pph_width=pph_width,
            rating_width=rating_width,
            history_width=history_width,
            match_width=0,
            url_width=url_width,
            show_buy_column=False,
            show_history_column=False,
            show_match_column=False,
            show_url_column=False,
        )

    base_width = (
        ref_width + 24 + price_width + hours_width + pph_width + rating_width + 19
    )
    remaining = terminal_width - base_width
    show_buy_column = False
    show_history_column = False
    show_match_column = False
    show_url_column = False
    match_width = 0
    if options.credit_price is not None and remaining >= 10:
        show_buy_column = True
        remaining -= 10
    if show_history and remaining >= history_width + 3:
        show_history_column = True
        remaining -= history_width + 3
    if options.match_context is not None:
        desired_match = min(
            24,
            max(
                12,
                max(
                    (len(reason) for reason in options.match_context.values()),
                    default=12,
                ),
            ),
        )
        if remaining >= desired_match + 3:
            show_match_column = True
            match_width = desired_match
            remaining -= desired_match + 3
    if options.show_url and remaining >= url_width + 3:
        show_url_column = True
        remaining -= url_width + 3

    return ProductLayout(
        mode="wide",
        terminal_width=terminal_width,
        ref_width=ref_width,
        title_width=max(24, min(80, 24 + max(0, remaining))),
        price_width=price_width,
        hours_width=hours_width,
        pph_width=pph_width,
        rating_width=rating_width,
        history_width=history_width,
        match_width=match_width,
        url_width=url_width,
        show_buy_column=show_buy_column,
        show_history_column=show_history_column,
        show_match_column=show_match_column,
        show_url_column=show_url_column,
    )


def _price_cell(
    product: Product,
    currency: str,
    max_price: float | None,
    atl_asins: Set[str] | None,
) -> str:
    rendered = price_str(product.price, currency)
    if atl_asins and product.asin in atl_asins:
        rendered = "[bold gold1]★[/bold gold1] " + rendered
    if product.price is not None and max_price is not None:
        if product.price <= max_price * 0.6:
            rendered = f"[bold green]{rendered}[/bold green]"
        elif product.price <= max_price:
            rendered = f"[green]{rendered}[/green]"
        else:
            rendered = f"[red]{rendered}[/red]"
    discount = product.discount_pct
    if discount and discount > 0 and product.list_price:
        color = _discount_color(discount)
        rendered += (
            f" [dim]{currency}{product.list_price:.0f}[/dim] "
            f"[{color}]-{discount}%[/{color}]"
        )
    return rendered


def _product_identity(product: Product) -> str:
    identity = safe_markup(product.asin)
    if product.series_name:
        identity += f" · {safe_markup(product.series_name)}"
        if product.series_position:
            identity += f" #{safe_markup(product.series_position)}"
    return identity


def _hist_cell(pct: int | None) -> str:
    if pct is None:
        return "[dim]-[/dim]"
    if pct < 0:
        return f"[green]{pct}%[/green]"
    if pct == 0:
        return "[dim]0%[/dim]"
    return f"[dim]+{pct}%[/dim]"


def _inline_signals(
    product: Product,
    *,
    currency: str,
    credit_price: float | None,
    hist_context: Mapping[str, int] | None,
    match_context: Mapping[str, str] | None,
    include_length: bool,
    include_buy: bool,
    include_history: bool,
    include_match: bool,
) -> list[str]:
    lines: list[str] = []
    if include_length:
        lines.append(
            f"[dim]Length[/dim] {product.hours if product.hours else '-'}h · "
            f"[dim]Value[/dim] {_pph_str(product, currency)}/hr"
        )
    lines.append(f"[dim]ID[/dim] [cyan]{_product_identity(product)}[/cyan]")
    signals: list[str] = []
    if include_buy and credit_price is not None:
        signals.append(f"[dim]Buy[/dim] {_buy_cell(product, credit_price)}")
    if include_history and hist_context is not None:
        signals.append(
            f"[dim]vs hist[/dim] {_hist_cell(hist_context.get(product.asin))}"
        )
    if include_match and match_context is not None and match_context.get(product.asin):
        signals.append(
            f"[dim]Match[/dim] [cyan]{safe_markup(match_context[product.asin])}[/cyan]"
        )
    if signals:
        lines.append(" · ".join(signals))
    return lines


def _display_product_cards(
    products: list[Product], options: ProductDisplayOptions
) -> None:
    terminal.console.print(f"[bold]{safe_markup(options.title)}[/bold]")
    for index, product in enumerate(products, 1):
        plus = " [magenta][+][/magenta]" if product.in_plus_catalog else ""
        terminal.console.print(
            f"\n[bold cyan]@{index}[/bold cyan]  "
            f"[bold]{safe_markup(product.full_title)}[/bold]{plus}"
        )
        if product.authors_str:
            terminal.console.print(f"[dim]By[/dim] {safe_markup(product.authors_str)}")
        terminal.console.print(
            f"[dim]Price[/dim] "
            f"{_price_cell(product, product.currency, options.max_price, options.atl_asins)}"
        )
        terminal.console.print(
            f"[dim]Length[/dim] {product.hours if product.hours else '-'}h · "
            f"[dim]Value[/dim] {_pph_str(product, product.currency)}/hr"
        )
        terminal.console.print(
            f"[dim]Rating[/dim] {rating_str(product.rating, product.num_ratings)}"
        )
        for line in _inline_signals(
            product,
            currency=product.currency,
            credit_price=options.credit_price,
            hist_context=options.hist_context,
            match_context=options.match_context,
            include_length=False,
            include_buy=True,
            include_history=True,
            include_match=True,
        ):
            terminal.console.print(line)


def _render_product_row(
    product: Product,
    index: int,
    options: ProductDisplayOptions,
    layout: ProductLayout,
) -> list[str]:
    currency = product.currency
    plus = " [magenta][+][/magenta]" if product.in_plus_catalog else ""
    title_cell = f"{safe_markup(product.title)}{plus}"
    if product.authors_str:
        title_cell += f"\n[dim]{safe_markup(product.authors_str)}[/dim]"
    if layout.mode == "compact":
        title_cell += "\n" + "\n".join(
            _inline_signals(
                product,
                currency=currency,
                credit_price=options.credit_price,
                hist_context=options.hist_context,
                match_context=options.match_context,
                include_length=True,
                include_buy=True,
                include_history=True,
                include_match=True,
            )
        )
    else:
        title_cell += f"\n[dim cyan]{_product_identity(product)}[/dim cyan]"
        folded = _inline_signals(
            product,
            currency=currency,
            credit_price=options.credit_price,
            hist_context=options.hist_context,
            match_context=options.match_context,
            include_length=False,
            include_buy=not layout.show_buy_column,
            include_history=not layout.show_history_column,
            include_match=not layout.show_match_column,
        )[1:]
        if folded:
            title_cell += "\n" + "\n".join(folded)

    row = [
        f"@{index}",
        title_cell,
        _price_cell(product, currency, options.max_price, options.atl_asins),
    ]
    if layout.wide:
        row.extend(
            [
                str(product.hours) if product.hours else "-",
                _pph_str(product, currency),
            ]
        )
    row.append(rating_str(product.rating, product.num_ratings))
    if layout.show_buy_column:
        row.append(_buy_cell(product, options.credit_price))
    if layout.show_history_column:
        pct = options.hist_context.get(product.asin) if options.hist_context else None
        row.append(_hist_cell(pct))
    if layout.show_match_column:
        row.append(safe_markup(options.match_context.get(product.asin, "")))
    if layout.show_url_column:
        row.append(safe_markup(product.url))
    return row


def _build_product_table(
    products: list[Product],
    options: ProductDisplayOptions,
    layout: ProductLayout,
) -> Table:
    table = Table(
        title=safe_markup(options.title),
        show_lines=False,
        padding=(0, 1),
        title_style="bold",
        expand=False,
    )
    table.add_column(
        "Ref", style="dim cyan", width=layout.ref_width, justify="right", no_wrap=True
    )
    table.add_column(
        "Title / Author", max_width=layout.title_width, overflow="ellipsis"
    )
    table.add_column("Price", justify="right", width=layout.price_width, no_wrap=True)
    if layout.wide:
        table.add_column("Hrs", justify="right", width=layout.hours_width, no_wrap=True)
        table.add_column(
            f"{options.currency}/hr",
            justify="right",
            width=layout.pph_width,
            no_wrap=True,
        )
    table.add_column("Rating", justify="right", width=layout.rating_width, no_wrap=True)
    if layout.show_buy_column:
        table.add_column("Buy", width=7)
    if layout.show_history_column:
        table.add_column(
            "vs hist", justify="right", width=layout.history_width, no_wrap=True
        )
    if layout.show_match_column:
        table.add_column("Match", width=layout.match_width, style="cyan")
    if layout.show_url_column:
        table.add_column(
            "URL",
            width=layout.url_width,
            min_width=layout.url_width,
            max_width=layout.url_width,
            no_wrap=True,
            overflow="ignore",
            style="dim cyan",
        )
    for index, product in enumerate(products, 1):
        table.add_row(*_render_product_row(product, index, options, layout))
    return table


def _display_url_fallback(products: list[Product]) -> None:
    terminal.console.print("\n[bold]URLs[/bold]")
    for index, product in enumerate(products, 1):
        terminal.console.print(
            f"  @{index}. {safe_markup(product.url)}",
            soft_wrap=True,
            overflow="fold",
        )


def display_products(
    products: list[Product],
    *,
    max_price: float | None = None,
    title: str = "Results",
    currency: str = "$",
    show_url: bool = False,
    atl_asins: Set[str] | None = None,
    hist_context: Mapping[str, int] | None = None,
    credit_price: float | None = None,
    match_context: Mapping[str, str] | None = None,
) -> None:
    if not products:
        terminal.console.print("[dim]No products found.[/dim]")
        return

    options = ProductDisplayOptions(
        max_price=max_price,
        title=title,
        currency=currency,
        show_url=show_url,
        atl_asins=atl_asins,
        hist_context=hist_context,
        credit_price=credit_price,
        match_context=match_context,
    )
    layout = calculate_product_layout(products, options, terminal.console.width or 80)
    if layout.mode == "cards":
        _display_product_cards(products, options)
        if show_url:
            _display_url_fallback(products)
        return

    terminal.console.print(_build_product_table(products, options, layout))
    if show_url and not layout.show_url_column:
        _display_url_fallback(products)


def display_product_detail(product: Product, credit_price: float | None = None) -> None:
    lines: list[str] = [f"[bold]{safe_markup(product.full_title)}[/bold]", ""]
    if product.authors:
        lines.append(
            f"  [dim]By:[/dim]        {safe_markup(', '.join(product.authors))}"
        )
    if product.narrators:
        lines.append(
            f"  [dim]Narrated:[/dim]   {safe_markup(', '.join(product.narrators))}"
        )
    if product.publisher:
        lines.append(f"  [dim]Publisher:[/dim]  {safe_markup(product.publisher)}")
    lines.append("")

    currency = product.currency
    price_line = f"  [dim]Price:[/dim]      {price_str(product.price, currency)}"
    if product.list_price and product.price != product.list_price:
        price_line += f"  [dim](was {price_str(product.list_price, currency)})[/dim]"
    if product.discount_pct and product.discount_pct > 0:
        price_line += f"  [bold yellow]-{product.discount_pct}% off[/bold yellow]"
    lines.append(price_line)

    if credit_price is not None and (verdict := buy_verdict(product, credit_price)):
        buy_line = f"  [dim]Buy with:[/dim]   {_VERDICT_MARKUP[verdict]}"
        if verdict == "cash":
            buy_line += f"  [dim](cheaper than a {price_str(credit_price, currency)} credit)[/dim]"
        elif verdict == "credit":
            buy_line += f"  [dim](a {price_str(credit_price, currency)} credit beats the cash price)[/dim]"
        else:
            buy_line += "  [dim](free with membership)[/dim]"
        lines.append(buy_line)

    lines.append(
        f"  [dim]Rating:[/dim]     {rating_str(product.rating, product.num_ratings)}"
    )
    lines.append(
        f"  [dim]Length:[/dim]     {product.hours} hours ({product.length_minutes} min)"
    )
    if product.series_name:
        series = product.series_name
        if product.series_position:
            series += f", Book {product.series_position}"
        lines.append(f"  [dim]Series:[/dim]     {safe_markup(series)}")
    if product.categories:
        lines.append(
            f"  [dim]Genres:[/dim]     {safe_markup(' > '.join(product.categories))}"
        )
    if product.language:
        lines.append(f"  [dim]Language:[/dim]   {safe_markup(product.language)}")
    if product.release_date:
        lines.append(f"  [dim]Released:[/dim]   {safe_markup(product.release_date)}")
    if product.in_plus_catalog:
        lines.append("  [magenta]Included in Audible Plus[/magenta]")
    lines.extend(["", f"  [dim]{safe_markup(product.url)}[/dim]"])

    terminal.console.print(
        Panel(
            "\n".join(lines),
            title=f"[cyan]{safe_markup(product.asin)}[/cyan]",
            border_style="dim",
            padding=(1, 2),
        )
    )


def display_comparison(
    products: list[Product], credit_price: float | None = None
) -> None:
    currency = products[0].currency if products else "$"
    table = Table(
        title="Comparison",
        show_lines=True,
        padding=(0, 1),
        title_style="bold",
        expand=False,
    )
    table.add_column("Field", style="dim", width=12)
    for product in products:
        table.add_column(safe_markup(product.asin), style="cyan", max_width=30)

    rows = [
        ("Title", [safe_markup(product.title) for product in products]),
        ("Author", [safe_markup(product.authors_str) for product in products]),
        ("Narrator", [safe_markup(product.narrators_str) for product in products]),
        ("Price", [price_str(product.price, product.currency) for product in products]),
        (
            "List Price",
            [price_str(product.list_price, product.currency) for product in products],
        ),
        (
            "Discount",
            [discount_str(product.discount_pct) or "-" for product in products],
        ),
        (
            "Hours",
            [str(product.hours) if product.hours else "-" for product in products],
        ),
        (
            f"{currency}/hr",
            [_pph_str(product, product.currency) for product in products],
        ),
        (
            "Rating",
            [rating_str(product.rating, product.num_ratings) for product in products],
        ),
        (
            "Series",
            [
                safe_markup(f"{product.series_name} #{product.series_position}")
                if product.series_name
                else "-"
                for product in products
            ],
        ),
        ("Language", [safe_markup(product.language or "-") for product in products]),
        (
            "Released",
            [safe_markup(product.release_date or "-") for product in products],
        ),
        ("Plus", ["Yes" if product.in_plus_catalog else "-" for product in products]),
    ]
    if credit_price is not None:
        rows.insert(
            6, ("Buy", [_buy_cell(product, credit_price) for product in products])
        )
    for label, values in rows:
        table.add_row(label, *values)
    terminal.console.print(table)

    priced = [
        product
        for product in products
        if product.price is not None and product.hours > 0
    ]
    if priced:
        best = min(priced, key=price_per_hour)
        terminal.console.print(
            f"\n  Best value: [bold green]{safe_markup(best.title)}[/bold green] "
            f"at {_pph_str(best, best.currency)}/hr"
        )
