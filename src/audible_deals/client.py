"""Domain facade for catalog browsing and deal discovery."""

from __future__ import annotations

import difflib
import json
import logging
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterator

from audible_deals import constants as _constants
from audible_deals import product as _product
from audible_deals.audible_transport import AudibleTransport as _AudibleTransport
from audible_deals.auth_store import AuthStore as _AuthStore
from audible_deals.storage import _atomic_write

logger = logging.getLogger(__name__)


_CATEGORY_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,30}$")


def _validate_category_id(value: str) -> None:
    """Validate category ID to prevent URL path injection."""
    if not _CATEGORY_ID_RE.fullmatch(value):
        raise ValueError(f"Invalid category ID format: {value!r}")


_MAX_CONCURRENT_FETCHES = 4


class DealsClient:
    """Audible API client for catalog browsing."""

    def __init__(self, auth_file: Path | None = None, locale: str = "us"):
        self.auth_file = _constants.AUTH_FILE if auth_file is None else auth_file
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
            # Audible's title-only catalog filter is zero-indexed even though
            # keyword and browse searches use one-indexed pages.
            page = 0 if title else 1
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

        products = [
            _product.parse_product(p, locale=self.locale)
            for p in resp.get("products", [])
        ]
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
        auth token refresh on a single thread); title-only searches map it to
        API page 0. Remaining pages are fetched concurrently and yielded in
        logical page order. Stops after an empty page, though later pages may
        already be in flight.
        """
        first_api_page = 0 if title else 1
        products, total = self.search_catalog(
            keywords=keywords,
            title=title,
            category_id=category_id,
            sort_by=sort_by,
            page=first_api_page,
        )
        yield products, 1, total

        last_page = (
            min(max_pages, math.ceil(total / _constants.MAX_PAGE_SIZE)) if total else 1
        )
        if not products or last_page < 2:
            return

        pool = ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_FETCHES)
        try:
            futures = {
                page: pool.submit(
                    self.search_catalog,
                    keywords=keywords,
                    title=title,
                    category_id=category_id,
                    sort_by=sort_by,
                    page=page - 1 if title else page,
                )
                for page in range(2, last_page + 1)
            }
            for page in range(2, last_page + 1):
                page_products, page_total = futures[page].result()
                yield page_products, page, page_total
                if not page_products:
                    break
        except BaseException:
            self._transport.cancel()
            raise
        finally:
            # wait=True: unwinding with reads still in flight lets the client
            # get GC'd under them, stalling each until its 10s socket timeout.
            pool.shutdown(wait=True, cancel_futures=True)
            self._transport.reset_abort()

    def get_library_asins(self) -> set[str]:
        """Fetch all ASINs in the user's Audible library.

        Cached on the client instance so repeated calls don't re-fetch.
        """
        if self._library_cache is not None:
            return self._library_cache

        asins: set[str] = set()
        page = 1
        while True:
            resp = self._transport.request(
                "1.0/library",
                num_results=1000,
                page=page,
                response_groups="product_attrs",
            )
            items = resp.get("items", [])
            for item in items:
                asin = item.get("asin", "")
                if asin:
                    asins.add(asin)
            if len(items) < 1000:
                break
            page += 1

        logger.debug("library asins fetched count=%d", len(asins))
        self._library_cache = asins
        return asins

    def get_library_pages(self) -> Iterator[tuple[list[_product.Product], int]]:
        """Yield (products, page_num) for each page of the user's library.

        Paginates through the library endpoint using MAX_PAGE_SIZE per page
        and the same response groups as catalog queries.
        """
        page = 1  # library API uses 1-indexed pages
        while True:
            resp = self._transport.request(
                "1.0/library",
                num_results=_constants.MAX_PAGE_SIZE,
                page=page,
                response_groups=_constants.CATALOG_RESPONSE_GROUPS,
            )
            items = resp.get("items", [])
            products = [
                _product.parse_product(raw, locale=self.locale)
                for raw in items
                if raw.get("asin") and raw.get("title")
            ]
            yield products, page
            if len(items) < _constants.MAX_PAGE_SIZE:
                break
            page += 1

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
        all_products: list[_product.Product] = []
        page = 0  # wishlist API uses 0-indexed pages
        while True:
            resp = self._transport.request(
                "1.0/wishlist",
                num_results=_constants.MAX_PAGE_SIZE,
                page=page,
                response_groups=_constants.CATALOG_RESPONSE_GROUPS,
                sort_by="-DateAdded",
            )
            raw_products = resp.get("products", [])
            products = [
                _product.parse_product(p, locale=self.locale)
                for p in raw_products
                if p.get("asin") and p.get("title")
            ]
            all_products.extend(products)
            if len(raw_products) < _constants.MAX_PAGE_SIZE:
                break
            page += 1
        logger.debug("wishlist fetched count=%d", len(all_products))
        return all_products

    def resolve_genre(self, query: str) -> tuple[str, str]:
        """Fuzzy-match a genre name to a category (id, name).

        Tries alias expansion, exact match, substring, then difflib.
        Raises ValueError if no match or ambiguous.
        """
        if self._categories_cache is None:
            self._categories_cache = self.get_categories()

        cats = self._categories_cache
        names = [c["name"] for c in cats]
        names_lower = [n.lower() for n in names]

        # Normalize and expand aliases
        q_raw = query.strip().lower()
        q = _constants.GENRE_ALIASES.get(q_raw, q_raw)
        if q != q_raw:
            logger.debug("resolve_genre alias %r -> %r", q_raw, q)

        # Exact match
        if q in names_lower:
            idx = names_lower.index(q)
            logger.debug(
                "resolve_genre exact match: %r -> %s", query, cats[idx]["name"]
            )
            return cats[idx]["id"], cats[idx]["name"]

        # Substring match
        matches = [i for i, n in enumerate(names_lower) if q in n]
        if len(matches) == 1:
            logger.debug(
                "resolve_genre substring match: %r -> %s",
                query,
                cats[matches[0]]["name"],
            )
            return cats[matches[0]]["id"], cats[matches[0]]["name"]
        if len(matches) > 1:
            options = ", ".join(names[i] for i in matches)
            raise ValueError(
                f'Ambiguous genre "{query}" matches: {options}\n'
                "Use a more specific name or --category ID."
            )

        # Fuzzy match via difflib
        close = difflib.get_close_matches(q, names_lower, n=1, cutoff=0.5)
        if close:
            idx = names_lower.index(close[0])
            logger.debug(
                "resolve_genre fuzzy match: %r -> %s", query, cats[idx]["name"]
            )
            return cats[idx]["id"], cats[idx]["name"]

        available = ", ".join(names)
        raise ValueError(f'No genre matching "{query}".\nAvailable: {available}')

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
        """Load top-level categories from disk cache if fresh."""
        cache_file = _constants.CATEGORIES_CACHE_FILE.with_suffix(
            f".{self.locale}.json"
        )
        if not cache_file.exists():
            logger.debug("categories cache miss (no file): %s", cache_file)
            return None
        try:
            data = json.loads(cache_file.read_text())
            if not isinstance(data, dict):
                raise ValueError("categories cache is not a dict")
            age = time.time() - data.get("ts", 0)
            if age < _constants.CATEGORIES_CACHE_TTL:
                logger.debug(
                    "categories cache hit (%s, age=%.0fs, %d items)",
                    cache_file,
                    age,
                    len(data.get("categories", [])),
                )
                return data["categories"]
            logger.debug(
                "categories cache stale (age=%.0fs > %ds)",
                age,
                _constants.CATEGORIES_CACHE_TTL,
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            logger.warning("categories cache corrupt: %s", cache_file, exc_info=True)
        return None

    def _save_categories_cache(self, categories: list[dict[str, str]]) -> None:
        """Persist top-level categories to disk."""
        cache_file = _constants.CATEGORIES_CACHE_FILE.with_suffix(
            f".{self.locale}.json"
        )
        _atomic_write(
            cache_file, json.dumps({"ts": time.time(), "categories": categories})
        )
        logger.debug(
            "categories cache saved (%s, %d items)", cache_file, len(categories)
        )

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

    def get_series_products(self, series_asin: str) -> list[_product.Product]:
        """Fetch all products in a series by its ASIN.

        Returns an empty list if the series is not found.
        """
        try:
            resp = self._transport.request(
                f"1.0/catalog/products/{series_asin}",
                response_groups="relationships",
            )
        except Exception:
            logger.warning(
                "get_series_products failed for %s", series_asin, exc_info=True
            )
            return []
        product_data = resp.get("product")
        if not product_data:
            return []
        child_asins = [
            r["asin"]
            for r in product_data.get("relationships", [])
            if r.get("relationship_to_product") == "child" and r.get("asin")
        ]
        if not child_asins:
            return []
        return self.get_products_batch(child_asins)

    def get_products_batch(self, asins: list[str]) -> list[_product.Product]:
        """Fetch multiple products in batches of up to 50, concurrently.

        Uses the plural catalog endpoint with comma-separated ASINs.
        Returns products in arbitrary order; missing ASINs are silently skipped.
        """
        batches = [
            asins[i : i + _constants.MAX_PAGE_SIZE]
            for i in range(0, len(asins), _constants.MAX_PAGE_SIZE)
        ]

        def _fetch_batch(batch: list[str]) -> dict:
            return self._transport.request(
                "1.0/catalog/products",
                asins=",".join(batch),
                num_results=len(batch),
                response_groups=_constants.CATALOG_RESPONSE_GROUPS,
            )

        if len(batches) <= 1:
            resps = [_fetch_batch(batch) for batch in batches]
        else:
            pool = ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_FETCHES)
            try:
                futures = [pool.submit(_fetch_batch, batch) for batch in batches]
                resps = [f.result() for f in futures]
            except BaseException:
                self._transport.cancel()
                raise
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
                self._transport.reset_abort()

        results: list[_product.Product] = []
        for resp in resps:
            for raw in resp.get("products", []):
                parsed = _product.parse_product(raw, locale=self.locale)
                if parsed.asin and parsed.title:
                    results.append(parsed)
        logger.debug("get_products_batch in=%d out=%d", len(asins), len(results))
        return results
