"""Interactive result-browsing mode."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal

import click

from audible_deals.filtering import _SORT_KEYS, sort_local
from audible_deals.presentation.products import (
    display_comparison,
    display_product_detail,
    display_products,
)
from audible_deals.presentation.reports import display_price_history
from audible_deals.presentation.terminal import console
from audible_deals.price_history import load_price_history
from audible_deals.product import Product
from audible_deals.results_cache import save_seen_asins, update_session_view
from audible_deals.selectors import _expand_ref_string
from audible_deals.wishlist import (
    WishlistMutationError,
    warn_wishlist_issues,
)
from audible_deals.wishlist_service import add_products, plan_product_add

INTERACTIVE_HINT = (
    "\n  [dim]Enter # or @# for details, 'o #/@#' open, 'w #/@#[,#-#]' wishlist, "
    "'c @# @#' compare, 'h @#' history, 's <key>' sort, 'n @#[,@#-@#]' not interested, "
    "'?' help, 'q' quit.[/dim]"
)
INTERACTIVE_INVALID_INPUT = (
    "[dim]Invalid input. Enter a number, 'o #', 'w #[,#-#]', 'c # #', "
    "'h #', 's <key>', 'n #[,#-#]', '?', or 'q'.[/dim]"
)

_VALID_SORT_KEYS = sorted(_SORT_KEYS.keys())


def _wishlist_operation(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except WishlistMutationError as exc:
        raise click.ClickException(str(exc)) from exc


InteractiveAction = Literal[
    "quit",
    "help",
    "detail",
    "compare",
    "open",
    "history",
    "wishlist",
    "sort",
    "hide",
]


@dataclass(frozen=True)
class InteractiveCommand:
    action: InteractiveAction
    positions: tuple[int, ...] = ()
    sort_key: str | None = None


class InteractiveCommandParseError(ValueError):
    pass


def parse_interactive_command(text: str) -> InteractiveCommand:
    choice = text.strip()
    if choice.lower() == "q":
        return InteractiveCommand("quit")
    if choice in ("?", "help"):
        return InteractiveCommand("help")

    parts = choice.split()
    verb = parts[0].lower() if parts else ""

    def parse_position(value: str) -> int:
        return int(value.removeprefix("@")) - 1

    try:
        if verb == "c":
            if len(parts) != 3:
                raise InteractiveCommandParseError(INTERACTIVE_INVALID_INPUT)
            return InteractiveCommand(
                "compare", (parse_position(parts[1]), parse_position(parts[2]))
            )
        if len(parts) == 2 and verb in ("w", "n"):
            one_based = _expand_ref_string(parts[1].replace("@", ""), label="selection")
            positions = tuple(n - 1 for n in dict.fromkeys(one_based))
            action: InteractiveAction = "wishlist" if verb == "w" else "hide"
            return InteractiveCommand(action, positions)
        if len(parts) == 2 and verb == "s":
            return InteractiveCommand("sort", sort_key=parts[1].lower())
        if len(parts) == 2 and verb in ("o", "h"):
            action = "open" if verb == "o" else "history"
            return InteractiveCommand(action, (parse_position(parts[1]),))
        return InteractiveCommand("detail", (parse_position(parts[0]),))
    except InteractiveCommandParseError:
        raise
    except (ValueError, IndexError) as exc:
        raise InteractiveCommandParseError(INTERACTIVE_INVALID_INPUT) from exc


class InteractiveBrowser:
    def __init__(
        self,
        products: list[Product],
        currency: str = "$",
        credit_price: float | None = None,
        *,
        title: str = "Results",
        max_price: float | None = None,
        show_url: bool = False,
        atl_asins: set[str] | None = None,
        hist_context: dict[str, int] | None = None,
        match_context: dict[str, str] | None = None,
    ) -> None:
        self.products = products
        self.currency = currency
        self.credit_price = credit_price
        self.title = title
        self.max_price = max_price
        self.show_url = show_url
        self.atl_asins = atl_asins
        self.hist_context = hist_context
        self.match_context = match_context

    def dispatch(self, command: InteractiveCommand) -> bool:
        handlers = {
            "quit": self._handle_quit,
            "help": self._handle_help,
            "detail": self._handle_detail,
            "compare": self._handle_compare,
            "open": self._handle_open,
            "history": self._handle_history,
            "wishlist": self._handle_wishlist,
            "sort": self._handle_sort,
            "hide": self._handle_hide,
        }
        return handlers[command.action](command)

    def _positions_in_bounds(self, positions: tuple[int, ...]) -> bool:
        if positions and all(
            0 <= position < len(self.products) for position in positions
        ):
            return True
        console.print(f"[dim]Number must be 1-{len(self.products)}.[/dim]")
        return False

    def _handle_quit(self, command: InteractiveCommand) -> bool:
        return False

    def _handle_help(self, command: InteractiveCommand) -> bool:
        console.print(INTERACTIVE_HINT)
        console.print(f"  [dim]Results: 1-{len(self.products)}[/dim]")
        return True

    def _handle_detail(self, command: InteractiveCommand) -> bool:
        if not self._positions_in_bounds(command.positions):
            return True
        display_product_detail(
            self.products[command.positions[0]], credit_price=self.credit_price
        )
        return True

    def _handle_compare(self, command: InteractiveCommand) -> bool:
        if not self._positions_in_bounds(command.positions):
            return True
        display_comparison(
            [self.products[position] for position in command.positions],
            credit_price=self.credit_price,
        )
        return True

    def _handle_open(self, command: InteractiveCommand) -> bool:
        if not self._positions_in_bounds(command.positions):
            return True
        product = self.products[command.positions[0]]
        console.print(f"[dim]Opening {product.url}[/dim]")
        click.launch(product.url)
        return True

    def _handle_history(self, command: InteractiveCommand) -> bool:
        if not self._positions_in_bounds(command.positions):
            return True
        product = self.products[command.positions[0]]
        entries = load_price_history(product.asin, product.locale)
        if not entries:
            console.print(f"[dim]No price history for {product.asin}[/dim]")
        else:
            display_price_history(entries, product.asin, self.currency)
        return True

    def _handle_wishlist(self, command: InteractiveCommand) -> bool:
        if not self._positions_in_bounds(command.positions):
            return True

        selected = [self.products[position] for position in command.positions]
        plan = _wishlist_operation(
            plan_product_add, (product.asin for product in selected)
        )
        warn_wishlist_issues(plan.issues)
        stored_asins = set(plan.already_present)
        pending_asins = set(plan.pending_asins)
        seen_pending: set[str] = set()
        pending_products: list[Product] = []
        for product in selected:
            if product.asin in stored_asins or product.asin in seen_pending:
                console.print(f"[dim]{product.asin} already on wishlist[/dim]")
            elif product.asin in pending_asins:
                seen_pending.add(product.asin)
                pending_products.append(product)
        if not pending_products:
            return True

        target_price = self._prompt_target_price()
        result = _wishlist_operation(add_products, pending_products, target_price)
        warn_wishlist_issues(result.issues)
        for event in result.events:
            product = event.product
            if event.action == "raced":
                console.print(f"[dim]{product.asin} already on wishlist[/dim]")
            else:
                target_note = (
                    f" (target: {product.currency}{target_price:.2f})"
                    if target_price is not None
                    else ""
                )
                console.print(
                    f"[green]+[/green] {product.title} added to wishlist{target_note}"
                )
        return True

    def _prompt_target_price(self) -> float | None:
        try:
            raw = click.prompt(
                "  Target price (or Enter to skip)",
                default="",
                show_default=False,
            ).strip()
        except EOFError:
            raw = ""
        if not raw:
            return None
        try:
            parsed = float(raw)
        except ValueError:
            console.print("[dim]Invalid price, no target set[/dim]")
            return None
        if math.isfinite(parsed) and parsed > 0:
            return parsed
        console.print(
            "[dim]Target must be a finite number greater than 0, no target set[/dim]"
        )
        return None

    def _handle_sort(self, command: InteractiveCommand) -> bool:
        sort_key = command.sort_key
        if sort_key not in _SORT_KEYS:
            console.print(
                f"[dim]Unknown sort key '{sort_key}'. "
                f"Valid: {', '.join(_VALID_SORT_KEYS)}[/dim]"
            )
            return True
        self.products[:] = sort_local(self.products, sort_key)
        try:
            update_session_view(
                [product.asin for product in self.products], sort=sort_key
            )
        except Exception:
            pass
        console.print()
        display_products(
            self.products,
            max_price=self.max_price,
            title=self.title,
            currency=self.currency,
            show_url=self.show_url,
            atl_asins=self.atl_asins,
            hist_context=self.hist_context,
            credit_price=self.credit_price,
            match_context=self.match_context,
        )
        return True

    def _handle_hide(self, command: InteractiveCommand) -> bool:
        if not self._positions_in_bounds(command.positions):
            return True
        asins = {self.products[position].asin for position in command.positions}
        save_seen_asins(asins)
        console.print(
            f"[dim]Won't show again in scans with --exclude-seen ({len(asins)} marked)[/dim]"
        )
        return True


def _interactive_browse(
    products: list[Product],
    currency: str = "$",
    credit_price: float | None = None,
    *,
    title: str = "Results",
    max_price: float | None = None,
    show_url: bool = False,
    atl_asins: set[str] | None = None,
    hist_context: dict[str, int] | None = None,
    match_context: dict[str, str] | None = None,
) -> None:
    """Interactive mode: let user pick items to view details, open, or wishlist."""
    browser = InteractiveBrowser(
        products,
        currency,
        credit_price,
        title=title,
        max_price=max_price,
        show_url=show_url,
        atl_asins=atl_asins,
        hist_context=hist_context,
        match_context=match_context,
    )
    console.print(INTERACTIVE_HINT)
    while True:
        try:
            choice = click.prompt("\n>", default="q", show_default=False)
        except (EOFError, KeyboardInterrupt):
            break
        try:
            command = parse_interactive_command(choice)
        except click.ClickException as exc:
            console.print(f"[dim]{exc.format_message()}[/dim]")
            continue
        except InteractiveCommandParseError as exc:
            console.print(str(exc))
            continue
        if not browser.dispatch(command):
            break
