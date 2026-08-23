"""Persistence and publication workflow for discovery results."""

from __future__ import annotations

import copy
import dataclasses
import datetime
import logging
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from audible_deals.presentation.result_output import (
    ResultPresentationRequest,
    emit_results,
)
from audible_deals.presentation.terminal import console, safe_markup, safe_text
from audible_deals.price_history import (
    history_key,
    hist_percentiles,
    load_price_history,
    price_drop_pcts,
    record_prices,
)
from audible_deals.product import Product
from audible_deals.refresh_eligibility import mark_refresh_eligible
from audible_deals.result_models import DiscoveryResult, ResultRecipe, ResultSession
from audible_deals.results_cache import (
    save_result_session,
    save_seen_asins,
)
from audible_deals.serialization import export_products, serialize_product

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResultSessionSpec:
    producer: str
    locale: str
    recipe: ResultRecipe
    source: dict
    constraints: dict
    ranking_context: dict = dataclasses.field(default_factory=dict)


@dataclass(frozen=True)
class ResultPublicationRequest:
    result: DiscoveryResult
    title: str
    limit: int | None
    output: Path | None
    json_flag: bool
    quiet: bool
    max_price: float | None
    currency: str
    session_spec: ResultSessionSpec
    interactive: bool = False
    show_url: bool = False
    credit_price: float | None = None
    candidates: tuple[Product, ...] = ()
    json_writer: Callable[[str], object] = print

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))


@dataclass(frozen=True)
class ResultPublicationOutcome:
    products: tuple[Product, ...]
    serialized: tuple[dict, ...]
    total_before_limit: int
    session: ResultSession


def record_prices_safely(
    products: list[Product], observation_date: datetime.date | str | None = None
) -> None:
    try:
        record_prices(products, observation_date=observation_date)
    except Exception as exc:
        logger.exception("record_prices failed for %d products", len(products))
        console.print(
            f"[dim]Warning: could not record price history: {safe_markup(exc)}[/dim]"
        )


def mark_refresh_eligible_safely(products: list[Product]) -> None:
    try:
        mark_refresh_eligible(products)
    except Exception:
        logger.warning(
            "Could not update refresh eligibility for %d products",
            len(products),
            exc_info=True,
        )


def _session_for_request(
    request: ResultPublicationRequest,
    visible: list[Product],
    histories: dict[str, list[dict]],
) -> ResultSession:
    spec = request.session_spec
    constraints = copy.deepcopy(spec.constraints)
    constraints["history_percentiles"] = hist_percentiles(
        list(request.candidates), histories
    )
    constraints["price_drop_pcts"] = price_drop_pcts(
        list(request.candidates), histories
    )
    constraints["credit_price"] = request.credit_price
    return ResultSession(
        producer=spec.producer,
        locale=spec.locale,
        title=request.title,
        source=copy.deepcopy(spec.source),
        candidates=[serialize_product(product) for product in request.candidates],
        baseline_recipe=spec.recipe,
        current_recipe=spec.recipe,
        visible_asins=[product.asin for product in visible],
        constraints=constraints,
        ranking_context=copy.deepcopy(spec.ranking_context),
    )


def publish_discovery(
    request: ResultPublicationRequest,
) -> ResultPublicationOutcome:
    all_products = list(request.result.products)
    total_before_limit = len(all_products)
    visible = (
        all_products[: request.limit]
        if request.limit is not None and request.limit > 0
        else all_products
    )

    if request.output:
        export_products(visible, request.output)
        export_message = f"Exported {len(visible)} items to {request.output}"
        if request.json_flag:
            print(safe_text(export_message), file=sys.stderr)
        else:
            console.print(f"[green]{safe_markup(export_message)}[/green]")

    histories = request.result.histories
    if histories is None:
        histories = {
            history_key(product.asin, product.locale): load_price_history(
                product.asin, product.locale
            )
            for product in request.candidates
            if product.price is not None
        }
    session = _session_for_request(request, visible, histories)
    surfaced = [
        product
        for product in visible
        if isinstance(product.price, (int, float))
        and not isinstance(product.price, bool)
        and math.isfinite(product.price)
    ]

    serialized_all = [serialize_product(product) for product in all_products]
    serialized = serialized_all[: len(visible)]

    def commit_presentation() -> None:
        observation_date = datetime.datetime.fromisoformat(session.timestamp).date()
        record_prices_safely(surfaced, observation_date=observation_date)
        mark_refresh_eligible_safely(surfaced)
        try:
            save_result_session(session)
        except Exception:
            logger.warning("Could not save last result session", exc_info=True)
        save_seen_asins({product.asin for product in visible})

    visible_result = dataclasses.replace(
        request.result,
        products=tuple(visible),
        histories=histories,
    )
    emit_results(
        ResultPresentationRequest(
            result=visible_result,
            serialized=tuple(serialized),
            title=request.title,
            json_flag=request.json_flag,
            quiet=request.quiet,
            max_price=request.max_price,
            total_before_limit=total_before_limit,
            currency=request.currency,
            interactive=request.interactive,
            show_url=request.show_url,
            credit_price=request.credit_price,
            suppress_action_footer=request.output is not None,
            json_writer=request.json_writer,
            on_presented=commit_presentation,
        )
    )
    return ResultPublicationOutcome(
        products=tuple(visible),
        serialized=tuple(serialized),
        total_before_limit=total_before_limit,
        session=session,
    )
