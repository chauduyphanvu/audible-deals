"""Shared terminal console and catalog scan progress."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
)

from audible_deals.result_models import CatalogScanPlan, CatalogScanProgress

console = Console()


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
        task = progress.add_task(description, total=plan.total_calls, items=0)

        def update(event: CatalogScanProgress) -> None:
            progress.update(
                task,
                total=event.total,
                completed=event.completed,
                items=event.items,
            )

        yield update
