"""Audible API client for catalog browsing and deal discovery."""

from __future__ import annotations

import contextlib
import difflib
import json
import logging
import math
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

import click

if TYPE_CHECKING:
    import audible

from audible_deals.constants import (
    AUTH_FILE,
    CATALOG_RESPONSE_GROUPS,
    CATEGORIES_CACHE_FILE,
    CATEGORIES_CACHE_TTL,
    GENRE_ALIASES,
    LOCALE_DOMAIN,
    MAX_PAGE_SIZE,
)
from audible_deals.storage import _atomic_write
from audible_deals.product import (  # noqa: F401 — re-exported for back-compat
    Product,
    _base_price,
    _extract_categories,
    _extract_plus,
    _extract_prices,
    _extract_rating,
    _extract_series,
    parse_product,
)

logger = logging.getLogger(__name__)


_CATEGORY_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,30}$")

_LOG_PARAM_KEYS = (
    "page",
    "num_results",
    "products_sort_by",
    "category_id",
    "asins",
    "sort_by",
)


def _log_request_params(params: dict[str, Any]) -> dict[str, Any]:
    """Subset of request params safe & useful to log (no full response_groups)."""
    snapshot: dict[str, Any] = {k: params[k] for k in _LOG_PARAM_KEYS if k in params}
    kw = params.get("keywords")
    if kw:
        snapshot["keywords"] = kw if len(kw) <= 80 else kw[:77] + "..."
    return snapshot


def _validate_category_id(value: str) -> None:
    """Validate category ID to prevent URL path injection."""
    if not _CATEGORY_ID_RE.fullmatch(value):
        raise ValueError(f"Invalid category ID format: {value!r}")


@contextlib.contextmanager
def _restrictive_umask():
    """Temporarily set umask to 0o177 so new files are created at 0o600.

    umask is process-global; only used during login/import, which never
    run concurrently with fetch worker threads.
    """
    old = os.umask(0o177)
    try:
        yield
    finally:
        os.umask(old)


_RETRY_DELAYS = (1.0, 2.0)

# Modest fan-out: enough to hide round-trip latency without tripping
# Audible's rate limiting (429s back off per-thread in _api_get).
_MAX_CONCURRENT_FETCHES = 4


def _retryable_status(exc: Exception) -> int | None:
    """Pull an HTTP status code off an exception or its response, if present."""
    return getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )


def _retry_delay(attempt: int, exc: Exception, status: int | None) -> float:
    """Jittered backoff delay for a retry, honoring a 429 Retry-After header."""
    delay = max(0.0, _RETRY_DELAYS[attempt - 1] + random.uniform(-0.3, 0.3))
    if status == 429:
        resp = getattr(exc, "response", None)
        headers = getattr(resp, "headers", None)
        retry_after = headers.get("Retry-After", "") if headers else ""
        if isinstance(retry_after, str) and retry_after.isdigit():
            delay = max(delay, min(int(retry_after), 120))
    return delay


def _auth_from_libation(data: dict, locale: str) -> dict:
    """Build Mkb79Auth-format auth data from Libation's AccountsSettings.json."""
    accounts = data["Accounts"]
    if not accounts:
        raise ValueError("No accounts found in Libation settings")
    tokens = accounts[0].get("IdentityTokens", {})
    for key in ("access_token", "refresh_token"):
        if not isinstance(tokens.get(key), str) or not tokens[key]:
            raise ValueError(f"Libation auth missing required key: {key!r}")
    return {
        "website_cookies": tokens.get("website_cookies"),
        "adp_token": tokens.get("adp_token"),
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "device_private_key": tokens.get("device_private_key"),
        "store_authentication_cookie": tokens.get("store_authentication_cookie"),
        "device_info": tokens.get("device_info", {}),
        "customer_info": tokens.get("customer_info", {}),
        "expires": tokens.get("expires", 0),
        "locale_code": tokens.get("locale_code", locale),
        "with_username": tokens.get("with_username", False),
        "encryption": False,
    }


def _validate_audible_cli_auth(data: dict) -> dict:
    """Validate auth data already in audible-cli / Mkb79Auth format."""
    for key in ("access_token", "refresh_token"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"Auth file missing required key: {key!r}")
    if "locale_code" in data and data["locale_code"] not in LOCALE_DOMAIN:
        raise ValueError(
            f"Unknown locale_code: {data['locale_code']!r}. "
            f"Valid: {', '.join(sorted(LOCALE_DOMAIN))}"
        )
    if "encryption" not in data:
        data["encryption"] = False
    return data


