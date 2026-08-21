"""Rich terminal display for Audible deal finder.

Formats products, categories, and detail views for the terminal using
rich tables and panels.
"""

from __future__ import annotations

import collections
import datetime

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
)
from rich.table import Table

from audible_deals.product import Product
from audible_deals.metrics import buy_verdict, price_per_hour

console = Console()


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
    """Return Rich color markup based on discount tier."""
    if pct >= 80:
        return "bold green"
    elif pct >= 50:
        return "yellow"
    return "dim"


_VERDICT_MARKUP = {
    "cash": "[green]cash[/green]",
    "credit": "[yellow]credit[/yellow]",
    "plus": "[magenta]plus[/magenta]",
}


def _buy_cell(p: Product, credit_price: float) -> str:
    verdict = buy_verdict(p, credit_price)
    if verdict is None:
        return "[dim]-[/dim]"
    return _VERDICT_MARKUP[verdict]


def _pph_str(p: Product, currency: str = "$") -> str:
    """Format price-per-hour."""
    pph = price_per_hour(p)
    if pph == float("inf"):
        return "-"
    return f"{currency}{pph:.2f}"


def _price_cell(
    p: Product,
    currency: str,
    max_price: float | None,
    atl_asins: set[str] | None,
) -> str:
    """Price cell: ATL star, threshold coloring, struck list price, and discount."""
    p_str = price_str(p.price, currency)
    if atl_asins and p.asin in atl_asins:
        p_str = "[bold gold1]★[/bold gold1] " + p_str
    if p.price is not None and max_price is not None:
        if p.price <= max_price * 0.6:
            p_str = f"[bold green]{p_str}[/bold green]"
        elif p.price <= max_price:
            p_str = f"[green]{p_str}[/green]"
        else:
            p_str = f"[red]{p_str}[/red]"
    d = p.discount_pct
    if d and d > 0 and p.list_price:
        color = _discount_color(d)
        p_str += f" [dim]{currency}{p.list_price:.0f}[/dim] [{color}]-{d}%[/{color}]"
    return p_str


def _product_identity(p: Product) -> str:
    identity = p.asin
    if p.series_name:
        identity += f" · {p.series_name}"
        if p.series_position:
            identity += f" #{p.series_position}"
    return identity


def _inline_signals(
    p: Product,
    *,
    currency: str,
    credit_price: float | None,
    hist_context: dict[str, int] | None,
    match_context: dict[str, str] | None,
    include_length: bool,
    include_buy: bool,
    include_history: bool,
    include_match: bool,
) -> list[str]:
    lines: list[str] = []
    if include_length:
        lines.append(
            f"[dim]Length[/dim] {p.hours if p.hours else '-'}h · "
            f"[dim]Value[/dim] {_pph_str(p, currency)}/hr"
        )
    lines.append(f"[dim]ID[/dim] [cyan]{_product_identity(p)}[/cyan]")
    signals: list[str] = []
    if include_buy and credit_price is not None:
        signals.append(f"[dim]Buy[/dim] {_buy_cell(p, credit_price)}")
    if include_history and hist_context is not None:
        signals.append(f"[dim]vs hist[/dim] {_hist_cell(hist_context.get(p.asin))}")
    if include_match and match_context is not None and match_context.get(p.asin):
        signals.append(f"[dim]Match[/dim] [cyan]{match_context[p.asin]}[/cyan]")
    if signals:
        lines.append(" · ".join(signals))
    return lines


def _display_product_cards(
    products: list[Product],
    *,
    title: str,
    max_price: float | None,
    atl_asins: set[str] | None,
    hist_context: dict[str, int] | None,
    credit_price: float | None,
    match_context: dict[str, str] | None,
) -> None:
    console.print(f"[bold]{title}[/bold]")
    for index, product in enumerate(products, 1):
        plus = " [magenta][+][/magenta]" if product.in_plus_catalog else ""
        console.print(
            f"\n[bold cyan]@{index}[/bold cyan]  [bold]{product.full_title}[/bold]{plus}"
        )
        if product.authors_str:
            console.print(f"[dim]By[/dim] {product.authors_str}")
        console.print(
            f"[dim]Price[/dim] {_price_cell(product, product.currency, max_price, atl_asins)}"
        )
        console.print(
            f"[dim]Length[/dim] {product.hours if product.hours else '-'}h · "
            f"[dim]Value[/dim] {_pph_str(product, product.currency)}/hr"
        )
        console.print(
            f"[dim]Rating[/dim] {rating_str(product.rating, product.num_ratings)}"
        )
        for line in _inline_signals(
            product,
            currency=product.currency,
            credit_price=credit_price,
            hist_context=hist_context,
            match_context=match_context,
            include_length=False,
            include_buy=True,
            include_history=True,
            include_match=True,
        ):
            console.print(line)


