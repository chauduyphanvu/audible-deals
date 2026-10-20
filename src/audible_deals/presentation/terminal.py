"""Shared terminal console and catalog scan progress."""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

import click
from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
)

from audible_deals.result_models import CatalogScanPlan, CatalogScanProgress

if TYPE_CHECKING:
    from audible_deals.presentation.live import LiveResults

console = Console()

_BIDI_CONTROLS = frozenset(
    {
        "\u061c",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    }
)


COLOR_MODES = ("auto", "always", "never")


def route_console_to_stderr(ctx: click.Context | None = None) -> None:
    """Send the shared console's human text to stderr (``--json``: stdout is data).

    The console is restored when *ctx* closes, so in-process callers (tests, a
    future long-lived host) that run several commands do not inherit it.
    """
    console.file = sys.stderr
    if ctx is not None:
        ctx.call_on_close(restore_console)


def restore_console() -> None:
    """Point the shared console back at stdout."""
    console.file = None  # type: ignore[assignment]  # None = "whatever stdout is now"


def set_color_mode(mode: str) -> None:
    """Apply ``--color``: ``always`` and ``never`` beat NO_COLOR/FORCE_COLOR.

    ``auto`` keeps what Rich decided from the environment and the terminal.
    Rich offers no public switch for this once a console exists (and every
    module holds the same console object), so the color system is set directly;
    ``tests/test_color.py`` fails if a Rich upgrade changes that.
    """
    if mode == "never":
        console.no_color = True
        console._color_system = None
    elif mode == "always":
        from rich.color import ColorSystem

        console.no_color = False
        if console._color_system is None:
            truecolor = os.environ.get("COLORTERM", "").lower() in (
                "truecolor",
                "24bit",
            )
            console._color_system = (
                ColorSystem.TRUECOLOR if truecolor else ColorSystem.EIGHT_BIT
            )


def safe_text(value: object) -> str:
    """Render untrusted text without terminal control characters."""
    text = value if isinstance(value, str) else str(value)
    rendered: list[str] = []
    for char in text:
        codepoint = ord(char)
        if char == "\t":
            rendered.append(r"\t")
        elif char == "\n":
            rendered.append(r"\n")
        elif char == "\r":
            rendered.append(r"\r")
        elif codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            rendered.append(
                f"\\x{codepoint:02x}" if codepoint <= 0xFF else f"\\u{codepoint:04x}"
            )
        elif char in _BIDI_CONTROLS:
            rendered.append(f"\\u{codepoint:04x}")
        else:
            rendered.append(char)
    return "".join(rendered)


def safe_markup(value: object) -> str:
    """Escape untrusted text for interpolation into trusted Rich markup."""
    return escape(safe_text(value))


def create_scan_progress(*, disable: bool = False) -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("[dim]{task.fields[items]} items fetched[/dim]"),
        console=console,
        disable=disable,
    )


def _progress_updater(
    progress: Progress, task: TaskID, description: str
) -> Callable[[CatalogScanProgress], None]:
    def update(event: CatalogScanProgress) -> None:
        if event.cached:
            # Pages answered by the cache cost no API call; say so.
            progress.update(
                task,
                total=event.total,
                completed=event.completed,
                items=event.items,
                description=f"{safe_markup(description)} · {event.cached} cached",
            )
        else:
            progress.update(
                task,
                total=event.total,
                completed=event.completed,
                items=event.items,
            )

    return update


@contextlib.contextmanager
def catalog_scan_progress(
    plan: CatalogScanPlan,
    description: str,
    *,
    disable: bool = False,
    live: LiveResults | None = None,
) -> Iterator[Callable[[CatalogScanProgress], None]]:
    """The progress bar for a scan, optionally with a live "best so far" table.

    *live* only takes effect on an interactive terminal; everywhere else the
    output is exactly the plain progress bar.
    """
    if plan.total_calls is None:
        raise ValueError("catalog scan categories have not been resolved")
    if live is not None and not disable and console.is_interactive:
        with _live_scan(plan, description, live) as update:
            yield update
        return
    with create_scan_progress(disable=disable) as progress:
        task = progress.add_task(
            safe_markup(description), total=plan.total_calls, items=0
        )
        yield _progress_updater(progress, task, description)


@contextlib.contextmanager
def _live_scan(
    plan: CatalogScanPlan, description: str, live: LiveResults
) -> Iterator[Callable[[CatalogScanProgress], None]]:
    from rich.console import Group
    from rich.live import Live

    # The bar is drawn inside this Live instead of starting its own, because
    # only one live display may be active at a time.
    progress = create_scan_progress()
    task = progress.add_task(safe_markup(description), total=plan.total_calls, items=0)
    update_bar = _progress_updater(progress, task, description)

    def body() -> Group:
        return Group(progress, live.view())

    with Live(body(), console=console, refresh_per_second=8, transient=True) as display:

        def update(event: CatalogScanProgress) -> None:
            update_bar(event)
            if event.new_products:
                live.add(event.new_products)
            display.update(body(), refresh=True)

        yield update
