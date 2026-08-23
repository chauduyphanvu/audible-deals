"""Catalog dry-run presentation."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from audible_deals.presentation.terminal import console, safe_markup
from audible_deals.result_models import CatalogScanPlan
from audible_deals.constants import MAX_CATALOG_CALLS_PER_SCAN


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

    def to_dict(self) -> dict:
        plan = self.plan
        return {
            "dry_run": True,
            "category": self.category_name or None,
            "subcategories": plan.category_multiplier,
            "query": self.query or None,
            "result_sort": self.result_sort,
            "limit": self.limit if self.limit and self.limit > 0 else None,
            "profile": self.profile_name,
            "filters": list(self.active_filters),
            "sort_orders": list(plan.sort_orders),
            "pages_per_sort": plan.pages,
            "max_items": plan.max_items,
            "api_calls": plan.total_calls,
            "catalog_call_limit": MAX_CATALOG_CALLS_PER_SCAN,
            "requires_large_scan_override": (
                None
                if plan.total_calls is None
                else plan.total_calls > MAX_CATALOG_CALLS_PER_SCAN
            ),
        }


def render_catalog_dry_run(
    summary: CatalogDryRunSummary,
    *,
    json_flag: bool = False,
    json_writer: Callable[[str], object] = print,
) -> None:
    if json_flag:
        json_writer(
            json.dumps(summary.to_dict(), indent=2, ensure_ascii=False, allow_nan=False)
        )
        return
    plan = summary.plan
    sort_label = ", ".join(plan.sort_orders)
    console.print("\n[bold]Dry run[/bold] — would scan:")
    if summary.category_name:
        console.print(f"  Category: {safe_markup(summary.category_name)}")
    if plan.category_ids is None:
        console.print("  Subcategories: unknown (resolved during scan)")
    elif plan.category_multiplier != 1:
        console.print(f"  Subcategories: {plan.category_multiplier}")
    if summary.query:
        console.print(f"  Query: {safe_markup(summary.query)}")
    console.print(f"  Result sort: {safe_markup(summary.result_sort)}")
    console.print(
        f"  Limit: {summary.limit if summary.limit and summary.limit > 0 else 'unlimited'}"
    )
    console.print(f"  Profile: {safe_markup(summary.profile_name or 'none')}")
    console.print(
        "  Filters: "
        + safe_markup(
            "; ".join(summary.active_filters) if summary.active_filters else "none"
        )
    )
    console.print(f"  Sort orders: {safe_markup(sort_label)}")
    console.print(f"  Pages per sort: {plan.pages}")
    if plan.total_calls is None:
        console.print("  Max items: unknown (depends on subcategory count)")
        console.print("  API calls: unknown (depends on subcategory count)")
    else:
        console.print(f"  Max items: ~{plan.max_items}")
        console.print(f"  API calls: {plan.total_calls}")
        if plan.total_calls > MAX_CATALOG_CALLS_PER_SCAN:
            console.print(
                "  [yellow]Warning: a live run requires --allow-large-scan "
                f"(limit: {MAX_CATALOG_CALLS_PER_SCAN} calls).[/yellow]"
            )
