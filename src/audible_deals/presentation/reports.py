"""Terminal report views."""

from __future__ import annotations

import collections
import datetime

from rich.table import Table

from audible_deals.presentation import terminal
from audible_deals.presentation.common import _buy_cell, price_str
from audible_deals.product import Product


def display_categories(
    categories: list[dict[str, str]], *, title: str = "Categories"
) -> None:
    if not categories:
        terminal.console.print("[dim]No categories found.[/dim]")
        return
    table = Table(title=title, show_lines=False, padding=(0, 1), title_style="bold")
    table.add_column("ID", style="cyan", width=16)
    table.add_column("Name", min_width=30)
    for category in categories:
        table.add_row(category["id"], category["name"])
    terminal.console.print(table)


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
            for label, count in sorted(filtered_out.items(), key=lambda item: -item[1])
        )
        detail_parts.append(f"{total_filtered} filtered out: {reasons}")
    if editions_removed > 0:
        detail_parts.append(f"{editions_removed} duplicate editions removed")
    if series_collapsed > 0:
        detail_parts.append(f"{series_collapsed} series collapsed")
    if detail_parts:
        parts.append(f"[dim]({', '.join(detail_parts)})[/dim]")
    terminal.console.print("  " + "  ".join(parts))


def _relative_date(date_str: str, today: datetime.date) -> str:
    try:
        date = datetime.date.fromisoformat(date_str)
        delta = (today - date).days
        if delta == 0:
            return "today"
        if delta == 1:
            return "yesterday"
        if delta < 7:
            return f"{delta}d ago"
        if delta < 30:
            return f"{delta // 7}w ago"
        return f"{delta // 30}mo ago"
    except ValueError:
        return ""


def _sparkline(prices: list[float]) -> str:
    sparks = " ▁▂▃▄▅▆▇█"
    low, high = min(prices), max(prices)
    if high == low:
        return sparks[4] * len(prices)
    return "".join(
        sparks[min(8, int((price - low) / (high - low) * 8))] for price in prices
    )


def display_price_history(entries: list[dict], asin: str, currency: str = "$") -> None:
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

    previous_price = None
    prices: list[float] = []
    for entry in entries:
        raw = entry.get("price")
        if not isinstance(raw, (int, float)):
            continue
        price = float(raw)
        rendered_price = f"{currency}{price:.2f}"
        if previous_price is None or price == previous_price:
            change = "[dim]-[/dim]"
        elif price < previous_price:
            change = f"[green]{price - previous_price:+.2f}[/green]"
        else:
            change = f"[red]+{price - previous_price:.2f}[/red]"
        table.add_row(
            entry.get("date", ""),
            _relative_date(entry.get("date", ""), today),
            rendered_price,
            change,
        )
        previous_price = price
        prices.append(price)

    terminal.console.print(table)
    if not prices:
        terminal.console.print("\n  [dim]No numeric prices in history.[/dim]")
        return
    low, high = min(prices), max(prices)
    current = prices[-1]
    terminal.console.print(
        f"\n  Low: [green]{currency}{low:.2f}[/green]  "
        f"High: [red]{currency}{high:.2f}[/red]  "
        f"Current: {currency}{current:.2f}"
    )
    if len(prices) > 1:
        terminal.console.print(f"  [dim]{_sparkline(prices)}[/dim]")


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
    terminal.console.print(f"\n[bold]Recap[/bold] (last {days} days)\n")

    def _label(asin: str, title: str) -> str:
        if not title:
            return asin
        rendered_title = title if len(title) <= 40 else title[:37] + "..."
        return f"{rendered_title}  {asin}"

    if drops:
        terminal.console.print(f"  [green]Price drops: {len(drops)}[/green]")
        for asin, title, old, new in sorted(
            drops, key=lambda item: item[2] - item[3], reverse=True
        )[:10]:
            terminal.console.print(
                f"    {_label(asin, title)}  {currency}{old:.2f} -> "
                f"[green]{currency}{new:.2f}[/green]  "
                f"([green]-{currency}{old - new:.2f}[/green])"
            )
    else:
        terminal.console.print("  [dim]No price drops[/dim]")

    if new_items:
        terminal.console.print(f"\n  [cyan]Newly tracked: {len(new_items)}[/cyan]")
        if show_new:
            for asin, title, price in new_items[:10]:
                terminal.console.print(
                    f"    [dim]{_label(asin, title)}  {currency}{price:.2f}[/dim]"
                )
    if wishlist_hits:
        terminal.console.print(
            f"\n  [bold green]Wishlist items at target: "
            f"{len(wishlist_hits)}[/bold green]"
        )
        for item in wishlist_hits:
            terminal.console.print(f"    {item['asin']}  {item['title']}")

    if atl_hits is not None:
        atl_header = (
            "Tracked items at all-time low:"
            if atl_all
            else "Wishlist items at all-time low:"
        )
        terminal.console.print(f"\n  [bold gold1]{atl_header}[/bold gold1]")
        if atl_hits:
            for item in atl_hits:
                target_str = (
                    f" (target {price_str(item['target'], currency)})"
                    if item.get("target") is not None
                    else ""
                )
                terminal.console.print(
                    f"    [gold1]{_label(item['asin'], item['title'])}  "
                    f"{currency}{item['price']:.2f}{target_str}[/gold1]"
                )
        else:
            terminal.console.print("    [dim]None at ATL[/dim]")

    if not drops and not new_items and not wishlist_hits and atl_hits is None:
        terminal.console.print("  [dim]Nothing to report.[/dim]")
    terminal.console.print()


