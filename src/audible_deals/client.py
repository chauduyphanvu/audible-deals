"""Audible API client for catalog browsing and deal discovery."""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import audible

# Response groups for catalog queries — comprehensive set for full product data.
CATALOG_RESPONSE_GROUPS = ",".join([
    "product_attrs",
    "product_desc",
    "contributors",
    "rating",
    "media",
    "category_ladders",
    "series",
    "product_plan_details",
    "product_plans",
    "price",
])

# Locale → currency symbol and Audible domain
LOCALE_CURRENCY: dict[str, str] = {
    "us": "$", "uk": "£", "ca": "CA$", "au": "A$",
    "in": "₹", "de": "€", "fr": "€", "jp": "¥", "es": "€",
}
LOCALE_DOMAIN: dict[str, str] = {
    "us": "www.audible.com", "uk": "www.audible.co.uk",
    "ca": "www.audible.ca", "au": "www.audible.com.au",
    "in": "www.audible.in", "de": "www.audible.de",
    "fr": "www.audible.fr", "jp": "www.audible.co.jp",
    "es": "www.audible.es",
}

CONFIG_DIR = Path.home() / ".config" / "audible-deals"
AUTH_FILE = CONFIG_DIR / "auth.json"
CATEGORIES_CACHE_FILE = CONFIG_DIR / "categories_cache.json"
CATEGORIES_CACHE_TTL = 86400 * 7  # 7 days


