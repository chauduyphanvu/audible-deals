"""Shared terminal console and catalog scan progress."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator

from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
)

from audible_deals.result_models import CatalogScanPlan, CatalogScanProgress

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


@contextlib.contextmanager
def catalog_scan_progress(
    plan: CatalogScanPlan, description: str, *, disable: bool = False
) -> Iterator[Callable[[CatalogScanProgress], None]]:
    if plan.total_calls is None:
        raise ValueError("catalog scan categories have not been resolved")
    with create_scan_progress(disable=disable) as progress:
        task = progress.add_task(
            safe_markup(description), total=plan.total_calls, items=0
        )

        def update(event: CatalogScanProgress) -> None:
            progress.update(
                task,
                total=event.total,
                completed=event.completed,
                items=event.items,
            )

        yield update
