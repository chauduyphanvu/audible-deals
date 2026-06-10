"""Shared constants for audible-deals.

Consolidates file paths, locale maps, sort options, genre aliases, and the
configuration schema used across the package. Apart from the back-compat
re-export at the bottom, this module is a dependency-free leaf, so it can
safely be imported by all other ``audible_deals`` modules.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".config" / "audible-deals"
AUTH_FILE = CONFIG_DIR / "auth.json"
CATEGORIES_CACHE_FILE = CONFIG_DIR / "categories_cache.json"
WISHLIST_FILE = CONFIG_DIR / "wishlist.json"
PROFILES_FILE = CONFIG_DIR / "profiles.json"
LAST_RESULTS_FILE = CONFIG_DIR / "last_results.json"
SEEN_ASINS_FILE = CONFIG_DIR / "seen_asins.json"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_DIR = CONFIG_DIR / "history"
NOTIFY_STATE_FILE = CONFIG_DIR / "notify_state.json"
LOCK_FILE = CONFIG_DIR / ".deals.lock"
TRACK_STATE_FILE = CONFIG_DIR / "track_state.json"
TRACK_LOG_FILE = CONFIG_DIR / "track.log"
TASTE_CACHE_FILE = CONFIG_DIR / "taste_cache.json"

# ---------------------------------------------------------------------------
# Locale maps
# ---------------------------------------------------------------------------

LOCALE_CURRENCY: dict[str, str] = {
    "us": "$",
    "uk": "£",
    "ca": "CA$",
    "au": "A$",
    "in": "₹",
    "de": "€",
    "fr": "€",
    "jp": "¥",
    "es": "€",
}
LOCALE_DOMAIN: dict[str, str] = {
    "us": "www.audible.com",
    "uk": "www.audible.co.uk",
    "ca": "www.audible.ca",
    "au": "www.audible.com.au",
    "in": "www.audible.in",
    "de": "www.audible.de",
    "fr": "www.audible.fr",
    "jp": "www.audible.co.jp",
    "es": "www.audible.es",
}


def product_url(asin: str, locale: str) -> str:
    """Audible product page URL for an ASIN in the given marketplace."""
    domain = LOCALE_DOMAIN.get(locale, "www.audible.com")
    return f"https://{domain}/pd/{asin}"


LOCALE_LANGUAGES: dict[str, str] = {
    "us": "english",
    "uk": "english",
    "ca": "english",
    "au": "english",
    "in": "english",
    "de": "german",
    "fr": "french",
    "jp": "japanese",
    "es": "spanish",
}

# ---------------------------------------------------------------------------
# API constants
# ---------------------------------------------------------------------------

MAX_PAGE_SIZE = 50
CATEGORIES_CACHE_TTL = 86400 * 7  # 7 days

CATALOG_RESPONSE_GROUPS = ",".join(
    [
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
    ]
)

# ---------------------------------------------------------------------------
# Sort options
# ---------------------------------------------------------------------------

# Server-side sort values accepted by Audible's catalog API
SORT_OPTIONS = {
    "rating": "AvgRating",
    "bestsellers": "BestSellers",
    "length": "-RuntimeLength",
    "date": "-ReleaseDate",
    "relevance": "Relevance",
    "title": "Title",
}

# Client-side sort keys (not supported by Audible API, applied locally)
CLIENT_SORT_OPTIONS = frozenset(
    {"price", "-price", "discount", "price-per-hour", "value"}
)

# All valid sort keys (server + client)
ALL_SORT_OPTIONS = frozenset(SORT_OPTIONS.keys()) | CLIENT_SORT_OPTIONS

DEFAULT_SORT = "price-per-hour"
DEFAULT_LIMIT = 25

# Sort orders used by --deep to maximize item coverage
DEEP_SORT_ORDERS = ["BestSellers", "-ReleaseDate", "AvgRating"]

# ---------------------------------------------------------------------------
# Genre aliases
# ---------------------------------------------------------------------------

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
    "horror": "mystery, thriller & suspense",
    "true crime": "mystery, thriller & suspense",
    "historical fiction": "literature & fiction",
    "historical": "history",
}

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_ASIN_RE = re.compile(r"^[A-Za-z0-9]{2,14}$")

# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------

_CONFIG_SCHEMA: dict[str, type] = {
    "skip_owned": bool,
    "max_price": float,
    "max_pph": float,
    "min_rating": float,
    "min_ratings": int,
    "min_hours": float,
    "min_discount": int,
    "language": str,
    "locale": str,
    "sort": str,
    "pages": int,
    "on_sale": bool,
    "deep": bool,
    "first_in_series": bool,
    "all_languages": bool,
    "interactive": bool,
    "limit": int,
    "narrator": str,
    "author": str,
    "series": str,
    "publisher": str,
    "skip_plus": bool,
    "only_plus": bool,
    "credit_price": float,
    "webhook": str,
    "webhook_format": str,
}

WEBHOOK_FORMATS = ("generic", "slack", "discord", "teams", "ntfy")


# Re-exported for back-compat; the lock lives in locking.py and resolves
# LOCK_FILE (defined above) at call time. Must stay at module bottom.
from audible_deals.locking import LockHeldError, run_lock  # noqa: E402, F401