def _hist_cell(pct: int | None) -> str:
    """Cell showing current price vs the historical median."""
    if pct is None:
        return "[dim]-[/dim]"
    if pct < 0:
        return f"[green]{pct}%[/green]"
    if pct == 0:
        return "[dim]0%[/dim]"
    return f"[dim]+{pct}%[/dim]"


def display_products(
    products: list[Product],
    *,
    max_price: float | None = None,
    title: str = "Results",
    currency: str = "$",
    show_url: bool = False,
    atl_asins: set[str] | None = None,
    hist_context: dict[str, int] | None = None,
    credit_price: float | None = None,
    match_context: dict[str, str] | None = None,
) -> None:
    """Display products as a wide table, compact table, or narrow cards."""
    if not products:
        console.print("[dim]No products found.[/dim]")
        return

    term_width = console.width or 80
    if term_width < 80:
        _display_product_cards(
            products,
            title=title,
            max_price=max_price,
            atl_asins=atl_asins,
            hist_context=hist_context,
            credit_price=credit_price,
            match_context=match_context,
        )
        if show_url:
            console.print("\n[bold]URLs[/bold]")
            for index, product in enumerate(products, 1):
                console.print(
                    f"  @{index}. {product.url}", soft_wrap=True, overflow="fold"
                )
        return

    url_width = max(len(product.url) for product in products)
    show_hist = hist_context is not None and any(
        p.asin in hist_context for p in products
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
        3, max(len(str(product.hours)) if product.hours else 1 for product in products)
    )
    pph_width = max(
        7, max(len(_pph_str(product, product.currency)) for product in products)
    )
    hist_width = max(
        7,
        max(
            (
                len(f"{hist_context.get(product.asin):+d}%")
                if hist_context and hist_context.get(product.asin) is not None
                else 1
            )
            for product in products
        ),
    )
    wide = term_width >= 120
    show_buy_column = False
    show_hist_column = False
    show_match_column = False
    show_url_column = False
    match_width = 0
    if wide:
        base_width = (
            ref_width + 24 + price_width + hours_width + pph_width + rating_width + 19
        )
        remaining = term_width - base_width
        if credit_price is not None and remaining >= 10:
            show_buy_column = True
            remaining -= 10
        if show_hist and remaining >= hist_width + 3:
            show_hist_column = True
            remaining -= hist_width + 3
        if match_context is not None:
            desired_match = min(
                24, max(12, *(len(reason) for reason in match_context.values()))
            )
            if remaining >= desired_match + 3:
                show_match_column = True
                match_width = desired_match
                remaining -= desired_match + 3
        if show_url and remaining >= url_width + 3:
            show_url_column = True
            remaining -= url_width + 3
        title_max = max(24, min(80, 24 + max(0, remaining)))
    else:
        title_max = max(24, term_width - price_width - rating_width - 17)

    table = Table(
        title=title,
        show_lines=False,
        padding=(0, 1),
        title_style="bold",
        expand=False,
    )
    table.add_column(
        "Ref", style="dim cyan", width=ref_width, justify="right", no_wrap=True
    )
    table.add_column("Title / Author", max_width=title_max, overflow="ellipsis")
    table.add_column("Price", justify="right", width=price_width, no_wrap=True)
    if wide:
        table.add_column("Hrs", justify="right", width=hours_width, no_wrap=True)
        table.add_column(
            f"{currency}/hr", justify="right", width=pph_width, no_wrap=True
        )
    table.add_column("Rating", justify="right", width=rating_width, no_wrap=True)
    if show_buy_column:
        table.add_column("Buy", width=7)
    if show_hist_column:
        table.add_column("vs hist", justify="right", width=hist_width, no_wrap=True)
    if show_match_column:
        table.add_column("Match", width=match_width, style="cyan")
    if show_url_column:
        table.add_column(
            "URL",
            width=url_width,
            min_width=url_width,
            max_width=url_width,
            no_wrap=True,
            overflow="ignore",
            style="dim cyan",
        )

    for i, p in enumerate(products, 1):
        cur = p.currency
        plus = " [magenta][+][/magenta]" if p.in_plus_catalog else ""
        title_cell = f"{p.title}{plus}"
        if p.authors_str:
            title_cell += f"\n[dim]{p.authors_str}[/dim]"
        if not wide:
            title_cell += "\n" + "\n".join(
                _inline_signals(
                    p,
                    currency=cur,
                    credit_price=credit_price,
                    hist_context=hist_context,
                    match_context=match_context,
                    include_length=True,
                    include_buy=True,
                    include_history=True,
                    include_match=True,
                )
            )
        else:
            title_cell += f"\n[dim cyan]{_product_identity(p)}[/dim cyan]"
            folded = _inline_signals(
                p,
                currency=cur,
                credit_price=credit_price,
                hist_context=hist_context,
                match_context=match_context,
                include_length=False,
                include_buy=not show_buy_column,
                include_history=not show_hist_column,
                include_match=not show_match_column,
            )[1:]
            if folded:
                title_cell += "\n" + "\n".join(folded)
        row = [
            f"@{i}",
            title_cell,
            _price_cell(p, cur, max_price, atl_asins),
        ]
        if wide:
            row.extend(
                [
                    str(p.hours) if p.hours else "-",
                    _pph_str(p, cur),
                ]
            )
        row.append(rating_str(p.rating, p.num_ratings))
        if show_buy_column:
            row.append(_buy_cell(p, credit_price))
        if show_hist_column:
            pct = hist_context.get(p.asin) if hist_context else None
            row.append(_hist_cell(pct))
        if show_match_column:
            row.append(match_context.get(p.asin, ""))
        if show_url_column:
            row.append(p.url)
        table.add_row(*row)

    console.print(table)
    if show_url and not show_url_column:
        console.print("\n[bold]URLs[/bold]")
        for i, product in enumerate(products, 1):
            console.print(f"  @{i}. {product.url}", soft_wrap=True, overflow="fold")


