"""The Textual results browser: a table of the results with a detail pane.

Keys: arrows or j/k move, Enter shows the title's details, ``/`` filters the
visible rows as you type, ``s`` cycles the sort (and remembers it in the saved
result session, so ``@N`` and ``deals last`` follow what you see), space marks,
``w`` adds the marked (or current) titles to the wishlist, ``o`` opens the page,
``c`` compares 2 to 4 marked titles, ``h`` shows the price history, ``n`` hides
titles for good, ``?`` lists the keys and ``q`` quits.

Every action goes through ``audible_deals.browse_actions`` and the presentation
renderables the prompt loop shares; this module only decides what is on screen.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import ClassVar, Literal

import click
from rich.console import RenderableType
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Static

from audible_deals import browse_actions
from audible_deals.presentation.products import (
    ProductDisplayOptions,
    calculate_product_layout,
    comparison_renderable,
    product_columns,
    product_detail_panel,
    product_row,
)
from audible_deals.presentation.reports import price_history_renderable
from audible_deals.presentation.terminal import safe_text
from audible_deals.product import Product
from audible_deals.wishlist import WishlistMutationError

logger = logging.getLogger(__name__)

SideMode = Literal["detail", "history"]

#: ``compare`` takes between these many marked titles (like the prompt loop's
#: two, widened by the comparison table, which has room for four).
COMPARE_MIN, COMPARE_MAX = 2, 4

#: (key, what it does): the help screen and the footer both come from here.
KEY_HELP: tuple[tuple[str, str], ...] = (
    ("up/down, j/k", "move"),
    ("Enter", "details of the title"),
    ("/", "filter by title, author, narrator or series"),
    ("Esc", "clear the filter, close a pane, or quit"),
    ("s", "next sort (remembered for @N and `deals last`)"),
    ("space", "mark / unmark (then moves down)"),
    ("w", "add marked (or current) titles to the wishlist"),
    ("o", "open the Audible page"),
    ("c", "compare 2 to 4 marked titles"),
    ("h", "price history"),
    ("n", "not interested: hide marked (or current) titles for good"),
    ("?", "this help"),
    ("q", "quit"),
)


def matches_filter(product: Product, query: str) -> bool:
    """Whether *product* contains every word of *query* (title, people, series)."""
    words = query.lower().split()
    if not words:
        return True
    haystack = " ".join(
        [
            product.title,
            product.subtitle,
            *product.authors,
            *product.narrators,
            product.series_name,
        ]
    ).lower()
    return all(word in haystack for word in words)


@dataclass(frozen=True)
class TargetAnswer:
    """What the target-price dialog returned (``price`` may be ``None``: no target)."""

    price: float | None


class TargetPriceScreen(ModalScreen["TargetAnswer | None"]):
    """Ask for the wishlist target price; Enter adds, Esc cancels."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel")
    ]

    def __init__(self, count: int, default: str, currency: str) -> None:
        super().__init__()
        self.count = count
        self.default = default
        self.currency = currency

    def compose(self) -> ComposeResult:
        what = "this title" if self.count == 1 else f"these {self.count} titles"
        with Vertical(id="dialog"):
            yield Static(f"Add {what} to the wishlist", markup=False)
            yield Static(
                f"Target price in {self.currency} (empty for no target)", markup=False
            )
            yield Input(value=self.default, id="target")
            yield Static("", id="error", markup=False)
            yield Static("Enter adds - Esc cancels", id="hint", markup=False)

    def on_mount(self) -> None:
        self.query_one("#target", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        target = browse_actions.parse_target_price(event.value)
        if target.problem:
            self.query_one("#error", Static).update(
                "Enter a number greater than 0, or leave it empty for no target"
            )
            return
        self.dismiss(TargetAnswer(target.price))

    def action_cancel(self) -> None:
        self.dismiss(None)


class CompareScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
    ]

    def __init__(self, renderable: RenderableType) -> None:
        super().__init__()
        self.renderable = renderable

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="compare"):
            yield Static(self.renderable, id="compare-body")
        yield Static("Esc closes", id="hint", markup=False)

    def action_close(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("question_mark", "close", "Close"),
    ]

    def compose(self) -> ComposeResult:
        text = Text()
        for key, meaning in KEY_HELP:
            text.append(f"{key:<14}", style="bold cyan")
            text.append(f"{meaning}\n")
        with Vertical(id="help"):
            yield Static(text, id="help-body")
            yield Static("Esc closes", id="hint", markup=False)

    def action_close(self) -> None:
        self.dismiss(None)


