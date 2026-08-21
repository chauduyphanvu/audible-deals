"""Framework-neutral planning and execution for catalog scans."""

from __future__ import annotations

import dataclasses
import logging
import math
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Protocol

from audible_deals.client import DealsClient
from audible_deals.constants import DEEP_SORT_ORDERS, MAX_PAGE_SIZE, SORT_OPTIONS
from audible_deals.product import Product
from audible_deals.result_models import CatalogScanPlan, CatalogScanProgress

logger = logging.getLogger(__name__)


class CatalogQueryError(ValueError):
    pass


class CatalogScanProgressCallback(Protocol):
    def __call__(self, event: CatalogScanProgress) -> None: ...


def _sort_orders(sort: str, *, deep: bool, fallback: str) -> tuple[str, ...]:
    return tuple(DEEP_SORT_ORDERS if deep else [SORT_OPTIONS.get(sort, fallback)])


def build_search_scan_plan(
    query: str,
    *,
    category_ids: Sequence[str] = ("",),
    sort: str = "relevance",
    deep: bool = False,
    pages: int = 3,
) -> CatalogScanPlan:
    queries = (
        tuple(part.strip() for part in query.split("|") if part.strip())
        if "|" in query
        else (query,)
    )
    if not queries:
        raise CatalogQueryError("No keywords found after splitting on '|'.")
    return CatalogScanPlan.create(
        queries=queries,
        category_ids=category_ids,
        sort_orders=_sort_orders(sort, deep=deep, fallback="Relevance"),
        exact_probe_queries=tuple(item for item in queries if item),
        pages=pages,
    )


def build_find_scan_plan(
    keywords: str,
    *,
    category_ids: Sequence[str] | None = ("",),
    sort: str = "price-per-hour",
    deep: bool = False,
    pages: int = 10,
    exact_probes: bool = True,
) -> CatalogScanPlan:
    return CatalogScanPlan.create(
        queries=(keywords,),
        category_ids=category_ids,
        sort_orders=_sort_orders(sort, deep=deep, fallback="BestSellers"),
        exact_probe_queries=(keywords,) if exact_probes and keywords else (),
        pages=pages,
    )


def build_monitor_scan_plan(
    *,
    mode: str,
    query: str,
    keywords: str,
    category_ids: Sequence[str] = ("",),
    sort: str,
    deep: bool,
    pages: int,
) -> CatalogScanPlan:
    if mode == "search":
        queries = tuple(part.strip() for part in query.split("|") if part.strip())
        if not queries:
            raise CatalogQueryError("--query must contain at least one keyword.")
        return CatalogScanPlan.create(
            queries=queries,
            category_ids=category_ids,
            sort_orders=_sort_orders(sort, deep=deep, fallback="Relevance"),
            exact_probe_queries=queries,
            pages=pages,
        )
    return build_find_scan_plan(
        keywords,
        category_ids=category_ids,
        sort=sort,
        deep=deep,
        pages=pages,
        exact_probes=False,
    )


def bind_catalog_categories(
    plan: CatalogScanPlan, category_ids: Sequence[str]
) -> CatalogScanPlan:
    return dataclasses.replace(plan, category_ids=tuple(category_ids))


def normalized_search_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def rank_catalog_relevance(products: list[Product], query: str) -> list[Product]:
    normalized_query = normalized_search_text(query)
    if not normalized_query:
        return products

    def tier(product: Product) -> int:
        title = normalized_search_text(product.title)
        authors = [normalized_search_text(author) for author in product.authors]
        if title == normalized_query or normalized_query in authors:
            return 0
        if normalized_query in title:
            return 1
        if any(normalized_query in author for author in authors):
            return 2
        return 3

    return sorted(products, key=tier)


def execute_catalog_scan(
    client: DealsClient,
    plan: CatalogScanPlan,
    callback: CatalogScanProgressCallback | None = None,
) -> list[Product]:
    if plan.category_ids is None or plan.total_calls is None:
        raise ValueError("catalog scan categories have not been resolved")

    total = plan.total_calls
    completed = 0
    item_asins: set[str] = set()
    results: list[Product] = []
    result_asins: set[str] = set()
    remaining_probes = Counter(plan.exact_probe_queries)

    def report(description: str) -> None:
        if callback is not None:
            callback(
                CatalogScanProgress(
                    description=description,
                    total=total,
                    completed=completed,
                    items=len(item_asins),
                )
            )

    def fetch_segment(
        query: str, category_id: str, sort_order: str, pages: int, *, exact: bool
    ) -> list[Product]:
        nonlocal completed, total
        description = f"Searching '{query}'" if query else "Scanning catalog"
        query_args = {"title": query} if exact else {"keywords": query}
        segment_products: list[Product] = []
        segment_asins: set[str] = set()
        first_page_seen = False
        for products, page_num, segment_total in client.search_pages(
            **query_args,
            category_id=category_id,
            sort_by=sort_order,
            max_pages=pages,
        ):
            for product in products:
                item_asins.add(product.asin)
                if product.asin not in segment_asins:
                    segment_asins.add(product.asin)
                    segment_products.append(product)
            completed += 1
            if page_num == 1 and not first_page_seen:
                actual = (
                    min(pages, math.ceil(segment_total / MAX_PAGE_SIZE))
                    if segment_total
                    else 1
                )
                total -= pages - actual
                first_page_seen = True
            report(description)
        return segment_products

    def extend_unique(
        destination: list[Product], seen: set[str], products: Iterable[Product]
    ) -> None:
        for product in products:
            if product.asin not in seen:
                seen.add(product.asin)
                destination.append(product)

    for query in plan.queries:
        broad_products: list[Product] = []
        broad_asins: set[str] = set()
        exact_products: list[Product] = []
        exact_asins: set[str] = set()
        for category_id in plan.category_ids:
            for sort_order in plan.sort_orders:
                extend_unique(
                    broad_products,
                    broad_asins,
                    fetch_segment(
                        query, category_id, sort_order, plan.pages, exact=False
                    ),
                )

        if remaining_probes[query]:
            remaining_probes[query] -= 1
            description = f"Searching '{query}'"
            for category_id in plan.category_ids:
                completed_before = completed
                try:
                    products = fetch_segment(
                        query, category_id, "Relevance", 1, exact=True
                    )
                except Exception as exc:
                    if completed == completed_before:
                        completed += 1
                        report(description)
                    logger.info(
                        "Exact-title probe failed for %r in category %r; "
                        "using broad results: %s",
                        query,
                        category_id,
                        exc,
                    )
                else:
                    extend_unique(exact_products, exact_asins, products)

        query_products: list[Product] = []
        query_asins: set[str] = set()
        extend_unique(query_products, query_asins, exact_products)
        extend_unique(query_products, query_asins, broad_products)
        for product in rank_catalog_relevance(query_products, query):
            if product.asin not in result_asins:
                result_asins.add(product.asin)
                results.append(product)

    if callback is not None:
        callback(
            CatalogScanProgress(
                description="Catalog scan complete",
                total=completed,
                completed=completed,
                items=len(item_asins),
            )
        )
    return results
