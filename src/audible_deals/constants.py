"""Shared constants for audible-deals.

Consolidates locale maps, sort options, genre aliases, and the configuration
schema used across the package. (File locations live in ``paths.py``, which
resolves them at call time.) This module is a dependency-free
leaf, so it can safely be imported by all other ``audible_deals`` modules.
"""

from __future__ import annotations

import re

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
MAX_CATALOG_CALLS_PER_SCAN = 60
CATEGORIES_CACHE_TTL = 86400 * 7  # 7 days
CATALOG_CACHE_DEFAULT_TTL_MINUTES = 120
CATALOG_CACHE_MAX_TTL_MINUTES = 7 * 24 * 60
LIBRARY_CACHE_TTL = 86400  # the cached list of owned titles

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

#: ``teams`` is an Adaptive Card for Power Automate Workflows webhooks (a URL on
#: the retired Office 365 connector hosts automatically keeps MessageCard);
#: ``teams-legacy`` always sends MessageCard.
WEBHOOK_FORMATS = ("generic", "slack", "discord", "teams", "teams-legacy", "ntfy")