class ResultsApp(App[None]):
    """Browse a list of results in the terminal."""

    TITLE = "deals"
    CSS = """
    #filter { display: none; }
    #filter.active { display: block; }
    #body { height: 1fr; }
    #results { width: 1fr; }
    #side { width: 40%; display: none; border-left: tall $primary 40%; }
    #side.active { display: block; }
    TargetPriceScreen, CompareScreen, HelpScreen { align: center middle; }
    #dialog, #help {
        width: 64; height: auto; padding: 1 2;
        border: thick $primary; background: $surface;
    }
    #error { color: $error; }
    #hint { color: $text-muted; }
    #compare { width: 96%; height: 90%; padding: 1 2; background: $surface; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "leave", "Back", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("slash", "filter", "Filter"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("space", "toggle_mark", "Mark"),
        Binding("w", "wishlist", "Wishlist"),
        Binding("o", "open", "Open"),
        Binding("c", "compare", "Compare"),
        Binding("h", "history", "History"),
        Binding("n", "dismiss_titles", "Not interested"),
        Binding("question_mark", "help", "Help"),
    ]

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
        sort_key: str | None = None,
    ) -> None:
        super().__init__()
        self.products = list(products)
        self.currency = currency
        self.credit_price = credit_price
        self.results_title = title
        self.options = ProductDisplayOptions(
            max_price=max_price,
            title=title,
            currency=currency,
            show_url=show_url,
            atl_asins=atl_asins,
            hist_context=hist_context,
            credit_price=credit_price,
            match_context=match_context,
        )
        self.sort_key = sort_key
        self.filter_text = ""
        self.marked: set[str] = set()
        self.side_mode: SideMode | None = None
        #: What was done, printed after the screen closes (it leaves no trace otherwise).
        self.summary: list[str] = []
        self._visible: list[tuple[int, Product]] = []

    # -- layout -------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(
            placeholder="Filter by title, author, narrator or series (Esc clears)",
            id="filter",
        )
        with Horizontal(id="body"):
            yield DataTable(id="results", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="side"):
                yield Static("", id="side-content")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#results", DataTable).focus()
        self._rebuild()

    # -- the table ----------------------------------------------------------------

    @property
    def table(self) -> DataTable:
        return self.query_one("#results", DataTable)

    def current(self) -> Product | None:
        """The product under the cursor, if any row is shown."""
        if not self._visible:
            return None
        row = self.table.cursor_row
        if 0 <= row < len(self._visible):
            return self._visible[row][1]
        return None

    def _targets(self) -> list[Product]:
        """The marked titles in the order shown, else the one under the cursor."""
        if self.marked:
            return [p for p in self.products if p.asin in self.marked]
        current = self.current()
        return [current] if current is not None else []

    def _rebuild(self, keep: str | None = None) -> None:
        """Redraw the table from ``self.products`` and the filter, keeping the cursor."""
        if keep is None:
            current = self.current()
            keep = current.asin if current is not None else None
        self._visible = [
            (position, product)
            for position, product in enumerate(self.products, 1)
            if matches_filter(product, self.filter_text)
        ]
        table = self.table
        table.clear(columns=True)
        if not self.products:
            self._refresh_chrome()
            return
        # With the side pane open the table has less room, so it takes the
        # compact layout (the same one the terminal table uses when narrow).
        available = self.size.width - (
            self.size.width * 2 // 5 if self.side_mode else 0
        )
        layout = calculate_product_layout(
            self.products, self.options, max(available, 80)
        )
        columns = product_columns(self.options, layout)
        table.add_column("", key="mark", width=1)
        for index, column in enumerate(columns):
            table.add_column(
                Text(column.title, justify=column.justify),
                key=f"c{index}",
                width=column.width
                if column.title != "Title / Author"
                else min(layout.title_width or 48, 56),
            )
        seen: set[str] = set()
        for position, product in self._visible:
            cells = product_row(product, position, self.options, layout)
            key = (
                product.asin
                if product.asin not in seen
                else f"{product.asin}:{position}"
            )
            seen.add(key)
            table.add_row(
                Text("●" if product.asin in self.marked else " ", style="bold magenta"),
                *(
                    Text.from_markup(cell, justify=column.justify)
                    for cell, column in zip(cells, columns, strict=True)
                ),
                height=None,
                key=key,
            )
        if keep is not None:
            for row, (_position, product) in enumerate(self._visible):
                if product.asin == keep:
                    table.move_cursor(row=row, animate=False)
                    break
        self._refresh_chrome()
        self._refresh_side()

    def _refresh_chrome(self) -> None:
        shown, total = len(self._visible), len(self.products)
        count = f"{total} result(s)" if shown == total else f"{shown} of {total}"
        parts = [safe_text(self.results_title), count]
        if self.filter_text:
            parts.append(f"filter: {safe_text(self.filter_text)}")
        if self.sort_key:
            parts.append(f"sort: {self.sort_key}")
        if self.marked:
            parts.append(f"{len(self.marked)} marked")
        self.sub_title = " - ".join(parts)

    # -- the side pane ------------------------------------------------------------

    def _refresh_side(self) -> None:
        side = self.query_one("#side")
        side.set_class(self.side_mode is not None, "active")
        if self.side_mode is None:
            return
        product = self.current()
        content = self.query_one("#side-content", Static)
        if product is None:
            content.update(Text("Nothing to show."))
        elif self.side_mode == "detail":
            content.update(product_detail_panel(product, self.credit_price))
        else:
            entries = browse_actions.history_entries(product)
            content.update(
                price_history_renderable(entries, product.asin, self.currency)
                if entries
                else Text(f"No price history for {product.asin}")
            )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._refresh_side()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._set_side("detail")

    def _set_side(self, mode: SideMode | None) -> None:
        """Show *mode* in the side pane (``None`` hides it); the table re-fits."""
        was_open = self.side_mode is not None
        self.side_mode = mode
        if was_open != (mode is not None):
            self._rebuild()
        else:
            self._refresh_side()

    def on_resize(self, event: events.Resize) -> None:
        if self.query("#results"):  # not before the table exists
            self._rebuild()

    # -- keys ---------------------------------------------------------------------

    def action_cursor_down(self) -> None:
        self.table.action_cursor_down()

    def action_cursor_up(self) -> None:
        self.table.action_cursor_up()

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.add_class("active")
        box.focus()

    def action_leave(self) -> None:
        """Esc: clear the filter, else close the side pane, else quit."""
        box = self.query_one("#filter", Input)
        if self.filter_text or box.has_class("active"):
            self.filter_text = ""
            box.value = ""
            box.remove_class("active")
            self.table.focus()
            self._rebuild()
        elif self.side_mode is not None:
            self._set_side(None)
        else:
            self.exit()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self.filter_text = event.value
            self._rebuild()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter":
            self.table.focus()  # keep the filter, go back to the rows

    def action_cycle_sort(self) -> None:
        self.sort_key = browse_actions.next_sort_key(self.sort_key)
        current = self.current()
        self.products = browse_actions.resort(self.products, self.sort_key)
        self._rebuild(keep=current.asin if current else None)
        self.notify(f"Sorted by {self.sort_key}", markup=False)

    def action_toggle_mark(self) -> None:
        product = self.current()
        if product is None:
            return
        self.marked.symmetric_difference_update({product.asin})
        row = self.table.cursor_row
        self._rebuild()
        if row + 1 < len(self._visible):
            self.table.move_cursor(row=row + 1, animate=False)

    def action_open(self) -> None:
        product = self.current()
        if product is None:
            return
        click.launch(product.url)
        self.notify(f"Opening {product.url}", markup=False)

    def action_history(self) -> None:
        self._set_side(None if self.side_mode == "history" else "history")

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_compare(self) -> None:
        chosen = [p for p in self.products if p.asin in self.marked]
        if not COMPARE_MIN <= len(chosen) <= COMPARE_MAX:
            self.notify(
                f"Mark {COMPARE_MIN} to {COMPARE_MAX} titles with space first "
                f"({len(chosen)} marked).",
                severity="warning",
                markup=False,
            )
            return
        self.push_screen(
            CompareScreen(comparison_renderable(chosen, self.credit_price))
        )

    def action_wishlist(self) -> None:
        targets = self._targets()
        if not targets:
            return
        try:
            selection = browse_actions.select_for_wishlist(targets)
        except WishlistMutationError as exc:
            self.notify(str(exc), severity="error", markup=False)
            return
        if not selection.pending:
            self.notify("Already on the wishlist.", markup=False)
            return
        default = ""
        if len(selection.pending) == 1 and selection.pending[0].price is not None:
            default = f"{selection.pending[0].price:.2f}"

        def finished(answer: TargetAnswer | None) -> None:
            if answer is None:
                return
            try:
                result = browse_actions.add_to_wishlist(selection.pending, answer.price)
            except WishlistMutationError as exc:
                self.notify(str(exc), severity="error", markup=False)
                return
            added = [p for p in result.added_products]
            for product in added:
                self.marked.discard(product.asin)
            target = (
                f" (target {self.currency}{answer.price:.2f})" if answer.price else ""
            )
            message = f"Added {len(added)} title(s) to the wishlist{target}"
            self.summary.append(message)
            self.notify(message, markup=False)
            self._rebuild()

        self.push_screen(
            TargetPriceScreen(len(selection.pending), default, self.currency), finished
        )

    def action_dismiss_titles(self) -> None:
        targets = self._targets()
        if not targets:
            return
        asins = {product.asin for product in targets}
        try:
            self.products = browse_actions.dismiss(self.products, asins)
        except Exception as exc:
            self.notify(f"Could not dismiss: {exc}", severity="error", markup=False)
            return
        self.marked -= asins
        message = f"Dismissed {len(asins)} title(s) for good"
        self.summary.append(message)
        self.notify(message, markup=False)
        self._rebuild()


def run_results_app(
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
    sort_key: str | None = None,
) -> list[str]:
    """Run the browser until the user quits; returns what was done (for the terminal)."""
    app = ResultsApp(
        products,
        currency,
        credit_price,
        title=title,
        max_price=max_price,
        show_url=show_url,
        atl_asins=atl_asins,
        hist_context=hist_context,
        match_context=match_context,
        sort_key=sort_key,
    )
    app.run()
    return app.summary
