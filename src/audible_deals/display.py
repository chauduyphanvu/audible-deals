"""Rich terminal display for Audible deal finder.

Formats products, categories, and detail views for the terminal using
rich tables and panels.
"""

from __future__ import annotations

import datetime

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from audible_deals.client import Product

console = Console()


def price_str(price: float | None, currency: str = "$") -> str:
    if price is None:
        return "-"
    return f"{currency}{price:.2f}"


def rating_str(rating: float, num_ratings: int = 0) -> str:
    if rating == 0:
        return "-"
    stars = round(rating * 2) / 2
    suffix = f" ({num_ratings:,})" if num_ratings else ""
    return f"{stars:.1f}{suffix}"


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


def _pph_str(price: float | None, hours: float, currency: str = "$") -> str:
    """Format price-per-hour."""
    if price is None or hours <= 0:
        return "-"
    return f"{currency}{price / hours:.2f}"


def display_products(
    products: list[Product],
    *,
    max_price: float | None = None,
    title: str = "Results",
    currency: str = "$",
    show_url: bool = False,
) -> None:
    """Display products in a compact rich table."""
    if not products:
        console.print("[dim]No products found.[/dim]")
        return

    term_width = console.width or 80
    title_max = max(30, min(term_width - 55, 80))

    table = Table(
        title=title,
        show_lines=False,
        padding=(0, 1),
        title_style="bold",
        expand=False,
    )
    table.add_column("#", style="dim", width=5, justify="right")
    table.add_column("Title / Author", no_wrap=True, max_width=title_max)
    table.add_column("Price", justify="right", width=12)
    table.add_column("Hrs", justify="right", width=7)
    table.add_column(f"{currency}/hr", justify="right", width=9)
    table.add_column("Rating", justify="right", width=10)
    if show_url:
        table.add_column("URL", no_wrap=True, style="dim cyan")

    for i, p in enumerate(products, 1):
        cur = p.currency
        # Price cell: combine current, original, and discount
        p_str = price_str(p.price, cur)
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
            p_str += f" [dim]{cur}{p.list_price:.0f}[/dim] [{color}]-{d}%[/{color}]"

        # Title + series + author + ASIN combined
        title_line = p.title
        if p.in_plus_catalog:
            title_line += " [magenta][+][/magenta]"
        if p.series_name:
            series_tag = p.series_name
            if p.series_position:
                series_tag += f" #{p.series_position}"
            title_line += f" [dim italic]({series_tag})[/dim italic]"
        meta = p.authors_str
        if meta:
            meta += f"  [cyan]{p.asin}[/cyan]"
        else:
            meta = f"[cyan]{p.asin}[/cyan]"
        title_line += f"\n[dim]{meta}[/dim]"

        row = [
            str(i),
            title_line,
            p_str,
            str(p.hours) if p.hours else "-",
            _pph_str(p.price, p.hours, cur),
            rating_str(p.rating, p.num_ratings),
        ]
        if show_url:
            row.append(p.url)
        table.add_row(*row)

    console.print(table)


def display_categories(categories: list[dict[str, str]], *, title: str = "Categories") -> None:
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


def display_product_detail(p: Product) -> None:
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

    console.print(Panel(
        "\n".join(lines),
        title=f"[cyan]{p.asin}[/cyan]",
        border_style="dim",
        padding=(1, 2),
    ))


