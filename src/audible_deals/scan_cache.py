"""Short-lived cache of catalog responses (``cache.db``) and what a run learned from it.

A command that opts in builds one :class:`ScanCache` and hands it to the client,
which asks it before every catalog request and offers it every successful
response. Commands that must see live prices (``track run``, ``notify``,
``watch``, monitors, wishlist checks) never build one, so they always bypass
the cache. Errors are never stored: only a parsed, successful response is.

The same object keeps the accounting (how many pages came from the cache, how
old they were, which products were only ever seen in cached pages) so the
command can say so and keep stale prices out of the price history.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from audible_deals import cache_store, constants
from audible_deals.product import Product

logger = logging.getLogger(__name__)

CATALOG_ENDPOINT = "1.0/catalog/products"
DEFAULT_TTL_MINUTES = constants.CATALOG_CACHE_DEFAULT_TTL_MINUTES
MAX_TTL_MINUTES = constants.CATALOG_CACHE_MAX_TTL_MINUTES
LIBRARY_TTL_S = constants.LIBRARY_CACHE_TTL
LIBRARY_NAMESPACE = "library"
CATEGORY_NAME_NAMESPACE = "category_names"
CATEGORY_NAME_TTL_S = constants.CATEGORIES_CACHE_TTL

#: Bump when the stored shape changes; old entries then simply never match.
_FORMAT = 1
_KEEP_ENTRIES = 3000
_MIN_RETENTION_S = 24 * 3600


def ttl_minutes(config: Mapping[str, Any]) -> int:
    """The configured ``cache-ttl`` in minutes (0 turns the cache off)."""
    value = config.get("cache_ttl")
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_TTL_MINUTES
    return min(max(value, 0), MAX_TTL_MINUTES)


def catalog_products_params(
    *,
    keywords: str = "",
    title: str = "",
    category_id: str = "",
    sort_by: str = "Relevance",
    page: int = 0,
    num_results: int = constants.MAX_PAGE_SIZE,
) -> dict[str, Any]:
    """The exact query one catalog search page sends (and is cached under)."""
    params: dict[str, Any] = {
        "num_results": min(num_results, constants.MAX_PAGE_SIZE),
        "page": page,
        "products_sort_by": sort_by,
        "response_groups": constants.CATALOG_RESPONSE_GROUPS,
    }
    if keywords:
        params["keywords"] = keywords
    if title:
        params["title"] = title
    if category_id:
        params["category_id"] = category_id
    return params


def entry_key(endpoint: str, params: Mapping[str, Any]) -> str:
    """A stable key for an endpoint and its normalized parameters."""
    normalized = json.dumps(
        [_FORMAT, endpoint, sorted(params.items())],
        ensure_ascii=False,
        default=str,
    )
    return hashlib.blake2b(normalized.encode("utf-8"), digest_size=16).hexdigest()


def pack_products(products: Iterable[Product]) -> list[dict[str, Any]]:
    return [dataclasses.asdict(product) for product in products]


_PRODUCT_FIELDS = frozenset(field.name for field in dataclasses.fields(Product))


def unpack_products(rows: object) -> list[Product]:
    """Rebuild products from :func:`pack_products` output (``ValueError`` if malformed)."""
    if not isinstance(rows, list):
        raise ValueError("cached products are not a list")
    products = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("cached product is not an object")
        try:
            products.append(
                Product(**{k: v for k, v in row.items() if k in _PRODUCT_FIELDS})
            )
        except TypeError as exc:
            raise ValueError(f"cached product is malformed: {exc}") from exc
    return products


@dataclass(frozen=True)
class CacheUse:
    """What a run took from the cache."""

    pages_cached: int = 0
    pages_live: int = 0
    #: Age of the oldest cached page used, in seconds.
    oldest_age_s: float | None = None
    #: Age of the cached list of owned titles, if one was used.
    library_age_s: float | None = None

    @property
    def any(self) -> bool:
        return self.pages_cached > 0 or self.library_age_s is not None


class ScanCache:
    """The catalog cache policy for one command run, with its accounting."""

    def __init__(self, ttl_s: float, *, refresh: bool = False):
        self.ttl_s = ttl_s
        self.refresh = refresh
        self._lock = threading.Lock()
        self._pages_cached = 0
        self._pages_live = 0
        self._oldest_age_s: float | None = None
        self._library_age_s: float | None = None
        self._cached_asins: set[str] = set()
        self._live_asins: set[str] = set()
        self._pruned = False

    @classmethod
    def from_config(
        cls, config: Mapping[str, Any], *, refresh: bool = False
    ) -> ScanCache | None:
        """The policy for a command run, or ``None`` when ``cache-ttl`` is 0."""
        minutes = ttl_minutes(config)
        return cls(minutes * 60, refresh=refresh) if minutes > 0 else None

    # -- catalog pages ------------------------------------------------------

    @staticmethod
    def _namespace(locale: str) -> str:
        return f"catalog:{locale}"

    def lookup(
        self, locale: str, endpoint: str, params: Mapping[str, Any]
    ) -> tuple[Any, float] | None:
        """``(value, age in seconds)`` for a fresh entry; ``None`` on a miss or ``--refresh``."""
        if self.refresh:
            return None
        return cache_store.get_entry(
            self._namespace(locale),
            entry_key(endpoint, params),
            max_age_s=self.ttl_s,
        )

    def store(
        self, locale: str, endpoint: str, params: Mapping[str, Any], value: Any
    ) -> None:
        cache_store.put(self._namespace(locale), entry_key(endpoint, params), value)
        with self._lock:
            first = not self._pruned
            self._pruned = True
        if first:
            cache_store.prune(
                "catalog:",
                max_age_s=max(self.ttl_s, _MIN_RETENTION_S),
                keep=_KEEP_ENTRIES,
            )

    def note_page(self, products: Iterable[Product], age_s: float | None) -> None:
        """Record where a page of products came from (``age_s`` is ``None`` when live)."""
        asins = {product.asin for product in products}
        with self._lock:
            if age_s is None:
                self._pages_live += 1
                self._live_asins |= asins
                return
            self._pages_cached += 1
            self._cached_asins |= asins
            if self._oldest_age_s is None or age_s > self._oldest_age_s:
                self._oldest_age_s = age_s

    def cached_asins(self) -> frozenset[str]:
        """Products whose price only ever came from a cached page in this run."""
        with self._lock:
            return frozenset(self._cached_asins)

    def use(self) -> CacheUse:
        with self._lock:
            return CacheUse(
                self._pages_cached,
                self._pages_live,
                self._oldest_age_s,
                self._library_age_s,
            )

    # -- owned titles -------------------------------------------------------

    def library_lookup(self, locale: str) -> set[str] | None:
        """The cached ASINs of the user's library (24 h), unless refreshing."""
        if self.refresh:
            return None
        entry = cache_store.get_entry(
            LIBRARY_NAMESPACE, locale, max_age_s=LIBRARY_TTL_S
        )
        if entry is None:
            return None
        value, age = entry
        if not isinstance(value, list) or not all(isinstance(a, str) for a in value):
            return None
        with self._lock:
            self._library_age_s = age
        return set(value)

    def library_store(self, locale: str, asins: Iterable[str]) -> None:
        cache_store.put(LIBRARY_NAMESPACE, locale, sorted(asins))


def category_name_lookup(locale: str, category_id: str) -> str | None:
    value = cache_store.get(
        CATEGORY_NAME_NAMESPACE,
        f"{locale}:{category_id}",
        max_age_s=CATEGORY_NAME_TTL_S,
    )
    return value if isinstance(value, str) and value else None


def category_name_store(locale: str, category_id: str, name: str) -> None:
    cache_store.put(CATEGORY_NAME_NAMESPACE, f"{locale}:{category_id}", name)