def display_categories(
    categories: list[dict[str, str]], *, title: str = "Categories"
) -> None:
    """Display categories in a table."""
    if not categories:
        console.print("[dim]No categories found.[/dim]")
        return

    table = Table(title=title, show_lines=False, padding=(0, 1), title_style="bold")
    table.add_column("ID", style="cyan", width=16)
    table.add_column("Name", min_width=30)

    for cat in categories:
        table.add_row(cat["id"], cat["name"])

    console.print(table)


def display_product_detail(p: Product, credit_price: float | None = None) -> None:
    """Display detailed info for a single product."""
    lines: list[str] = []
    lines.append(f"[bold]{p.full_title}[/bold]")
    lines.append("")

    if p.authors:
        lines.append(f"  [dim]By:[/dim]        {', '.join(p.authors)}")
    if p.narrators:
        lines.append(f"  [dim]Narrated:[/dim]   {', '.join(p.narrators)}")
    if p.publisher:
        lines.append(f"  [dim]Publisher:[/dim]  {p.publisher}")

    lines.append("")

    cur = p.currency
    price_line = f"  [dim]Price:[/dim]      {price_str(p.price, cur)}"
    if p.list_price and p.price != p.list_price:
        price_line += f"  [dim](was {price_str(p.list_price, cur)})[/dim]"
    if p.discount_pct and p.discount_pct > 0:
        price_line += f"  [bold yellow]-{p.discount_pct}% off[/bold yellow]"
    lines.append(price_line)

    if credit_price is not None and (verdict := buy_verdict(p, credit_price)):
        buy_line = f"  [dim]Buy with:[/dim]   {_VERDICT_MARKUP[verdict]}"
        if verdict == "cash":
            buy_line += (
                f"  [dim](cheaper than a {price_str(credit_price, cur)} credit)[/dim]"
            )
        elif verdict == "credit":
            buy_line += f"  [dim](a {price_str(credit_price, cur)} credit beats the cash price)[/dim]"
        else:
            buy_line += "  [dim](free with membership)[/dim]"
        lines.append(buy_line)

    lines.append(f"  [dim]Rating:[/dim]     {rating_str(p.rating, p.num_ratings)}")
    lines.append(f"  [dim]Length:[/dim]     {p.hours} hours ({p.length_minutes} min)")

    if p.series_name:
        s = p.series_name
        if p.series_position:
            s += f", Book {p.series_position}"
        lines.append(f"  [dim]Series:[/dim]     {s}")

    if p.categories:
        lines.append(f"  [dim]Genres:[/dim]     {' > '.join(p.categories)}")
    if p.language:
        lines.append(f"  [dim]Language:[/dim]   {p.language}")
    if p.release_date:
        lines.append(f"  [dim]Released:[/dim]   {p.release_date}")
    if p.in_plus_catalog:
        lines.append("  [magenta]Included in Audible Plus[/magenta]")

    lines.append("")
    lines.append(f"  [dim link={p.url}]{p.url}[/dim link]")

    console.print(
        Panel(
            "\n".join(lines),
            title=f"[cyan]{p.asin}[/cyan]",
            border_style="dim",
            padding=(1, 2),
        )
    )


