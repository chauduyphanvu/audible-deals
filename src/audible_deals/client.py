"""Audible API client for catalog browsing and deal discovery.

Architecture informed by Libation's AudibleUtilities/ApiExtended.cs:
- Response group selection matching Libation's catalog query patterns
- Batch sizing (50 items) and concurrency patterns
- Price/category field parsing from AudibleApi.Common.Item model
- Auth token format from Mkb79Auth.cs (shared with Python audible package)
"""

from __future__ import annotations

import difflib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import audible

# Response groups for catalog queries, matching Libation's comprehensive set.
# ref: Libation/Source/ApplicationServices/LibraryCommands.cs:124-130
# ref: Libation/Source/AudibleUtilities/ApiExtended.cs:231-235
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

# Matches Libation's BatchSize constant (ApiExtended.cs:26)
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
    """Audiobook product from Audible catalog.

    Field mapping follows Libation's DataLayer/EfClasses/Book.cs entity
    and AudibleApi.Common.Item model.
    """

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

    Handles the nested response format from Audible's catalog API,
    mirroring Libation's DtoImporterService item mapping logic.
    """
    price = _extract_price(raw)
    list_price = _extract_list_price(raw)

    authors = [a.get("name", "") for a in raw.get("authors", []) if a.get("name")]
    narrators = [n.get("name", "") for n in raw.get("narrators", []) if n.get("name")]

    # Rating - nested in overall_distribution
    rating_data = raw.get("rating", {})
    rating = 0.0
    num_ratings = 0
    if isinstance(rating_data, dict):
        dist = rating_data.get("overall_distribution", {})
        try:
            rating = float(dist.get("display_average_rating", 0) or 0)
        except (ValueError, TypeError):
            pass
        try:
            num_ratings = int(dist.get("num_ratings", 0) or 0)
        except (ValueError, TypeError):
            pass

    # Categories - flatten ladder structure
    # ref: Libation DataLayer/EfClasses/CategoryLadder.cs
    categories: list[str] = []
    category_ids: list[str] = []
    for ladder in raw.get("category_ladders", []):
        for cat in ladder.get("ladder", []):
            name = cat.get("name", "")
            cid = cat.get("id", "")
            if name and name not in categories:
                categories.append(name)
            if cid and cid not in category_ids:
                category_ids.append(cid)

    # Series info
    series_name = ""
    series_position = ""
    series_list = raw.get("series", [])
    if series_list:
        s = series_list[0]
        series_name = s.get("title", "")
        series_position = s.get("sequence", "")

    # Audible Plus detection (ref: Libation checks IsAyce / Plans)
    in_plus = False
    for plan in raw.get("plans", []):
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
    """Audible API client for catalog browsing.

    Concurrency and pagination patterns match Libation's ApiExtended.cs
    (MaxConcurrency=10, BatchSize=50).
    """

    def __init__(self, auth_file: Path = AUTH_FILE, locale: str = "us"):
        self.auth_file = auth_file
        self.locale = locale
        self._client: audible.Client | None = None
        self._categories_cache: list[dict[str, str]] | None = None
        self._library_cache: set[str] | None = None

    def login(self, username: str, password: str) -> None:
        """Interactive Audible login. Persists tokens to auth_file."""
        self.auth_file.parent.mkdir(parents=True, exist_ok=True)
        auth = audible.Authenticator.from_login(
            username,
            password,
            locale=self.locale,
            with_username=True,
        )
        auth.to_file(self.auth_file)

    def login_external(self, callback_url_file: Path | None = None) -> None:
        """Login via external browser (for captcha/2FA). Persists tokens.

        Uses the audible package's login_url_callback parameter to control
        how the callback URL is collected. If callback_url_file is set,
        prints the OAuth URL, waits for the user to save the callback URL
        to that file, then reads it — avoiding the flaky input() prompt.
        """
        self.auth_file.parent.mkdir(parents=True, exist_ok=True)

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

        auth.to_file(self.auth_file)

    def import_auth(self, source_path: Path) -> None:
        """Import auth from an audible-cli or Libation-exported JSON file.

        The Mkb79Auth format used by Libation (Mkb79Auth.cs) is directly
        compatible with the Python audible package's auth file format.
        Both share the same JSON keys: access_token, refresh_token,
        adp_token, device_private_key, device_info, customer_info, etc.
        """
        self.auth_file.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(source_path.read_text())

        # Libation's AccountsSettings.json wraps tokens in an Accounts array.
        # Each account has IdentityTokens in Mkb79Auth format.
        if "Accounts" in data:
            accounts = data["Accounts"]
            if not accounts:
                raise ValueError("No accounts found in Libation settings")
            tokens = accounts[0].get("IdentityTokens", {})
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
            self.auth_file.write_text(json.dumps(auth_data, indent=2))
        else:
            # Already in audible-cli / Mkb79Auth format
            if "encryption" not in data:
                data["encryption"] = False
            self.auth_file.write_text(json.dumps(data, indent=2))

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
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"ts": time.time(), "categories": categories}))

    def get_categories(self, root: str = "") -> list[dict[str, str]]:
        """Get category listing. Returns list of {id, name} dicts.

        Top-level categories are cached to disk for 7 days to save API calls.
        """
        if root:
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

        Uses the plural catalog endpoint with comma-separated ASINs
        (like Libation's GetCatalogProductsAsync).
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