def _atomic_write_simple(path: Path, content: str) -> None:
    """Write content to path atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


_CATEGORY_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,30}$")


def _validate_category_id(value: str) -> None:
    """Validate category ID to prevent URL path injection."""
    if not _CATEGORY_ID_RE.fullmatch(value):
        raise ValueError(f"Invalid category ID format: {value!r}")


@contextlib.contextmanager
def _restrictive_umask():
    """Temporarily set umask to 0o177 so new files are created at 0o600.

    umask is process-global; safe for this single-threaded CLI.
    """
    old = os.umask(0o177)
    try:
        yield
    finally:
        os.umask(old)


MAX_PAGE_SIZE = 50

# Common genre abbreviations for fuzzy matching
GENRE_ALIASES: dict[str, str] = {
    "sci-fi": "science fiction",
    "scifi": "science fiction",
    "sf": "science fiction",
    "fantasy": "science fiction & fantasy",
    "mystery": "mystery, thriller & suspense",
    "thriller": "mystery, thriller & suspense",
    "suspense": "mystery, thriller & suspense",
    "bio": "biographies & memoirs",
    "memoir": "biographies & memoirs",
    "memoirs": "biographies & memoirs",
    "ya": "teen & young adult",
    "young adult": "teen & young adult",
    "kids": "children's audiobooks",
    "children": "children's audiobooks",
    "biz": "business & careers",
    "business": "business & careers",
    "self-help": "relationships, parenting & personal development",
    "selfhelp": "relationships, parenting & personal development",
    "history": "history",
    "romance": "romance",
    "erotica": "erotica",
    "comedy": "comedy & humor",
    "humor": "comedy & humor",
    "tech": "computers & technology",
    "science": "science & engineering",
    "religion": "religion & spirituality",
    "politics": "politics & social sciences",
    "sports": "sports & outdoors",
    "finance": "money & finance",
    "money": "money & finance",
    "lgbtq": "lgbtq+",
    "health": "health & wellness",
    "fiction": "literature & fiction",
    "lit": "literature & fiction",
}


@dataclass
class Product:
    """Audiobook product from Audible catalog."""

    asin: str
    title: str
    subtitle: str = ""
    authors: list[str] = field(default_factory=list)
    narrators: list[str] = field(default_factory=list)
    publisher: str = ""
    price: float | None = None
    list_price: float | None = None
    length_minutes: int = 0
    rating: float = 0.0
    num_ratings: int = 0
    categories: list[str] = field(default_factory=list)
    category_ids: list[str] = field(default_factory=list)
    series_name: str = ""
    series_position: str = ""
    language: str = ""
    release_date: str = ""
    in_plus_catalog: bool = False
    locale: str = "us"

    @property
    def full_title(self) -> str:
        if self.subtitle:
            return f"{self.title}: {self.subtitle}"
        return self.title

    @property
    def hours(self) -> float:
        return round(self.length_minutes / 60, 1) if self.length_minutes else 0.0

    @property
    def discount_pct(self) -> int | None:
        if self.price is not None and self.list_price and self.list_price > 0:
            return round((1 - self.price / self.list_price) * 100)
        return None

    @property
    def authors_str(self) -> str:
        return ", ".join(self.authors[:3])

    @property
    def narrators_str(self) -> str:
        return ", ".join(self.narrators[:2])

    @property
    def currency(self) -> str:
        return LOCALE_CURRENCY.get(self.locale, "$")

    @property
    def url(self) -> str:
        domain = LOCALE_DOMAIN.get(self.locale, "www.audible.com")
        return f"https://{domain}/pd/{self.asin}"


def parse_product(raw: dict[str, Any], locale: str = "us") -> Product:
    """Parse a raw API product dict into a Product.

    Handles the nested response format from Audible's catalog API.
    """
    price = _extract_price(raw)
    list_price = _extract_list_price(raw)

    authors = [a.get("name", "") for a in (raw.get("authors") or []) if a.get("name")]
    narrators = [n.get("name", "") for n in (raw.get("narrators") or []) if n.get("name")]

    # Rating - nested in overall_distribution
    rating_data = raw.get("rating") or {}
    rating = 0.0
    num_ratings = 0
    if isinstance(rating_data, dict):
        dist = rating_data.get("overall_distribution") or {}
        try:
            rating = float(dist.get("display_average_rating", 0) or 0)
        except (ValueError, TypeError):
            pass
        try:
            num_ratings = int(dist.get("num_ratings", 0) or 0)
        except (ValueError, TypeError):
            pass

    # Categories - flatten ladder structure
    categories: list[str] = []
    category_ids: list[str] = []
    for ladder in (raw.get("category_ladders") or []):
        for cat in (ladder.get("ladder") or []):
            name = cat.get("name", "")
            cid = cat.get("id", "")
            if name and name not in categories:
                categories.append(name)
            if cid and cid not in category_ids:
                category_ids.append(cid)

    # Series info
    series_name = ""
    series_position = ""
    series_list = raw.get("series") or []
    if series_list:
        s = series_list[0]
        series_name = s.get("title", "")
        series_position = s.get("sequence", "")

    # Audible Plus detection
    in_plus = False
    for plan in (raw.get("plans") or []):
        pname = plan.get("plan_name", "")
        if "Plus" in pname or "AYCE" in pname:
            in_plus = True
            break

    return Product(
        asin=raw.get("asin", ""),
        title=raw.get("title", ""),
        subtitle=raw.get("subtitle", ""),
        authors=authors,
        narrators=narrators,
        publisher=raw.get("publisher_name", ""),
        price=price,
        list_price=list_price,
        length_minutes=raw.get("runtime_length_min", 0) or 0,
        rating=rating,
        num_ratings=num_ratings,
        categories=categories,
        category_ids=category_ids,
        series_name=series_name,
        series_position=series_position,
        language=raw.get("language", ""),
        release_date=raw.get("release_date", ""),
        in_plus_catalog=in_plus,
        locale=locale,
    )


def _extract_price(raw: dict) -> float | None:
    """Extract current/sale price. Checks lowest_price first for deals."""
    price_obj = raw.get("price")
    if isinstance(price_obj, dict):
        lowest = price_obj.get("lowest_price")
        if isinstance(lowest, dict) and "base" in lowest:
            val = lowest["base"]
            if val is not None:
                return float(val)
        lp = price_obj.get("list_price")
        if isinstance(lp, dict) and "base" in lp:
            val = lp["base"]
            if val is not None:
                return float(val)
    elif isinstance(price_obj, (int, float)):
        return float(price_obj)
    return None


def _extract_list_price(raw: dict) -> float | None:
    """Extract original list price for discount calculation."""
    price_obj = raw.get("price")
    if isinstance(price_obj, dict):
        lp = price_obj.get("list_price")
        if isinstance(lp, dict) and "base" in lp:
            val = lp["base"]
            if val is not None:
                return float(val)
    lp = raw.get("list_price")
    if isinstance(lp, (int, float)):
        return float(lp)
    return None


class DealsClient:
    """Audible API client for catalog browsing."""

    def __init__(self, auth_file: Path = AUTH_FILE, locale: str = "us"):
        self.auth_file = auth_file
        self.locale = locale
        self._client: audible.Client | None = None
        self._categories_cache: list[dict[str, str]] | None = None
        self._library_cache: set[str] | None = None

    def login(self, username: str, password: str) -> None:
        """Interactive Audible login. Persists tokens to auth_file."""
        self.auth_file.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.auth_file.parent, 0o700)
        auth = audible.Authenticator.from_login(
            username,
            password,
            locale=self.locale,
            with_username=True,
        )
        with _restrictive_umask():
            auth.to_file(self.auth_file)
        os.chmod(self.auth_file, 0o600)

    def login_external(self, callback_url_file: Path | None = None) -> None:
        """Login via external browser (for captcha/2FA). Persists tokens.

        Uses the audible package's login_url_callback parameter to control
        how the callback URL is collected. If callback_url_file is set,
        prints the OAuth URL, waits for the user to save the callback URL
        to that file, then reads it — avoiding the flaky input() prompt.
        """
        self.auth_file.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.auth_file.parent, 0o700)

        if callback_url_file:

            def _file_callback(oauth_url: str) -> str:
                print()
                print("Open this URL in your browser and log in:")
                print()
                print(oauth_url)
                print()
                print(
                    "After login you'll see a 'Page not found' page. "
                    "That's expected."
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

    def import_auth(self, source_path: Path) -> None:
        """Import auth from an audible-cli or Libation-exported JSON file."""
        self.auth_file.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.auth_file.parent, 0o700)

        raw = source_path.read_text()
        if len(raw) > 1_000_000:
            raise ValueError(
                f"Auth file too large ({len(raw):,} chars). "
                "Expected a small JSON credentials file."
            )

        data = json.loads(raw)

        # Libation's AccountsSettings.json wraps tokens in an Accounts array.
        if "Accounts" in data:
            accounts = data["Accounts"]
            if not accounts:
                raise ValueError("No accounts found in Libation settings")
            tokens = accounts[0].get("IdentityTokens", {})
            for key in ("access_token", "refresh_token"):
                if not isinstance(tokens.get(key), str) or not tokens[key]:
                    raise ValueError(
                        f"Libation auth missing required key: {key!r}"
                    )
            auth_data = {
                "website_cookies": tokens.get("website_cookies"),
                "adp_token": tokens.get("adp_token"),
                "access_token": tokens.get("access_token"),
                "refresh_token": tokens.get("refresh_token"),
                "device_private_key": tokens.get("device_private_key"),
                "store_authentication_cookie": tokens.get(
                    "store_authentication_cookie"
                ),
                "device_info": tokens.get("device_info", {}),
                "customer_info": tokens.get("customer_info", {}),
                "expires": tokens.get("expires", 0),
                "locale_code": tokens.get("locale_code", self.locale),
                "with_username": tokens.get("with_username", False),
                "encryption": False,
            }
            _atomic_write_simple(self.auth_file, json.dumps(auth_data, indent=2))
            os.chmod(self.auth_file, 0o600)
        else:
            # Already in audible-cli / Mkb79Auth format — validate required keys
            for key in ("access_token", "refresh_token"):
                if not isinstance(data.get(key), str) or not data[key]:
                    raise ValueError(
                        f"Auth file missing required key: {key!r}"
                    )
            if "locale_code" in data and data["locale_code"] not in LOCALE_DOMAIN:
                raise ValueError(
                    f"Unknown locale_code: {data['locale_code']!r}. "
                    f"Valid: {', '.join(sorted(LOCALE_DOMAIN))}"
                )
            if "encryption" not in data:
                data["encryption"] = False
            _atomic_write_simple(self.auth_file, json.dumps(data, indent=2))
            os.chmod(self.auth_file, 0o600)

    @property
    def is_authenticated(self) -> bool:
        return self.auth_file.exists()

    @property
    def client(self) -> audible.Client:
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

        resp = self.client.get("1.0/catalog/products", **params)
        if isinstance(resp, tuple):
            resp = resp[0]

        products = [parse_product(p, locale=self.locale) for p in resp.get("products", [])]
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
        """Yield (products, page_num, total) for each page of results."""
        for page_num in range(1, max_pages + 1):
            products, total = self.search_catalog(
                keywords=keywords,
                category_id=category_id,
                sort_by=sort_by,
                page=page_num,
            )
            yield products, page_num, total

            if page_num * MAX_PAGE_SIZE >= total or not products:
                break

    def get_library_asins(self) -> set[str]:
        """Fetch all ASINs in the user's Audible library.

        Cached on the client instance so repeated calls don't re-fetch.
        """
        if self._library_cache is not None:
            return self._library_cache

        asins: set[str] = set()
        page = 1
        while True:
            resp = self.client.get(
                "1.0/library",
                num_results=1000,
                page=page,
                response_groups="product_attrs",
            )
            if isinstance(resp, tuple):
                resp = resp[0]
            items = resp.get("items", [])
            for item in items:
                asin = item.get("asin", "")
                if asin:
                    asins.add(asin)
            if len(items) < 1000:
                break
            page += 1

        self._library_cache = asins
        return asins

    def get_library_pages(self) -> Iterator[tuple[list[Product], int]]:
        """Yield (products, page_num) for each page of the user's library.

        Paginates through the library endpoint using MAX_PAGE_SIZE per page
        and the same response groups as catalog queries.
        """
        page = 1  # library API uses 1-indexed pages
        while True:
            resp = self.client.get(
                "1.0/library",
                num_results=MAX_PAGE_SIZE,
                page=page,
                response_groups=CATALOG_RESPONSE_GROUPS,
            )
            if isinstance(resp, tuple):
                resp = resp[0]
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
            resp = self.client.get(
                "1.0/wishlist",
                num_results=MAX_PAGE_SIZE,
                page=page,
                response_groups=CATALOG_RESPONSE_GROUPS,
                sort_by="-DateAdded",
            )
            if isinstance(resp, tuple):
                resp = resp[0]
            products = [parse_product(p, locale=self.locale) for p in resp.get("products", [])]
            all_products.extend(products)
            if len(products) < MAX_PAGE_SIZE:
                break
            page += 1
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
        q = query.strip().lower()
        q = GENRE_ALIASES.get(q, q)

        # Exact match
        if q in names_lower:
            idx = names_lower.index(q)
            return cats[idx]["id"], cats[idx]["name"]

        # Substring match
        matches = [i for i, n in enumerate(names_lower) if q in n]
        if len(matches) == 1:
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
            return cats[idx]["id"], cats[idx]["name"]

        available = ", ".join(names)
        raise ValueError(
            f'No genre matching "{query}".\n'
            f"Available: {available}"
        )

    def get_category_name(self, category_id: str) -> str:
        """Look up a category's display name by ID."""
        _validate_category_id(category_id)
        try:
            resp = self.client.get(f"1.0/catalog/categories/{category_id}")
            if isinstance(resp, tuple):
                resp = resp[0]
            return resp.get("category", {}).get("name", category_id)
        except Exception:
            return category_id

    def _load_categories_cache(self) -> list[dict[str, str]] | None:
        """Load top-level categories from disk cache if fresh."""
        cache_file = CATEGORIES_CACHE_FILE.with_suffix(f".{self.locale}.json")
        if not cache_file.exists():
            return None
        try:
            data = json.loads(cache_file.read_text())
            if time.time() - data.get("ts", 0) < CATEGORIES_CACHE_TTL:
                return data["categories"]
        except (json.JSONDecodeError, KeyError):
            pass
        return None

    def _save_categories_cache(self, categories: list[dict[str, str]]) -> None:
        """Persist top-level categories to disk."""
        cache_file = CATEGORIES_CACHE_FILE.with_suffix(f".{self.locale}.json")
        _atomic_write_simple(cache_file, json.dumps({"ts": time.time(), "categories": categories}))

    def get_categories(self, root: str = "") -> list[dict[str, str]]:
        """Get category listing. Returns list of {id, name} dicts.

        Top-level categories are cached to disk for 7 days to save API calls.
        """
        if root:
            _validate_category_id(root)
            # Subcategories: fetch children of a specific category
            resp = self.client.get(f"1.0/catalog/categories/{root}")
            if isinstance(resp, tuple):
                resp = resp[0]
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
            resp = self.client.get(
                "1.0/catalog/categories",
                category_type="CategoriesTopLevel",
            )
            if isinstance(resp, tuple):
                resp = resp[0]
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

    def get_products_batch(self, asins: list[str]) -> list[Product]:
        """Fetch multiple products in batches of up to 50.

        Uses the plural catalog endpoint with comma-separated ASINs.
        Returns products in arbitrary order; missing ASINs are silently skipped.
        """
        results: list[Product] = []
        for i in range(0, len(asins), MAX_PAGE_SIZE):
            batch = asins[i:i + MAX_PAGE_SIZE]
            resp = self.client.get(
                "1.0/catalog/products",
                asins=",".join(batch),
                num_results=len(batch),
                response_groups=CATALOG_RESPONSE_GROUPS,
            )
            if isinstance(resp, tuple):
                resp = resp[0]
            for raw in resp.get("products", []):
                product = parse_product(raw, locale=self.locale)
                if product.asin and product.title:
                    results.append(product)
        return results
