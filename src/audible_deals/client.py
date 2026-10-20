"""Domain facade for catalog browsing and deal discovery."""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from audible_deals import cache_store as _cache_store
from audible_deals import constants as _constants
from audible_deals import genres as _genres
from audible_deals import paths as _paths
from audible_deals import product as _product
from audible_deals.audible_transport import AudibleTransport as _AudibleTransport
from audible_deals.auth_store import AuthStore as _AuthStore
from audible_deals.pagination import (
    MAX_CONCURRENT_FETCHES,
    page_count,
    prefetch_in_order,
    read_serially,
)

logger = logging.getLogger(__name__)

_CATEGORIES_NAMESPACE = "categories"


_CATEGORY_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,30}$")


def _validate_category_id(value: str) -> None:
    """Validate category ID to prevent URL path injection."""
    if not _CATEGORY_ID_RE.fullmatch(value):
        raise ValueError(f"Invalid category ID format: {value!r}")


# One page of the user's library: (products, items on the page, reported total).
_LibraryPage = tuple[list[_product.Product], int, Any]


def _parse_api_products(raw_products: object, locale: str) -> list[_product.Product]:
    if not isinstance(raw_products, list):
        logger.warning("API products payload is not a list; ignoring it")
        return []
    products: list[_product.Product] = []
    for raw in raw_products:
        if not isinstance(raw, dict):
            logger.warning("Skipping malformed API product record")
            continue
        try:
            products.append(_product.parse_product(raw, locale=locale))
        except ValueError as exc:
            logger.warning("Skipping malformed API product record: %s", exc)
    return products


@dataclass(frozen=True)
class CatalogSearchRequest:
    keywords: str = ""
    title: str = ""
    category_id: str = ""
    sort_by: str = "Relevance"
    max_pages: int = 10
    optional: bool = False


@dataclass(frozen=True)
class CatalogSearchResult:
    pages: tuple[tuple[list[_product.Product], int, int], ...]
    error: Exception | None = None


@dataclass(frozen=True)
class SeriesProductsBatch:
    products: dict[str, tuple[_product.Product, ...]]
    failures: dict[str, Exception]
    missing_asins: dict[str, tuple[str, ...]]
    product_failures: dict[str, tuple[Exception, ...]] = field(default_factory=dict)


