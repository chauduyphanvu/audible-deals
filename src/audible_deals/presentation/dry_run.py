"""Catalog dry-run presentation."""

from __future__ import annotations

from dataclasses import dataclass

from audible_deals.presentation.terminal import console
from audible_deals.result_models import CatalogScanPlan


@dataclass(frozen=True)
class CatalogDryRunSummary:
    plan: CatalogScanPlan
    category_name: str
    query: str
    result_sort: str
    limit: int | None
    profile_name: str | None
    active_filters: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "active_filters", tuple(self.active_filters))


def render_catalog_dry_run(summary: CatalogDryRunSummary) -> None:
    plan = summary.plan
    sort_label = ", ".join(plan.sort_orders)
    console.print("\n[bold]Dry run[/bold] — would scan:")
    if summary.category_name:
        console.print(f"  Category: {summary.category_name}")
    if plan.category_ids is None:
        console.print("  Subcategories: unknown (resolved during scan)")
    elif plan.category_multiplier != 1:
        console.print(f"  Subcategories: {plan.category_multiplier}")
    if summary.query:
        console.print(f"  Query: {summary.query}")
    console.print(f"  Result sort: {summary.result_sort}")
    console.print(
        f"  Limit: {summary.limit if summary.limit and summary.limit > 0 else 'unlimited'}"
    )
    console.print(f"  Profile: {summary.profile_name or 'none'}")
    console.print(
        "  Filters: "
        + ("; ".join(summary.active_filters) if summary.active_filters else "none")
    )
    console.print(f"  Sort orders: {sort_label}")
    console.print(f"  Pages per sort: {plan.pages}")
    if plan.total_calls is None:
        console.print("  Max items: unknown (depends on subcategory count)")
        console.print("  API calls: unknown (depends on subcategory count)")
    else:
        console.print(f"  Max items: ~{plan.max_items}")
        console.print(f"  API calls: {plan.total_calls}")
