"""Presentation of processed discovery results."""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable
from dataclasses import dataclass

from audible_deals.price_history import (
    history_key,
    load_price_history,
    price_history_context,
)
from audible_deals.presentation.products import display_products
from audible_deals.presentation.reports import display_summary
from audible_deals.presentation.terminal import console
from audible_deals.product import Product
from audible_deals.result_models import DiscoveryResult


@dataclass(frozen=True)
class ResultPresentationRequest:
    result: DiscoveryResult
    serialized: tuple[dict, ...]
    title: str
    json_flag: bool
    quiet: bool
    max_price: float | None
    total_before_limit: int
    currency: str = "$"
    interactive: bool = False
    show_url: bool = False
    credit_price: float | None = None
    suppress_action_footer: bool = False
    json_writer: Callable[[str], object] = print
    on_presented: Callable[[], object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "serialized", tuple(self.serialized))


def _prior_histories(products: list[Product], histories) -> dict[str, list[dict]]:
    today_iso = datetime.date.today().isoformat()
    source = histories
    if source is None:
        source = {
            history_key(product.asin, product.locale): load_price_history(
                product.asin, product.locale
            )
            for product in products
            if product.price is not None
        }
    return {
        key: [dict(entry) for entry in entries if entry.get("date") != today_iso]
        for key, entries in source.items()
    }


def emit_results(request: ResultPresentationRequest) -> None:
    filtered = list(request.result.products)
    if request.json_flag:
        request.json_writer(
            json.dumps(request.serialized, indent=2, ensure_ascii=False)
        )
    need_context = (not request.json_flag and not request.quiet) or request.interactive
    atl_asins = request.result.atl_asins
    hist_context = request.result.hist_context
    if need_context and atl_asins is None and hist_context is None:
        atl_asins, hist_context = price_history_context(
            filtered,
            histories=_prior_histories(filtered, request.result.histories),
        )
    if not request.json_flag and not request.quiet:
        console.print()
        display_products(
            filtered,
            max_price=request.max_price,
            title=request.title,
            currency=request.currency,
            show_url=request.show_url,
            atl_asins=atl_asins,
            hist_context=hist_context,
            credit_price=request.credit_price,
            match_context=request.result.match_reasons,
        )
        display_summary(
            len(filtered),
            request.result.breakdown,
            max_price=request.max_price,
            editions_removed=request.result.editions_removed,
            series_collapsed=request.result.series_collapsed,
            currency=request.currency,
            total_before_limit=request.total_before_limit,
        )
        if (
            filtered
            and not request.interactive
            and not request.suppress_action_footer
            and console.is_terminal
        ):
            actions = ["deals detail @1", "deals wishlist add @1"]
            if len(filtered) >= 2:
                actions.insert(1, "deals compare @1 @2")
            console.print("  [dim]Next: " + " · ".join(actions) + "[/dim]")
    if request.on_presented is not None:
        request.on_presented()
    if request.interactive and filtered and not request.json_flag:
        from audible_deals.cli.interactive import _interactive_browse

        _interactive_browse(
            filtered,
            currency=request.currency,
            credit_price=request.credit_price,
            title=request.title,
            max_price=request.max_price,
            show_url=request.show_url,
            atl_asins=atl_asins,
            hist_context=hist_context,
            match_context=request.result.match_reasons,
        )
