"""Rich terminal display for Audible deal finder.

Formats products, categories, and detail views for the terminal using
rich tables and panels.
"""

from __future__ import annotations

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
    if pct >= 70:
        return "bold green"
    elif pct >= 40:
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
) -> None:
    """Display products in a compact rich table."""
    if not products:
        console.print("[dim]No products found.[/dim]")
        return

    table = Table(
        title=title,
        show_lines=False,
        padding=(0, 1),
        title_style="bold",
        expand=False,
    )
    table.add_column("#", style="dim", width=5, justify="right")
    table.add_column("Title / Author", no_wrap=True, max_width=38)
    table.add_column("Price", justify="right", width=12)
    table.add_column("Hrs", justify="right", width=7)
    table.add_column("$/hr", justify="right", width=9)
    table.add_column("Rating", justify="right", width=10)

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

        table.add_row(
            str(i),
            title_line,
            p_str,
            str(p.hours) if p.hours else "-",
            _pph_str(p.price, p.hours, cur),
            rating_str(p.rating, p.num_ratings),
        )

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
        ("$/hr", [_pph_str(p.price, p.hours, p.currency) for p in products]),
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
    filtered_out: int,
    max_price: float | None = None,
    editions_removed: int = 0,
    series_collapsed: int = 0,
    currency: str = "$",
    total_before_limit: int | None = None,
) -> None:
    """Print a summary line after filtering."""
    if total_before_limit is not None and total_before_limit > shown:
        parts = [f"[bold]{shown}[/bold] of {total_before_limit} deals shown"]
    else:
        parts = [f"[bold]{shown}[/bold] deals found"]
    if max_price is not None:
        parts[0] += f" under [green]{currency}{max_price:.2f}[/green]"
    detail_parts = []
    if filtered_out > 0:
        detail_parts.append(f"{filtered_out} filtered out")
    if editions_removed > 0:
        detail_parts.append(f"{editions_removed} duplicate editions removed")
    if series_collapsed > 0:
        detail_parts.append(f"{series_collapsed} series collapsed")
    if detail_parts:
        parts.append(f"[dim]({', '.join(detail_parts)})[/dim]")
    console.print("  " + "  ".join(parts))