def display_comparison(
    products: list[Product], credit_price: float | None = None
) -> None:
    """Display a side-by-side comparison of multiple products."""
    cur = products[0].currency if products else "$"

    table = Table(
        title="Comparison",
        show_lines=True,
        padding=(0, 1),
        title_style="bold",
        expand=False,
    )
    table.add_column("Field", style="dim", width=12)
    for p in products:
        table.add_column(p.asin, style="cyan", max_width=30)

    rows = [
        ("Title", [p.title for p in products]),
        ("Author", [p.authors_str for p in products]),
        ("Narrator", [p.narrators_str for p in products]),
        ("Price", [price_str(p.price, p.currency) for p in products]),
        ("List Price", [price_str(p.list_price, p.currency) for p in products]),
        ("Discount", [discount_str(p.discount_pct) or "-" for p in products]),
        ("Hours", [str(p.hours) if p.hours else "-" for p in products]),
        (f"{cur}/hr", [_pph_str(p, p.currency) for p in products]),
        ("Rating", [rating_str(p.rating, p.num_ratings) for p in products]),
        (
            "Series",
            [
                f"{p.series_name} #{p.series_position}" if p.series_name else "-"
                for p in products
            ],
        ),
        ("Language", [p.language or "-" for p in products]),
        ("Released", [p.release_date or "-" for p in products]),
        ("Plus", ["Yes" if p.in_plus_catalog else "-" for p in products]),
    ]
    if credit_price is not None:
        rows.insert(6, ("Buy", [_buy_cell(p, credit_price) for p in products]))

    for label, values in rows:
        table.add_row(label, *values)

    console.print(table)

    # Highlight the best value
    priced = [p for p in products if p.price is not None and p.hours > 0]
    if priced:
        best = min(priced, key=price_per_hour)
        console.print(
            f"\n  Best value: [bold green]{best.title}[/bold green] "
            f"at {_pph_str(best, best.currency)}/hr"
        )