class DealsClient:
    """Audible API client for catalog browsing."""

    def __init__(self, auth_file: Path | None = None, locale: str = "us"):
        self.auth_file = _paths.auth_file() if auth_file is None else auth_file
        self.locale = locale
        self._auth_store = _AuthStore(self.auth_file, locale)
        self._transport = _AudibleTransport(self._auth_store)
        self._categories_cache: list[dict[str, str]] | None = None
        self._library_cache: set[str] | None = None
        logger.debug("DealsClient init locale=%s auth_file=%s", locale, self.auth_file)

    def login(self, username: str, password: str) -> None:
        """Interactive Audible login. Persists tokens to auth_file."""
        self._auth_store.login(username, password)

    def login_external(
        self,
        callback_url_file: Path | None = None,
        login_url_callback: Callable[[str], str] | None = None,
    ) -> None:
        """Login via external browser (for captcha/2FA). Persists tokens.

        Uses the audible package's login_url_callback parameter to control
        how the callback URL is collected. If callback_url_file is set,
        prints the OAuth URL, waits for the user to save the callback URL
        to that file, then reads it — avoiding the flaky input() prompt.
        """
        self._auth_store.login_external(callback_url_file, login_url_callback)

    def import_auth(self, source_path: Path) -> None:
        """Import auth from an audible-cli or Libation-exported JSON file."""
        self._auth_store.import_auth(source_path)

    @property
    def is_authenticated(self) -> bool:
        return self._auth_store.is_authenticated

    def check_connection(self) -> None:
        """Verify that the saved authentication can reach Audible."""
        self._transport.request("1.0/catalog/products", num_results=1)

    def close(self) -> None:
        self._transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextlib.contextmanager
    def _pool(self, *, abortable: bool = True) -> Iterator[ThreadPoolExecutor]:
        """A worker pool for concurrent requests, torn down on every exit path.

        With *abortable*, an error (or the caller closing a generator) first
        cancels requests that are retrying, and the transport is usable again
        afterwards. Either way the pool is drained with ``wait=True``:
        unwinding with reads still in flight lets the client get GC'd under
        them, stalling each until its 10s socket timeout.
        """
        pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FETCHES)
        try:
            yield pool
        except BaseException:
            if abortable:
                self._transport.cancel()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            if abortable:
                self._transport.reset_abort()

    def _settle(
        self, fn: Callable[[Any], Any], items: list[Any]
    ) -> list[tuple[Any, Any, Exception | None]]:
        """Run ``fn(item)`` for every item, concurrently when there are several.

        Returns ``(item, value, error)`` per item in input order; a failing item
        does not stop the others.
        """
        outcomes: list[tuple[Any, Any, Exception | None]] = []
        if len(items) <= 1:
            for item in items:
                try:
                    outcomes.append((item, fn(item), None))
                except Exception as exc:
                    outcomes.append((item, None, exc))
            return outcomes
        with self._pool(abortable=False) as pool:
            futures = [(item, pool.submit(fn, item)) for item in items]
            for item, future in futures:
                try:
                    outcomes.append((item, future.result(), None))
                except Exception as exc:
                    outcomes.append((item, None, exc))
        return outcomes

    def search_catalog(
        self,
        *,
        keywords: str = "",
        title: str = "",
        category_id: str = "",
        sort_by: str = "Relevance",
        num_results: int = _constants.MAX_PAGE_SIZE,
        page: int | None = None,
    ) -> tuple[list[_product.Product], int]:
        """Search the Audible catalog. Returns (products, total_results)."""
        if page is None:
            page = 0
        params: dict[str, Any] = {
            "num_results": min(num_results, _constants.MAX_PAGE_SIZE),
            "page": page,
            "products_sort_by": sort_by,
            "response_groups": _constants.CATALOG_RESPONSE_GROUPS,
        }
        if keywords:
            params["keywords"] = keywords
        if title:
            params["title"] = title
        if category_id:
            params["category_id"] = category_id

        resp = self._transport.request("1.0/catalog/products", **params)

        products = _parse_api_products(resp.get("products", []), self.locale)
        total = resp.get("total_results", len(products))

        return products, total

    def search_pages(
        self,
        *,
        keywords: str = "",
        title: str = "",
        category_id: str = "",
        sort_by: str = "Relevance",
        max_pages: int = 10,
    ) -> Iterator[tuple[list[_product.Product], int, int]]:
        """Yield (products, page_num, total) for each page of results.

        Logical page 1 is fetched first to learn the total (and to warm any
        auth token refresh on a single thread). Remaining pages are fetched
        concurrently and yielded in logical page order. Stops after an empty
        page, though later pages may already be in flight.
        """

        def fetch(page: int) -> tuple[list[_product.Product], int]:
            return self.search_catalog(
                keywords=keywords,
                title=title,
                category_id=category_id,
                sort_by=sort_by,
                page=page - 1,
            )

        products, total = fetch(1)
        yield products, 1, total

        last_page = page_count(total, max_pages)
        if not products or last_page < 2:
            return

        with self._pool() as pool:
            for page, (page_products, page_total) in prefetch_in_order(
                pool, fetch, range(2, last_page + 1)
            ):
                yield page_products, page, page_total
                if not page_products:
                    break

    def search_segments(
        self,
        requests: list[CatalogSearchRequest],
        page_callback: Callable[[int, list[_product.Product], int, int], None]
        | None = None,
    ) -> list[CatalogSearchResult]:
        """Fetch independent catalog segments through one four-worker pool."""
        if not requests:
            return []

        pages: list[dict[int, tuple[list[_product.Product], int]]] = [
            {} for _ in requests
        ]
        errors: list[Exception | None] = [None for _ in requests]
        next_callback_page = [1 for _ in requests]
        callback_stopped = [False for _ in requests]

        def fetch(index: int, logical_page: int):
            request = requests[index]
            api_page = logical_page - 1
            products, total = self.search_catalog(
                keywords=request.keywords,
                title=request.title,
                category_id=request.category_id,
                sort_by=request.sort_by,
                page=api_page,
            )
            return index, logical_page, products, total

        def store(index: int, logical_page: int, products, total: int) -> None:
            pages[index][logical_page] = (products, total)
            while not callback_stopped[index]:
                next_page = next_callback_page[index]
                page_result = pages[index].get(next_page)
                if page_result is None:
                    break
                page_products, page_total = page_result
                if page_callback is not None:
                    page_callback(index, page_products, next_page, page_total)
                next_callback_page[index] += 1
                if not page_products:
                    callback_stopped[index] = True

        def remaining_pages(index: int, products, total: int) -> range:
            if not products:
                return range(0)
            return range(2, page_count(total, requests[index].max_pages) + 1)

        try:
            index, logical_page, products, total = fetch(0, 1)
        except Exception as exc:
            if not requests[0].optional:
                raise
            errors[0] = exc
            store(0, 1, [], 0)
            initial_remaining = range(0)
        else:
            store(index, logical_page, products, total)
            initial_remaining = remaining_pages(0, products, total)

        pending = {}
        with self._pool() as pool:
            for index in range(1, len(requests)):
                future = pool.submit(fetch, index, 1)
                pending[future] = (index, 1)
            for logical_page in initial_remaining:
                future = pool.submit(fetch, 0, logical_page)
                pending[future] = (0, logical_page)

            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    request_index, requested_page = pending.pop(future)
                    try:
                        index, logical_page, products, total = future.result()
                    except Exception as exc:
                        if not requests[request_index].optional:
                            raise
                        errors[request_index] = exc
                        store(request_index, requested_page, [], 0)
                        continue
                    store(index, logical_page, products, total)
                    if logical_page == 1:
                        for next_page in remaining_pages(index, products, total):
                            next_future = pool.submit(fetch, index, next_page)
                            pending[next_future] = (index, next_page)

        results: list[CatalogSearchResult] = []
        for index, page_map in enumerate(pages):
            ordered = []
            for logical_page in sorted(page_map):
                products, total = page_map[logical_page]
                ordered.append((products, logical_page, total))
                if not products:
                    break
            results.append(CatalogSearchResult(tuple(ordered), errors[index]))
        return results

    def get_library_asins(self) -> set[str]:
        """Fetch all ASINs in the user's Audible library.

        Cached on the client instance so repeated calls don't re-fetch.
        """
        if self._library_cache is not None:
            return self._library_cache

        def fetch(page: int) -> tuple[list[str], int]:
            resp = self._transport.request(
                "1.0/library",
                num_results=1000,
                page=page,
                response_groups="product_attrs",
            )
            items = resp.get("items", [])
            return [item["asin"] for item in items if item.get("asin")], len(items)

        asins: set[str] = set()
        for _, page_asins in read_serially(fetch, 1, 1000):
            asins.update(page_asins)

        logger.debug("library asins fetched count=%d", len(asins))
        self._library_cache = asins
        return asins

    def get_library_pages(self) -> Iterator[tuple[list[_product.Product], int]]:
        """Yield (products, page_num) for each page of the user's library.

        Fetches the first page synchronously, then uses the reported result
        count to fetch remaining pages with up to four workers.
        """

        def fetch(page: int) -> _LibraryPage:
            resp = self._transport.request(
                "1.0/library",
                num_results=_constants.MAX_PAGE_SIZE,
                page=page,
                response_groups=_constants.CATALOG_RESPONSE_GROUPS,
            )
            items = resp.get("items", [])
            products = _parse_api_products(items, self.locale)
            item_count = len(items) if isinstance(items, list) else 0
            return products, item_count, resp.get("total_results")

        def fetch_serially(start_page: int):
            def read(page: int) -> tuple[list[_product.Product], int]:
                page_products, count, _ = fetch(page)
                return page_products, count

            for page, page_products in read_serially(
                read, start_page, _constants.MAX_PAGE_SIZE
            ):
                yield page_products, page

        products, item_count, total_results = fetch(1)
        yield products, 1
        if item_count < _constants.MAX_PAGE_SIZE:
            return

        if (
            isinstance(total_results, bool)
            or not isinstance(total_results, int)
            or total_results < item_count
        ):
            yield from fetch_serially(2)
            return

        last_page = (
            total_results + _constants.MAX_PAGE_SIZE - 1
        ) // _constants.MAX_PAGE_SIZE
        if last_page < 2:
            yield from fetch_serially(2)
            return
        if last_page == 2:
            products, item_count, _ = fetch(2)
            yield products, 2
            if item_count == _constants.MAX_PAGE_SIZE:
                yield from fetch_serially(3)
            return

        with self._pool() as pool:
            for page, (products, item_count, _) in prefetch_in_order(
                pool, fetch, range(2, last_page + 1), window=MAX_CONCURRENT_FETCHES
            ):
                yield products, page
                if item_count < _constants.MAX_PAGE_SIZE and page < last_page:
                    self._transport.cancel()
                    break

        if item_count == _constants.MAX_PAGE_SIZE:
            yield from fetch_serially(last_page + 1)

    def get_library(self) -> list[_product.Product]:
        """Fetch all products in the user's Audible library with full metadata.

        Delegates to get_library_pages for pagination.
        """
        all_products: list[_product.Product] = []
        for page_products, _ in self.get_library_pages():
            all_products.extend(page_products)
        return all_products

    def get_wishlist(self) -> list[_product.Product]:
        """Fetch the user's Audible account wishlist (all pages).

        The wishlist API uses 0-indexed pages and returns up to MAX_PAGE_SIZE
        products per page in the same format as the catalog.
        """

        def fetch(page: int) -> tuple[list[_product.Product], int]:
            resp = self._transport.request(
                "1.0/wishlist",
                num_results=_constants.MAX_PAGE_SIZE,
                page=page,
                response_groups=_constants.CATALOG_RESPONSE_GROUPS,
                sort_by="-DateAdded",
            )
            raw_products = resp.get("products", [])
            return _parse_api_products(raw_products, self.locale), len(raw_products)

        all_products: list[_product.Product] = []
        # The wishlist API counts pages from 0.
        for _, products in read_serially(fetch, 0, _constants.MAX_PAGE_SIZE):
            all_products.extend(products)
        logger.debug("wishlist fetched count=%d", len(all_products))
        return all_products

    def resolve_genre(self, query: str) -> tuple[str, str]:
        """Fuzzy-match a genre name to a category (id, name).

        Raises ValueError if there is no match or the name is ambiguous.
        """
        if self._categories_cache is None:
            self._categories_cache = self.get_categories()
        return _genres.match_genre(query, self._categories_cache)

    def get_category_name(self, category_id: str) -> str:
        """Look up a category's display name by ID."""
        _validate_category_id(category_id)
        try:
            resp = self._transport.request(f"1.0/catalog/categories/{category_id}")
            return resp.get("category", {}).get("name", category_id)
        except Exception:
            logger.warning(
                "get_category_name failed for %s", category_id, exc_info=True
            )
            return category_id

    def _load_categories_cache(self) -> list[dict[str, str]] | None:
        """Top-level categories from the disk cache, when fresh."""
        cached = _cache_store.get(
            _CATEGORIES_NAMESPACE,
            self.locale,
            max_age_s=_constants.CATEGORIES_CACHE_TTL,
        )
        if isinstance(cached, list) and all(isinstance(item, dict) for item in cached):
            return cached
        return None

    def _save_categories_cache(self, categories: list[dict[str, str]]) -> None:
        """Persist top-level categories to the disk cache."""
        _cache_store.put(_CATEGORIES_NAMESPACE, self.locale, categories)

    def get_categories(self, root: str = "") -> list[dict[str, str]]:
        """Get category listing. Returns list of {id, name} dicts.

        Top-level categories are cached to disk for 7 days to save API calls.
        """
        if root:
            _validate_category_id(root)
            # Subcategories: fetch children of a specific category
            resp = self._transport.request(f"1.0/catalog/categories/{root}")
            cat_data = resp.get("category", {})
            return [
                {"id": c.get("id", ""), "name": c.get("name", "")}
                for c in cat_data.get("children", [])
            ]
        else:
            cached = self._load_categories_cache()
            if cached:
                return cached
            # Top-level categories
            resp = self._transport.request(
                "1.0/catalog/categories",
                category_type="CategoriesTopLevel",
            )
            categories = [
                {"id": c.get("id", ""), "name": c.get("name", "")}
                for c in resp.get("categories", [])
            ]
            self._save_categories_cache(categories)
            return categories

    def get_product(self, asin: str) -> _product.Product:
        """Get detailed product info by ASIN."""
        results = self.get_products_batch([asin])
        if not results:
            raise ValueError(f"Product not found: {asin}")
        return results[0]

    def _get_series_child_asins(self, series_asin: str) -> list[str]:
        resp = self._transport.request(
            f"1.0/catalog/products/{series_asin}",
            response_groups="relationships",
        )
        product_data = resp.get("product")
        if not product_data:
            return []
        return [
            r["asin"]
            for r in product_data.get("relationships", [])
            if r.get("relationship_to_product") == "child" and r.get("asin")
        ]

    def get_series_products_many(self, series_asins: list[str]) -> SeriesProductsBatch:
        """Fetch multiple series with bounded relationship and product batches."""
        unique_series = list(dict.fromkeys(series_asins))
        children: dict[str, list[str]] = {}
        failures: dict[str, Exception] = {}
        for series_asin, child_asins, error in self._settle(
            self._get_series_child_asins, unique_series
        ):
            if error is not None:
                failures[series_asin] = error
            else:
                children[series_asin] = child_asins

        all_child_asins = list(
            dict.fromkeys(asin for values in children.values() for asin in values)
        )
        batches = [
            tuple(all_child_asins[i : i + _constants.MAX_PAGE_SIZE])
            for i in range(0, len(all_child_asins), _constants.MAX_PAGE_SIZE)
        ]
        fetched: list[_product.Product] = []
        failed_batches: list[tuple[tuple[str, ...], Exception]] = []
        for batch, batch_products, error in self._settle(
            lambda batch: self._fetch_products_batch(list(batch)), batches
        ):
            if error is not None:
                failed_batches.append((batch, error))
            else:
                fetched.extend(batch_products)

        by_asin = {product.asin: product for product in fetched}
        products: dict[str, tuple[_product.Product, ...]] = {}
        missing_asins: dict[str, tuple[str, ...]] = {}
        product_failures: dict[str, tuple[Exception, ...]] = {}
        for series_asin, child_asins in children.items():
            products[series_asin] = tuple(
                by_asin[asin] for asin in child_asins if asin in by_asin
            )
            failed_asins: set[str] = set()
            series_errors: list[Exception] = []
            for failed_batch, batch_error in failed_batches:
                affected = set(child_asins).intersection(failed_batch)
                if affected:
                    failed_asins.update(affected)
                    series_errors.append(batch_error)
            if series_errors:
                product_failures[series_asin] = tuple(series_errors)
            missing = tuple(
                asin
                for asin in child_asins
                if asin not in by_asin and asin not in failed_asins
            )
            if missing:
                missing_asins[series_asin] = missing
        return SeriesProductsBatch(
            products,
            failures,
            missing_asins,
            product_failures,
        )

    def _fetch_products_batch(self, batch: list[str]) -> list[_product.Product]:
        resp = self._transport.request(
            "1.0/catalog/products",
            asins=",".join(batch),
            num_results=len(batch),
            response_groups=_constants.CATALOG_RESPONSE_GROUPS,
        )
        results = []
        for parsed in _parse_api_products(resp.get("products", []), self.locale):
            results.append(parsed)
        return results

    def get_products_batch(self, asins: list[str]) -> list[_product.Product]:
        """Fetch multiple products in batches of up to 50, concurrently.

        Uses the plural catalog endpoint with comma-separated ASINs.
        Returns products in arbitrary order; missing ASINs are silently skipped.
        """
        batches = [
            asins[i : i + _constants.MAX_PAGE_SIZE]
            for i in range(0, len(asins), _constants.MAX_PAGE_SIZE)
        ]

        if len(batches) <= 1:
            product_batches = [self._fetch_products_batch(batch) for batch in batches]
        else:
            with self._pool() as pool:
                futures = [
                    pool.submit(self._fetch_products_batch, batch) for batch in batches
                ]
                product_batches = [future.result() for future in futures]

        results = [product for batch in product_batches for product in batch]
        logger.debug("get_products_batch in=%d out=%d", len(asins), len(results))
        return results
