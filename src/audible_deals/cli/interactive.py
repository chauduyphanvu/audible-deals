"""Interactive result-browsing mode."""

from __future__ import annotations

import click

from audible_deals.product import Product
from audible_deals.display import (
    console,
    display_comparison,
    display_price_history,
    display_product_detail,
)
from audible_deals.price_history import load_price_history
from audible_deals.results_cache import _expand_ref_string
from audible_deals.wishlist import load_wishlist, save_wishlist, wishlist_entry


def _interactive_browse(products: list[Product], currency: str = "$") -> None:
    """Interactive mode: let user pick items to view details, open, or wishlist."""
    _HINT = (
        "\n  [dim]Enter a # for details, 'o #' open, 'w #[,#-#]' wishlist, "
        "'c # #' compare, 'h #' history, '?' help, 'q' quit.[/dim]"
    )
    console.print(_HINT)
    while True:
        try:
            choice = click.prompt("\n>", default="q", show_default=False).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if choice.lower() == "q":
            break

        if choice.strip() in ("?", "help"):
            console.print(_HINT)
            console.print(f"  [dim]Results: 1-{len(products)}[/dim]")
            continue

        parts = choice.split()
        action = "detail"
        idx = -1
        idx2 = -1
        w_ref = None
        try:
            if len(parts) >= 1 and parts[0].lower() == "c":
                if len(parts) != 3:
                    raise ValueError
                action = "compare"
                idx = int(parts[1]) - 1
                idx2 = int(parts[2]) - 1
            elif len(parts) == 2 and parts[0].lower() == "w":
                action = "wishlist"
                w_ref = parts[1]
            elif len(parts) == 2 and parts[0].lower() in ("o", "h"):
                action = {"o": "open", "h": "history"}[parts[0].lower()]
                idx = int(parts[1]) - 1
            else:
                idx = int(parts[0]) - 1
        except (ValueError, IndexError):
            console.print(
                "[dim]Invalid input. Enter a number, 'o #', 'w #[,#-#]', 'c # #', 'h #', '?', or 'q'.[/dim]"
            )
            continue

        if action == "wishlist":
            try:
                one_based = _expand_ref_string(w_ref, label="selection")
            except click.ClickException as exc:
                console.print(f"[dim]{exc.format_message()}[/dim]")
                continue
            selected = [n - 1 for n in dict.fromkeys(one_based)]
            if not all(0 <= z < len(products) for z in selected):
                console.print(f"[dim]Number must be 1-{len(products)}.[/dim]")
                continue

            items = load_wishlist()
            existing_asins = {item.get("asin") for item in items}
            to_add: list[int] = []
            for z in selected:
                p = products[z]
                if p.asin in existing_asins:
                    console.print(f"[dim]{p.asin} already on wishlist[/dim]")
                else:
                    existing_asins.add(p.asin)
                    to_add.append(z)
            if not to_add:
                continue

            target_price = None
            try:
                raw = click.prompt(
                    "  Target price (or Enter to skip)",
                    default="",
                    show_default=False,
                ).strip()
                if raw:
                    target_price = float(raw)
            except (ValueError, EOFError):
                pass

            for z in to_add:
                p = products[z]
                items.append(wishlist_entry(p, target_price))
                target_note = (
                    f" (target: {p.currency}{target_price:.2f})"
                    if target_price is not None
                    else ""
                )
                console.print(
                    f"[green]+[/green] {p.title} added to wishlist{target_note}"
                )
            save_wishlist(items)
            continue

        indices = [idx, idx2] if action == "compare" else [idx]
        if not all(0 <= i < len(products) for i in indices):
            console.print(f"[dim]Number must be 1-{len(products)}.[/dim]")
            continue

        p = products[idx]
        if action == "compare":
            display_comparison([p, products[idx2]])
        elif action == "detail":
            display_product_detail(p)
        elif action == "open":
            console.print(f"[dim]Opening {p.url}[/dim]")
            click.launch(p.url)
        elif action == "history":
            entries = load_price_history(p.asin)
            if not entries:
                console.print(f"[dim]No price history for {p.asin}[/dim]")
            else:
                display_price_history(entries, p.asin, currency)