def display_comparison(products: list[Product]) -> None:
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
        (f"{cur}/hr", [_pph_str(p.price, p.hours, p.currency) for p in products]),
        ("Rating", [rating_str(p.rating, p.num_ratings) for p in products]),
        ("Series", [
            f"{p.series_name} #{p.series_position}" if p.series_name else "-"
            for p in products
        ]),
        ("Language", [p.language or "-" for p in products]),
        ("Released", [p.release_date or "-" for p in products]),
        ("Plus", ["Yes" if p.in_plus_catalog else "-" for p in products]),
    ]

    for label, values in rows:
        table.add_row(label, *values)

    console.print(table)

    # Highlight the best value
    priced = [p for p in products if p.price is not None and p.hours > 0]
    if priced:
        best = min(priced, key=lambda p: p.price / p.hours)
        console.print(
            f"\n  Best value: [bold green]{best.title}[/bold green] "
            f"at {_pph_str(best.price, best.hours, best.currency)}/hr"
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


def display_price_history(entries: list[dict], asin: str, currency: str = "$") -> None:
    """Display price history table with sparkline for an ASIN."""
    today = datetime.date.today()

    def _relative_date(date_str: str) -> str:
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

    table = Table(title=f"Price History: {asin}", show_lines=False, padding=(0, 1), title_style="bold")
    table.add_column("Date", width=12)
    table.add_column("Ago", width=10, style="dim")
    table.add_column("Price", justify="right", width=10)
    table.add_column("Change", justify="right", width=10)

    prev_price = None
    for entry in entries:
        price = entry["price"]
        p_str = f"{currency}{price:.2f}"
        if prev_price is not None:
            diff = price - prev_price
            if diff < 0:
                change = f"[green]{diff:+.2f}[/green]"
            elif diff > 0:
                change = f"[red]+{diff:.2f}[/red]"
            else:
                change = "[dim]-[/dim]"
        else:
            change = "[dim]-[/dim]"
        table.add_row(entry["date"], _relative_date(entry["date"]), p_str, change)
        prev_price = price

    console.print(table)

    prices = [e["price"] for e in entries]
    low, high = min(prices), max(prices)
    current = prices[-1]
    console.print(f"\n  Low: [green]{currency}{low:.2f}[/green]  High: [red]{currency}{high:.2f}[/red]  Current: {currency}{current:.2f}")

    if len(prices) > 1:
        sparks = " ▁▂▃▄▅▆▇█"
        if high == low:
            line = sparks[4] * len(prices)
        else:
            line = "".join(sparks[min(8, int((p - low) / (high - low) * 8))] for p in prices)
        console.print(f"  [dim]{line}[/dim]")


def display_recap(
    drops: list[tuple[str, str, float, float]],
    new_items: list[tuple[str, str, float]],
    wishlist_hits: list[dict],
    days: int,
    currency: str = "$",
    show_new: bool = False,
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
        for asin, title, old, new in sorted(drops, key=lambda x: x[2] - x[3], reverse=True)[:10]:
            console.print(f"    {_label(asin, title)}  {currency}{old:.2f} -> [green]{currency}{new:.2f}[/green]  ([green]-{currency}{old - new:.2f}[/green])")
    else:
        console.print("  [dim]No price drops[/dim]")

    if new_items:
        console.print(f"\n  [cyan]Newly tracked: {len(new_items)}[/cyan]")
        if show_new:
            for asin, title, price in new_items[:10]:
                console.print(f"    [dim]{_label(asin, title)}  {currency}{price:.2f}[/dim]")
    if wishlist_hits:
        console.print(f"\n  [bold green]Wishlist items at target: {len(wishlist_hits)}[/bold green]")
        for item in wishlist_hits:
            console.print(f"    {item['asin']}  {item['title']}")

    if not drops and not new_items and not wishlist_hits:
        console.print("  [dim]Nothing to report.[/dim]")
    console.print()


def display_watch_table(
    products: list[Product],
    targets: dict[str, float | None],
    currency: str = "$",
    buy_only: bool = False,
    show_url: bool = False,
) -> int:
    """Display a wishlist price-check table. Returns the number of BUY hits."""
    table = Table(title="Wishlist Price Check", show_lines=False, padding=(0, 1), title_style="bold")
    table.add_column("Title", max_width=35)
    table.add_column("Price", justify="right", width=12)
    table.add_column("Target", justify="right", width=10)
    table.add_column("Status", width=10)
    if show_url:
        table.add_column("URL", max_width=50)

    hits = 0
    for p in products:
        target = targets.get(p.asin)
        target_str = f"{currency}{target:.2f}" if target is not None else "-"
        p_str = f"{currency}{p.price:.2f}" if p.price is not None else "-"
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
        row = [
            f"{p.title}\n[dim]{p.authors_str}  [cyan]{p.asin}[/cyan][/dim]",
            p_str,
            target_str,
            status,
        ]
        if show_url:
            row.append(p.url)
        table.add_row(*row)

    console.print(table)
    if hits:
        console.print(f"\n  [bold green]{hits} item(s) at or below target price![/bold green]")
    else:
        console.print(f"\n  [dim]No items at target price yet. {len(products)} watched.[/dim]")
    return hits