def create_scan_progress() -> Progress:
    """Create a Rich progress bar for catalog scanning."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("[dim]{task.fields[items]} items fetched[/dim]"),
        console=console,
    )


def display_summary(
    shown: int,
    filtered_out: dict[str, int],
    max_price: float | None = None,
    editions_removed: int = 0,
    series_collapsed: int = 0,
    currency: str = "$",
    total_before_limit: int | None = None,
    noun: str = "deals",
) -> None:
    """Print a summary line after filtering."""
    if total_before_limit is not None and total_before_limit > shown:
        parts = [f"[bold]{shown}[/bold] of {total_before_limit} {noun} shown"]
    else:
        parts = [f"[bold]{shown}[/bold] {noun} found"]
    if max_price is not None:
        parts[0] += f" under [green]{currency}{max_price:.2f}[/green]"
    detail_parts: list[str] = []
    total_filtered = sum(filtered_out.values())
    if total_filtered > 0:
        reasons = ", ".join(
            f"{count} by {label}"
            for label, count in sorted(filtered_out.items(), key=lambda x: -x[1])
        )
        detail_parts.append(f"{total_filtered} filtered out: {reasons}")
    if editions_removed > 0:
        detail_parts.append(f"{editions_removed} duplicate editions removed")
    if series_collapsed > 0:
        detail_parts.append(f"{series_collapsed} series collapsed")
    if detail_parts:
        parts.append(f"[dim]({', '.join(detail_parts)})[/dim]")
    console.print("  " + "  ".join(parts))


def _relative_date(date_str: str, today: datetime.date) -> str:
    """Human-friendly age of an ISO date relative to today."""
    try:
        d = datetime.date.fromisoformat(date_str)
        delta = (today - d).days
        if delta == 0:
            return "today"
        elif delta == 1:
            return "yesterday"
        elif delta < 7:
            return f"{delta}d ago"
        elif delta < 30:
            return f"{delta // 7}w ago"
        else:
            return f"{delta // 30}mo ago"
    except ValueError:
        return ""


def _sparkline(prices: list[float]) -> str:
    """Render prices as a unicode sparkline scaled between their min and max."""
    sparks = " ▁▂▃▄▅▆▇█"
    low, high = min(prices), max(prices)
    if high == low:
        return sparks[4] * len(prices)
    return "".join(sparks[min(8, int((p - low) / (high - low) * 8))] for p in prices)


def display_price_history(entries: list[dict], asin: str, currency: str = "$") -> None:
    """Display price history table with sparkline for an ASIN."""
    today = datetime.date.today()

    table = Table(
        title=f"Price History: {asin}",
        show_lines=False,
        padding=(0, 1),
        title_style="bold",
    )
    table.add_column("Date", width=12)
    table.add_column("Ago", width=10, style="dim")
    table.add_column("Price", justify="right", width=10)
    table.add_column("Change", justify="right", width=10)

    prev_price = None
    prices: list[float] = []
    for entry in entries:
        raw = entry.get("price")
        if not isinstance(raw, (int, float)):
            continue
        price = float(raw)
        p_str = f"{currency}{price:.2f}"
        if prev_price is None or price == prev_price:
            change = "[dim]-[/dim]"
        elif price < prev_price:
            change = f"[green]{price - prev_price:+.2f}[/green]"
        else:
            change = f"[red]+{price - prev_price:.2f}[/red]"
        table.add_row(
            entry.get("date", ""),
            _relative_date(entry.get("date", ""), today),
            p_str,
            change,
        )
        prev_price = price
        prices.append(price)

    console.print(table)

    if not prices:
        console.print("\n  [dim]No numeric prices in history.[/dim]")
        return

    low, high = min(prices), max(prices)
    current = prices[-1]
    console.print(
        f"\n  Low: [green]{currency}{low:.2f}[/green]  High: [red]{currency}{high:.2f}[/red]  Current: {currency}{current:.2f}"
    )

    if len(prices) > 1:
        console.print(f"  [dim]{_sparkline(prices)}[/dim]")


def display_recap(
    drops: list[tuple[str, str, float, float]],
    new_items: list[tuple[str, str, float]],
    wishlist_hits: list[dict],
    days: int,
    currency: str = "$",
    show_new: bool = False,
    atl_hits: list[dict] | None = None,
    atl_all: bool = False,
) -> None:
    """Display a recap of price changes, new items, and wishlist hits."""
    console.print(f"\n[bold]Recap[/bold] (last {days} days)\n")

    def _label(asin: str, title: str) -> str:
        if not title:
            return asin
        t = title if len(title) <= 40 else title[:37] + "..."
        return f"{t}  {asin}"

    if drops:
        console.print(f"  [green]Price drops: {len(drops)}[/green]")
        for asin, title, old, new in sorted(
            drops, key=lambda x: x[2] - x[3], reverse=True
        )[:10]:
            console.print(
                f"    {_label(asin, title)}  {currency}{old:.2f} -> [green]{currency}{new:.2f}[/green]  ([green]-{currency}{old - new:.2f}[/green])"
            )
    else:
        console.print("  [dim]No price drops[/dim]")

    if new_items:
        console.print(f"\n  [cyan]Newly tracked: {len(new_items)}[/cyan]")
        if show_new:
            for asin, title, price in new_items[:10]:
                console.print(
                    f"    [dim]{_label(asin, title)}  {currency}{price:.2f}[/dim]"
                )
    if wishlist_hits:
        console.print(
            f"\n  [bold green]Wishlist items at target: {len(wishlist_hits)}[/bold green]"
        )
        for item in wishlist_hits:
            console.print(f"    {item['asin']}  {item['title']}")

    if atl_hits is not None:
        atl_header = (
            "Tracked items at all-time low:"
            if atl_all
            else "Wishlist items at all-time low:"
        )
        console.print(f"\n  [bold gold1]{atl_header}[/bold gold1]")
        if atl_hits:
            for item in atl_hits:
                target_str = (
                    f" (target {price_str(item['target'], currency)})"
                    if item.get("target") is not None
                    else ""
                )
                console.print(
                    f"    [gold1]{_label(item['asin'], item['title'])}  {currency}{item['price']:.2f}{target_str}[/gold1]"
                )
        else:
            console.print("    [dim]None at ATL[/dim]")

    if not drops and not new_items and not wishlist_hits and atl_hits is None:
        console.print("  [dim]Nothing to report.[/dim]")
    console.print()


def display_wishlist(
    asin_items: list[dict], author_items: list[dict], currency: str = "$"
) -> None:
    """Display the wishlist ASIN and author-watch tables."""
    if asin_items:
        table = Table(
            title="Wishlist", show_lines=False, padding=(0, 1), title_style="bold"
        )
        table.add_column("ASIN", style="cyan", width=14)
        table.add_column("Title", max_width=40)
        table.add_column("Target", justify="right", width=10)

        for item in asin_items:
            target = price_str(item.get("max_price"), currency)
            table.add_row(item.get("asin", ""), item.get("title", ""), target)

        console.print(table)

    if author_items:
        atbl = Table(
            title="Author watches",
            show_lines=False,
            padding=(0, 1),
            title_style="bold",
        )
        atbl.add_column("Author", max_width=40)
        atbl.add_column("Target", justify="right", width=10)
        atbl.add_column("Added", width=12)
        for item in author_items:
            target = price_str(item.get("max_price"), currency)
            atbl.add_row(item.get("author", ""), target, item.get("added", ""))
        console.print(atbl)


def display_watch_table(
    products: list[Product],
    targets: dict[str, int | float | None],
    currency: str = "$",
    buy_only: bool = False,
    show_url: bool = False,
    credit_price: float | None = None,
) -> int:
    """Display a wishlist price-check table. Returns the number of BUY hits."""
    show_url_column = show_url and (console.width or 80) >= 180
    url_width = max((len(product.url) for product in products), default=len("URL"))
    table = Table(
        title="Wishlist Price Check",
        show_lines=False,
        padding=(0, 1),
        title_style="bold",
    )
    table.add_column("Title", max_width=35)
    table.add_column("Price", justify="right", width=12)
    table.add_column("Target", justify="right", width=10)
    table.add_column("Status", width=10)
    if credit_price is not None:
        table.add_column("Buy", width=7)
    if show_url_column:
        table.add_column(
            "URL",
            width=url_width,
            min_width=url_width,
            max_width=url_width,
            no_wrap=True,
            overflow="ignore",
        )

    hits = 0
    displayed_products: list[Product] = []
    for p in products:
        target = targets.get(p.asin)
        product_currency = p.currency or currency
        target_str = price_str(target, product_currency)
        p_str = f"{product_currency}{p.price:.2f}" if p.price is not None else "-"
        is_buy = target is not None and p.price is not None and p.price <= target
        if is_buy:
            status = "[bold green]BUY[/bold green]"
            p_str = f"[bold green]{p_str}[/bold green]"
            hits += 1
        elif p.discount_pct and p.discount_pct > 0:
            status = f"[yellow]-{p.discount_pct}%[/yellow]"
        else:
            status = "[dim]waiting[/dim]"
        if buy_only and not is_buy:
            continue
        displayed_products.append(p)
        row = [
            f"{p.title}\n[dim]{p.authors_str}  [cyan]{p.asin}[/cyan][/dim]",
            p_str,
            target_str,
            status,
        ]
        if credit_price is not None:
            row.append(_buy_cell(p, credit_price))
        if show_url_column:
            row.append(p.url)
        table.add_row(*row)

    console.print(table)
    if show_url and not show_url_column and displayed_products:
        console.print("\n[bold]URLs[/bold]")
        for product in displayed_products:
            console.print(
                f"  {product.asin}: {product.url}",
                soft_wrap=True,
                overflow="ignore",
            )
    if hits:
        console.print(
            f"\n  [bold green]{hits} item(s) at or below target price![/bold green]"
        )
    else:
        console.print(
            f"\n  [dim]No items at target price yet. {len(products)} watched.[/dim]"
        )
    return hits


def display_series_gaps(gaps: list[dict], currency: str = "$") -> None:
    """Display per-series gap report for --gaps mode."""
    if not gaps:
        console.print(
            "[dim]No gaps found in scanned series (filters applied — see --max-price/--on-sale).[/dim]"
        )
        return

    for entry in gaps:
        series_name = entry["series"]
        owned = entry["owned"]
        total = entry["total_known"]
        missing = entry["missing"]
        console.print(
            f"\n[bold]{series_name}[/bold] [dim]— own {owned} of {total}[/dim]"
        )
        for book in missing:
            pos = book["position"]
            pos_str = f"#{pos}" if pos else "  "
            title = book["title"]
            price = book["price"]
            p_str = price_str(price, currency) if price is not None else "-"
            atl = "[bold gold1] ★[/bold gold1]" if book.get("atl") else ""
            console.print(
                f"  [dim]missing[/dim]  {pos_str:<6} {title:<45} {p_str}{atl}"
            )


def display_track_history(runs: list[dict]) -> None:
    """Display a table of track run history entries (newest-first)."""
    if not runs:
        console.print("  [dim]No run history.[/dim]")
        return

    table = Table(
        title="Run History",
        show_lines=False,
        padding=(0, 1),
        title_style="bold",
    )
    table.add_column("When", width=19)
    table.add_column("Duration", justify="right", width=8)
    table.add_column("Checked", justify="right", width=10)
    table.add_column("Hits", justify="right", width=5)
    table.add_column("Monitors", justify="right", width=8)
    table.add_column("Status", min_width=4)

    for run in runs:
        at = run.get("at", "?")
        dur = run.get("duration_s")
        dur_str = f"{dur}s" if dur is not None else "-"
        wishlist = run.get("wishlist_checked", 0) or 0
        extra = run.get("extra_tracked_checked", 0) or 0
        checked_str = f"{wishlist}+{extra}"
        hits = str(run.get("hits", 0) or 0)
        if any(
            key in run
            for key in ("monitors_checked", "monitor_events", "monitor_failures")
        ):
            failures = run.get("monitor_failures", [])
            failure_count = len(failures) if isinstance(failures, list) else 0
            monitors = f"{run.get('monitors_checked', 0) or 0}/{run.get('monitor_events', 0) or 0}/{failure_count}"
        else:
            monitors = "-"
        error = run.get("error")
        if error:
            status_cell = (
                f"[red]{error[:60]}[/red]" if len(error) > 60 else f"[red]{error}[/red]"
            )
        else:
            status_cell = "[green]ok[/green]"
        table.add_row(at, dur_str, checked_str, hits, monitors, status_cell)

    console.print(table)


def display_library_stats(products: list[Product], currency: str = "$") -> None:
    """Display aggregate statistics for a library product list."""
    if not products:
        console.print("[dim]Library is empty.[/dim]")
        return

    total = len(products)
    total_hours = sum(p.hours for p in products)
    avg_hours = total_hours / total
    rated = [p.rating for p in products if p.rating > 0]
    avg_rating = sum(rated) / len(rated) if rated else 0.0

    headline = Table(show_header=False, box=None, padding=(0, 2))
    headline.add_column(style="dim", width=18)
    headline.add_column()
    headline.add_row("Total books", f"[bold]{total:,}[/bold]")
    headline.add_row("Total hours", f"[bold]{total_hours:,.0f} h[/bold]")
    headline.add_row("Avg length", f"{avg_hours:.1f} h")
    if avg_rating:
        headline.add_row("Avg rating", f"{avg_rating:.2f}")
    console.print(headline)

    genre_counts: collections.Counter[str] = collections.Counter()
    for p in products:
        for cat in p.categories:
            genre_counts[cat] += 1

    author_counts: collections.Counter[str] = collections.Counter()
    for p in products:
        for a in p.authors:
            author_counts[a] += 1

    narrator_counts: collections.Counter[str] = collections.Counter()
    for p in products:
        for n in p.narrators:
            narrator_counts[n] += 1

    def _top_table(title: str, counter: collections.Counter[str]) -> None:
        table = Table(
            title=title,
            show_lines=False,
            padding=(0, 1),
            title_style="bold",
            expand=False,
        )
        table.add_column("Name", min_width=30)
        table.add_column("#", justify="right", width=6)
        for name, count in counter.most_common(5):
            table.add_row(name, str(count))
        console.print(table)

    _top_table("Top Genres", genre_counts)
    _top_table("Top Authors", author_counts)
    _top_table("Top Narrators", narrator_counts)