def display_wishlist(
    asin_items: list[dict], author_items: list[dict], currency: str = "$"
) -> None:
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
        terminal.console.print(table)

    if author_items:
        table = Table(
            title="Author watches",
            show_lines=False,
            padding=(0, 1),
            title_style="bold",
        )
        table.add_column("Author", max_width=40)
        table.add_column("Target", justify="right", width=10)
        table.add_column("Added", width=12)
        for item in author_items:
            target = price_str(item.get("max_price"), currency)
            table.add_row(item.get("author", ""), target, item.get("added", ""))
        terminal.console.print(table)


def display_watch_table(
    products: list[Product],
    targets: dict[str, int | float | None],
    currency: str = "$",
    buy_only: bool = False,
    show_url: bool = False,
    credit_price: float | None = None,
) -> int:
    show_url_column = show_url and (terminal.console.width or 80) >= 180
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
    for product in products:
        target = targets.get(product.asin)
        product_currency = product.currency or currency
        target_str = price_str(target, product_currency)
        product_price = (
            f"{product_currency}{product.price:.2f}"
            if product.price is not None
            else "-"
        )
        is_buy = (
            target is not None and product.price is not None and product.price <= target
        )
        if is_buy:
            status = "[bold green]BUY[/bold green]"
            product_price = f"[bold green]{product_price}[/bold green]"
            hits += 1
        elif product.discount_pct and product.discount_pct > 0:
            status = f"[yellow]-{product.discount_pct}%[/yellow]"
        else:
            status = "[dim]waiting[/dim]"
        if buy_only and not is_buy:
            continue
        displayed_products.append(product)
        row = [
            f"{product.title}\n[dim]{product.authors_str}  "
            f"[cyan]{product.asin}[/cyan][/dim]",
            product_price,
            target_str,
            status,
        ]
        if credit_price is not None:
            row.append(_buy_cell(product, credit_price))
        if show_url_column:
            row.append(product.url)
        table.add_row(*row)

    terminal.console.print(table)
    if show_url and not show_url_column and displayed_products:
        terminal.console.print("\n[bold]URLs[/bold]")
        for product in displayed_products:
            terminal.console.print(
                f"  {product.asin}: {product.url}",
                soft_wrap=True,
                overflow="ignore",
            )
    if hits:
        terminal.console.print(
            f"\n  [bold green]{hits} item(s) at or below target price![/bold green]"
        )
    else:
        terminal.console.print(
            f"\n  [dim]No items at target price yet. {len(products)} watched.[/dim]"
        )
    return hits


def display_series_gaps(gaps: list[dict], currency: str = "$") -> None:
    if not gaps:
        terminal.console.print(
            "[dim]No gaps found in scanned series (filters applied — see "
            "--max-price/--on-sale).[/dim]"
        )
        return
    for entry in gaps:
        terminal.console.print(
            f"\n[bold]{entry['series']}[/bold] "
            f"[dim]— own {entry['owned']} of {entry['total_known']}[/dim]"
        )
        for book in entry["missing"]:
            position = book["position"]
            position_str = f"#{position}" if position else "  "
            price = book["price"]
            rendered_price = price_str(price, currency) if price is not None else "-"
            atl = "[bold gold1] ★[/bold gold1]" if book.get("atl") else ""
            terminal.console.print(
                f"  [dim]missing[/dim]  {position_str:<6} "
                f"{book['title']:<45} {rendered_price}{atl}"
            )


def display_track_history(runs: list[dict]) -> None:
    if not runs:
        terminal.console.print("  [dim]No run history.[/dim]")
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
        duration = run.get("duration_s")
        duration_str = f"{duration}s" if duration is not None else "-"
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
            monitors = (
                f"{run.get('monitors_checked', 0) or 0}/"
                f"{run.get('monitor_events', 0) or 0}/{failure_count}"
            )
        else:
            monitors = "-"
        error = run.get("error")
        if error:
            status = (
                f"[red]{error[:60]}[/red]" if len(error) > 60 else f"[red]{error}[/red]"
            )
        else:
            status = "[green]ok[/green]"
        table.add_row(at, duration_str, checked_str, hits, monitors, status)
    terminal.console.print(table)


def display_library_stats(products: list[Product], currency: str = "$") -> None:
    if not products:
        terminal.console.print("[dim]Library is empty.[/dim]")
        return
    total = len(products)
    total_hours = sum(product.hours for product in products)
    avg_hours = total_hours / total
    rated = [product.rating for product in products if product.rating > 0]
    avg_rating = sum(rated) / len(rated) if rated else 0.0

    headline = Table(show_header=False, box=None, padding=(0, 2))
    headline.add_column(style="dim", width=18)
    headline.add_column()
    headline.add_row("Total books", f"[bold]{total:,}[/bold]")
    headline.add_row("Total hours", f"[bold]{total_hours:,.0f} h[/bold]")
    headline.add_row("Avg length", f"{avg_hours:.1f} h")
    if avg_rating:
        headline.add_row("Avg rating", f"{avg_rating:.2f}")
    terminal.console.print(headline)

    genre_counts: collections.Counter[str] = collections.Counter()
    for product in products:
        for category in product.categories:
            genre_counts[category] += 1
    author_counts: collections.Counter[str] = collections.Counter()
    for product in products:
        for author in product.authors:
            author_counts[author] += 1
    narrator_counts: collections.Counter[str] = collections.Counter()
    for product in products:
        for narrator in product.narrators:
            narrator_counts[narrator] += 1

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
        terminal.console.print(table)

    _top_table("Top Genres", genre_counts)
    _top_table("Top Authors", author_counts)
    _top_table("Top Narrators", narrator_counts)
