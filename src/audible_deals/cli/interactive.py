"""Interactive result-browsing mode."""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Literal

import click

from audible_deals import browse_actions
from audible_deals.filtering import _SORT_KEYS
from audible_deals.presentation.products import (
    display_comparison,
    display_product_detail,
    display_products,
)
from audible_deals.presentation.reports import display_price_history
from audible_deals.presentation.terminal import console, safe_markup
from audible_deals.product import Product
from audible_deals.selectors import _expand_ref_string
from audible_deals.wishlist import (
    WishlistMutationError,
    warn_wishlist_issues,
)

INTERACTIVE_HINT = (
    "\n  [dim]Enter # or @# for details, 'o #/@#' open, 'w #/@#[,#-#]' wishlist, "
    "'c @# @#' compare, 'h @#' history, 's <key>' sort, 'n @#[,@#-@#]' not interested, "
    "'?' help, 'q' quit.[/dim]"
)
INTERACTIVE_INVALID_INPUT = (
    "[dim]Invalid input. Enter a number, 'o #', 'w #[,#-#]', 'c # #', "
    "'h #', 's <key>', 'n #[,#-#]', '?', or 'q'.[/dim]"
)

_VALID_SORT_KEYS = browse_actions.VALID_SORT_KEYS


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
    """Unparseable prompt input.

    The message is always a trusted markup constant (``INTERACTIVE_INVALID_INPUT``)
    and is printed as markup; never put user-derived text in it.
    """


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
        atl_asins: AbstractSet[str] | None = None,
        hist_context: Mapping[str, int] | None = None,
        match_context: Mapping[str, str] | None = None,
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
        console.print(f"[dim]Opening {safe_markup(product.url)}[/dim]")
        click.launch(product.url)
        return True

    def _handle_history(self, command: InteractiveCommand) -> bool:
        if not self._positions_in_bounds(command.positions):
            return True
        product = self.products[command.positions[0]]
        entries = browse_actions.history_entries(product)
        if not entries:
            console.print(
                f"[dim]No price history for {safe_markup(product.asin)}[/dim]"
            )
        else:
            display_price_history(entries, product.asin, self.currency)
        return True

    def _handle_wishlist(self, command: InteractiveCommand) -> bool:
        if not self._positions_in_bounds(command.positions):
            return True

        selected = [self.products[position] for position in command.positions]
        selection = _wishlist_operation(browse_actions.select_for_wishlist, selected)
        warn_wishlist_issues(selection.issues)
        for product in selection.already:
            console.print(f"[dim]{safe_markup(product.asin)} already on wishlist[/dim]")
        if not selection.pending:
            return True

        target_price = self._prompt_target_price()
        result = _wishlist_operation(
            browse_actions.add_to_wishlist, selection.pending, target_price
        )
        warn_wishlist_issues(result.issues)
        for event in result.events:
            product = event.product
            if event.action == "raced":
                console.print(
                    f"[dim]{safe_markup(product.asin)} already on wishlist[/dim]"
                )
            else:
                target_note = (
                    f" (target: {product.currency}{target_price:.2f})"
                    if target_price is not None
                    else ""
                )
                console.print(
                    f"[green]+[/green] {safe_markup(product.title)} "
                    f"added to wishlist{target_note}"
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
        target = browse_actions.parse_target_price(raw)
        if target.problem:
            console.print(f"[dim]{target.problem}[/dim]")
        return target.price

    def _handle_sort(self, command: InteractiveCommand) -> bool:
        sort_key = command.sort_key
        if sort_key not in _SORT_KEYS:
            console.print(
                f"[dim]Unknown sort key '{safe_markup(sort_key)}'. "
                f"Valid: {', '.join(_VALID_SORT_KEYS)}[/dim]"
            )
            return True
        self.products[:] = browse_actions.resort(self.products, sort_key)
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
        try:
            remaining = browse_actions.dismiss(self.products, asins)
        except Exception as exc:
            console.print(f"[red]Could not dismiss selection: {safe_markup(exc)}[/red]")
            return True
        self.products[:] = remaining
        console.print(f"[dim]Globally dismissed {len(asins)} title(s).[/dim]")
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


def _interactive_browse(
    products: list[Product],
    currency: str = "$",
    credit_price: float | None = None,
    *,
    title: str = "Results",
    max_price: float | None = None,
    show_url: bool = False,
    atl_asins: AbstractSet[str] | None = None,
    hist_context: Mapping[str, int] | None = None,
    match_context: Mapping[str, str] | None = None,
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
            console.print(f"[dim]{safe_markup(exc.format_message())}[/dim]")
            continue
        except InteractiveCommandParseError as exc:
            # The parse error text is a trusted constant carrying its own
            # markup; escaping it would show literal "[dim]" tags to the user.
            console.print(str(exc))
            continue
        if not browser.dispatch(command):
            break