class DealsClient:
    """Audible API client for catalog browsing."""

    def __init__(self, auth_file: Path = AUTH_FILE, locale: str = "us"):
        self.auth_file = auth_file
        self.locale = locale
        self._client: audible.Client | None = None
        self._categories_cache: list[dict[str, str]] | None = None
        self._library_cache: set[str] | None = None
        self._abort_fetch = threading.Event()
        logger.debug("DealsClient init locale=%s auth_file=%s", locale, auth_file)

    def _api_get(self, endpoint: str, **params: Any) -> dict:
        """Wrap self.client.get with timing + DEBUG logging + retry-with-backoff."""
        debug = logger.isEnabledFor(logging.DEBUG)
        for attempt in range(1, 4):
            if debug:
                logger.debug(
                    "API GET %s params=%s", endpoint, _log_request_params(params)
                )
                start = time.monotonic()
            try:
                resp = self.client.get(endpoint, **params)
            except Exception as exc:
                if isinstance(exc, click.ClickException):
                    raise
                status = _retryable_status(exc)
                if status is not None and 400 <= status < 500 and status != 429:
                    raise
                if attempt >= 3:
                    logger.warning(
                        "API GET %s failed (attempt %d/%d): %s; giving up",
                        endpoint,
                        attempt,
                        3,
                        exc,
                    )
                    raise
                delay = _retry_delay(attempt, exc, status)
                logger.warning(
                    "API GET %s failed (attempt %d/%d): %s; retrying in %.1fs",
                    endpoint,
                    attempt,
                    3,
                    exc,
                    delay,
                )
                # wait() doubles as an abort check: a scan being torn down
                # wakes sleeping retries immediately instead of letting them
                # run against a client that is about to go away.
                if self._abort_fetch.wait(delay):
                    raise
                continue
            if isinstance(resp, tuple):
                resp = resp[0]
            if debug:
                elapsed_ms = (time.monotonic() - start) * 1000
                items = 0
                if isinstance(resp, dict):
                    for key in ("products", "items", "categories"):
                        val = resp.get(key)
                        if isinstance(val, list):
                            items = len(val)
                            break
                total = resp.get("total_results") if isinstance(resp, dict) else None
                logger.debug(
                    "API GET %s done %.0fms items=%d total=%s",
                    endpoint,
                    elapsed_ms,
                    items,
                    total,
                )
            return resp

    def _prepare_auth_dir(self) -> None:
        """Create the auth directory with owner-only permissions."""
        self.auth_file.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.auth_file.parent, 0o700)

    def login(self, username: str, password: str) -> None:
        """Interactive Audible login. Persists tokens to auth_file."""
        import audible

        logger.info("login (interactive) locale=%s", self.locale)
        self._prepare_auth_dir()
        auth = audible.Authenticator.from_login(
            username,
            password,
            locale=self.locale,
            with_username=True,
        )
        with _restrictive_umask():
            auth.to_file(self.auth_file)
        os.chmod(self.auth_file, 0o600)
        logger.info("login complete, auth written to %s", self.auth_file)

    def login_external(self, callback_url_file: Path | None = None) -> None:
        """Login via external browser (for captcha/2FA). Persists tokens.

        Uses the audible package's login_url_callback parameter to control
        how the callback URL is collected. If callback_url_file is set,
        prints the OAuth URL, waits for the user to save the callback URL
        to that file, then reads it — avoiding the flaky input() prompt.
        """
        import audible

        logger.info(
            "login_external locale=%s via_file=%s",
            self.locale,
            callback_url_file,
        )
        self._prepare_auth_dir()

        if callback_url_file:

            def _file_callback(oauth_url: str) -> str:
                print()
                print("Open this URL in your browser and log in:")
                print()
                print(oauth_url)
                print()
                print(
                    "After login you'll see a 'Page not found' page. That's expected."
                )
                print(
                    "Copy the FULL URL from your browser's address bar "
                    f"and save it to:\n  {callback_url_file}"
                )
                print()
                input("Press Enter here once the file is saved...")
                url = callback_url_file.read_text().strip()
                if not url:
                    raise RuntimeError(f"File is empty: {callback_url_file}")
                return url

            auth = audible.Authenticator.from_login_external(
                locale=self.locale,
                login_url_callback=_file_callback,
            )
        else:
            auth = audible.Authenticator.from_login_external(
                locale=self.locale,
            )

        with _restrictive_umask():
            auth.to_file(self.auth_file)
        os.chmod(self.auth_file, 0o600)
        logger.info("login_external complete, auth written to %s", self.auth_file)

    def import_auth(self, source_path: Path) -> None:
        """Import auth from an audible-cli or Libation-exported JSON file."""
        logger.info("import_auth from %s", source_path)
        self._prepare_auth_dir()

        raw = source_path.read_text()
        if len(raw) > 1_000_000:
            raise ValueError(
                f"Auth file too large ({len(raw):,} chars). "
                "Expected a small JSON credentials file."
            )

        data = json.loads(raw)

        # Libation's AccountsSettings.json wraps tokens in an Accounts array.
        if "Accounts" in data:
            auth_data = _auth_from_libation(data, self.locale)
            source_format = "Libation"
        else:
            auth_data = _validate_audible_cli_auth(data)
            source_format = "audible-cli"

        _atomic_write(self.auth_file, json.dumps(auth_data, indent=2))
        os.chmod(self.auth_file, 0o600)
        logger.info(
            "import_auth (%s format) written to %s", source_format, self.auth_file
        )

    @property
    def is_authenticated(self) -> bool:
        return self.auth_file.exists()

    @property
    def client(self) -> audible.Client:
        import audible

        if self._client is None:
            if not self.auth_file.exists():
                raise RuntimeError("Not authenticated. Run 'deals login' first.")
            auth = audible.Authenticator.from_file(self.auth_file)
            self._client = audible.Client(auth=auth)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def search_catalog(
        self,
        *,
        keywords: str = "",
        category_id: str = "",
        sort_by: str = "Price",
        num_results: int = MAX_PAGE_SIZE,
        page: int = 1,
    ) -> tuple[list[Product], int]:
        """Search the Audible catalog. Returns (products, total_results)."""
        params: dict[str, Any] = {
            "num_results": min(num_results, MAX_PAGE_SIZE),
            "page": page,
            "products_sort_by": sort_by,
            "response_groups": CATALOG_RESPONSE_GROUPS,
        }
        if keywords:
            params["keywords"] = keywords
        if category_id:
            params["category_id"] = category_id

        resp = self._api_get("1.0/catalog/products", **params)

        products = [
            parse_product(p, locale=self.locale) for p in resp.get("products", [])
        ]
        total = resp.get("total_results", len(products))

        return products, total

    def search_pages(
        self,
        *,
        keywords: str = "",
        category_id: str = "",
        sort_by: str = "Price",
        max_pages: int = 10,
    ) -> Iterator[tuple[list[Product], int, int]]:
        """Yield (products, page_num, total) for each page of results.

        Page 1 is fetched first to learn the total (and to warm any auth
        token refresh on a single thread); the remaining pages are fetched
        concurrently and yielded in page order. Stops yielding after an
        empty page, though later pages may already be in flight.
        """
        products, total = self.search_catalog(
            keywords=keywords,
            category_id=category_id,
            sort_by=sort_by,
            page=1,
        )
        yield products, 1, total

        last_page = min(max_pages, math.ceil(total / MAX_PAGE_SIZE)) if total else 1
        if not products or last_page < 2:
            return

        pool = ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_FETCHES)
        try:
            futures = {
                page: pool.submit(
                    self.search_catalog,
                    keywords=keywords,
                    category_id=category_id,
                    sort_by=sort_by,
                    page=page,
                )
                for page in range(2, last_page + 1)
            }
            for page in range(2, last_page + 1):
                page_products, page_total = futures[page].result()
                yield page_products, page, page_total
                if not page_products:
                    break
        except BaseException:
            self._abort_fetch.set()
            raise
        finally:
            # wait=True: unwinding with reads still in flight lets the client
            # get GC'd under them, stalling each until its 10s socket timeout.
            pool.shutdown(wait=True, cancel_futures=True)
            self._abort_fetch.clear()

    def get_library_asins(self) -> set[str]:
        """Fetch all ASINs in the user's Audible library.

        Cached on the client instance so repeated calls don't re-fetch.
        """
        if self._library_cache is not None:
            return self._library_cache

        asins: set[str] = set()
        page = 1
        while True:
            resp = self._api_get(
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

    def get_library_pages(self) -> Iterator[tuple[list[Product], int]]:
        """Yield (products, page_num) for each page of the user's library.

        Paginates through the library endpoint using MAX_PAGE_SIZE per page
        and the same response groups as catalog queries.
        """
        page = 1  # library API uses 1-indexed pages
        while True:
            resp = self._api_get(
                "1.0/library",
                num_results=MAX_PAGE_SIZE,
                page=page,
                response_groups=CATALOG_RESPONSE_GROUPS,
            )
            items = resp.get("items", [])
            products = [
                parse_product(raw, locale=self.locale)
                for raw in items
                if raw.get("asin") and raw.get("title")
            ]
            yield products, page
            if len(items) < MAX_PAGE_SIZE:
                break
            page += 1

    def get_library(self) -> list[Product]:
        """Fetch all products in the user's Audible library with full metadata.

        Delegates to get_library_pages for pagination.
        """
        all_products: list[Product] = []
        for page_products, _ in self.get_library_pages():
            all_products.extend(page_products)
        return all_products

    def get_wishlist(self) -> list[Product]:
        """Fetch the user's Audible account wishlist (all pages).

        The wishlist API uses 0-indexed pages and returns up to MAX_PAGE_SIZE
        products per page in the same format as the catalog.
        """
        all_products: list[Product] = []
        page = 0  # wishlist API uses 0-indexed pages
        while True:
            resp = self._api_get(
                "1.0/wishlist",
                num_results=MAX_PAGE_SIZE,
                page=page,
                response_groups=CATALOG_RESPONSE_GROUPS,
                sort_by="-DateAdded",
            )
            products = [
                parse_product(p, locale=self.locale) for p in resp.get("products", [])
            ]
            all_products.extend(products)
            if len(products) < MAX_PAGE_SIZE:
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
        q = GENRE_ALIASES.get(q_raw, q_raw)
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
            resp = self._api_get(f"1.0/catalog/categories/{category_id}")
            return resp.get("category", {}).get("name", category_id)
        except Exception:
            logger.warning(
                "get_category_name failed for %s", category_id, exc_info=True
            )
            return category_id

    def _load_categories_cache(self) -> list[dict[str, str]] | None:
        """Load top-level categories from disk cache if fresh."""
        cache_file = CATEGORIES_CACHE_FILE.with_suffix(f".{self.locale}.json")
        if not cache_file.exists():
            logger.debug("categories cache miss (no file): %s", cache_file)
            return None
        try:
            data = json.loads(cache_file.read_text())
            age = time.time() - data.get("ts", 0)
            if age < CATEGORIES_CACHE_TTL:
                logger.debug(
                    "categories cache hit (%s, age=%.0fs, %d items)",
                    cache_file,
                    age,
                    len(data.get("categories", [])),
                )
                return data["categories"]
            logger.debug(
                "categories cache stale (age=%.0fs > %ds)", age, CATEGORIES_CACHE_TTL
            )
        except (json.JSONDecodeError, KeyError):
            logger.warning("categories cache corrupt: %s", cache_file, exc_info=True)
        return None

    def _save_categories_cache(self, categories: list[dict[str, str]]) -> None:
        """Persist top-level categories to disk."""
        cache_file = CATEGORIES_CACHE_FILE.with_suffix(f".{self.locale}.json")
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
            resp = self._api_get(f"1.0/catalog/categories/{root}")
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
            resp = self._api_get(
                "1.0/catalog/categories",
                category_type="CategoriesTopLevel",
            )
            categories = [
                {"id": c.get("id", ""), "name": c.get("name", "")}
                for c in resp.get("categories", [])
            ]
            self._save_categories_cache(categories)
            return categories

    def get_product(self, asin: str) -> Product:
        """Get detailed product info by ASIN."""
        results = self.get_products_batch([asin])
        if not results:
            raise ValueError(f"Product not found: {asin}")
        return results[0]

    def get_series_products(self, series_asin: str) -> list[Product]:
        """Fetch all products in a series by its ASIN.

        Returns an empty list if the series is not found.
        """
        try:
            resp = self._api_get(
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

    def get_products_batch(self, asins: list[str]) -> list[Product]:
        """Fetch multiple products in batches of up to 50.

        Uses the plural catalog endpoint with comma-separated ASINs.
        Returns products in arbitrary order; missing ASINs are silently skipped.
        """
        results: list[Product] = []
        for i in range(0, len(asins), MAX_PAGE_SIZE):
            batch = asins[i : i + MAX_PAGE_SIZE]
            resp = self._api_get(
                "1.0/catalog/products",
                asins=",".join(batch),
                num_results=len(batch),
                response_groups=CATALOG_RESPONSE_GROUPS,
            )
            for raw in resp.get("products", []):
                product = parse_product(raw, locale=self.locale)
                if product.asin and product.title:
                    results.append(product)
        logger.debug("get_products_batch in=%d out=%d", len(asins), len(results))
        return results
