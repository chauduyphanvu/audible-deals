"""Framework-neutral planning and execution for catalog scans."""

from __future__ import annotations

import dataclasses
import logging
import math
import unicodedata
from collections.abc import Sequence
from typing import Protocol

from audible_deals.client import CatalogSearchRequest, DealsClient
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
    requests: list[CatalogSearchRequest] = []
    metadata: list[tuple[int, str, str, str]] = []
    remaining_probes = list(plan.exact_probe_queries)

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

    def page_fetched(
        request_index: int,
        products: list[Product],
        page_num: int,
        segment_total: int,
    ) -> None:
        nonlocal completed, total
        request = requests[request_index]
        query = request.title or request.keywords
        description = f"Searching '{query}'" if query else "Scanning catalog"
        item_asins.update(product.asin for product in products)
        completed += 1
        if page_num == 1:
            actual = (
                min(request.max_pages, math.ceil(segment_total / MAX_PAGE_SIZE))
                if segment_total
                else 1
            )
            total -= request.max_pages - actual
        report(description)

    for query_index, query in enumerate(plan.queries):
        for category_id in plan.category_ids:
            for sort_order in plan.sort_orders:
                requests.append(
                    CatalogSearchRequest(
                        keywords=query,
                        category_id=category_id,
                        sort_by=sort_order,
                        max_pages=plan.pages,
                    )
                )
                metadata.append((query_index, "broad", query, category_id))
        if query in remaining_probes:
            remaining_probes.remove(query)
            for category_id in plan.category_ids:
                requests.append(
                    CatalogSearchRequest(
                        title=query,
                        category_id=category_id,
                        sort_by="Relevance",
                        max_pages=1,
                        optional=True,
                    )
                )
                metadata.append((query_index, "exact", query, category_id))

    segment_results = client.search_segments(requests, page_fetched)
    broad_by_query: list[list[Product]] = [[] for _ in plan.queries]
    exact_by_query: list[list[Product]] = [[] for _ in plan.queries]
    broad_seen: list[set[str]] = [set() for _ in plan.queries]
    exact_seen: list[set[str]] = [set() for _ in plan.queries]

    for meta, segment_result in zip(metadata, segment_results):
        query_index, kind, query, category_id = meta
        if segment_result.error is not None:
            logger.info(
                "Exact-title probe failed for %r in category %r; using broad results: %s",
                query,
                category_id,
                segment_result.error,
            )
            continue
        destination = (
            exact_by_query[query_index]
            if kind == "exact"
            else broad_by_query[query_index]
        )
        seen = exact_seen[query_index] if kind == "exact" else broad_seen[query_index]
        for products, _, _ in segment_result.pages:
            for product in products:
                if product.asin not in seen:
                    seen.add(product.asin)
                    destination.append(product)

    results: list[Product] = []
    result_asins: set[str] = set()
    for query_index, query in enumerate(plan.queries):
        query_products: list[Product] = []
        query_asins: set[str] = set()
        for product in (*exact_by_query[query_index], *broad_by_query[query_index]):
            if product.asin not in query_asins:
                query_asins.add(product.asin)
                query_products.append(product)
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
