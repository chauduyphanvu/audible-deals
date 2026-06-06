"""Shared constants and base utilities for audible-deals.

Consolidates file paths, locale maps, sort options, genre aliases,
configuration schema, and the atomic-write utility used across the package.
This module is a dependency-free leaf — it does not import from any other
``audible_deals`` module, so it can safely be imported by all of them.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import time
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
}


# ---------------------------------------------------------------------------
# Atomic file write
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, content: str) -> None:
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


# ---------------------------------------------------------------------------
# Run lock
# ---------------------------------------------------------------------------

_LOCK_STALE_SECONDS = 600  # 10 minutes


class LockHeldError(Exception):
    """Raised when the run lock is held by another process."""


@contextlib.contextmanager
def run_lock():
    """Exclusive cross-process lock for unattended commands.

    Acquires via O_CREAT|O_EXCL (atomic on POSIX and Windows NTFS).
    Treats the lock as stale when its mtime is older than 10 minutes.
    Raises LockHeldError when a fresh lock is held by another process.

    PID-ownership: writes our PID to the lock file; the finally block only
    unlinks the file when it still contains our PID, so we never remove
    another process's lock on exit.

    Stale-break: after unlinking a stale lock we retry the O_EXCL create
    exactly once; if that create also fails, we lost the race and raise
    LockHeldError rather than looping indefinitely.
    """
    my_pid = str(os.getpid()).encode()
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    def _try_create() -> bool:
        """Attempt O_EXCL create; return True on success, False on FileExistsError."""
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        try:
            os.write(fd, my_pid)
        except OSError:
            os.close(fd)
            LOCK_FILE.unlink(missing_ok=True)
            raise
        os.close(fd)
        return True

    if not _try_create():
        # Lock file exists — check staleness
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
        except FileNotFoundError:
            age = _LOCK_STALE_SECONDS + 1  # vanished between checks; retry once

        if age > _LOCK_STALE_SECONDS:
            try:
                LOCK_FILE.unlink()
            except FileNotFoundError:
                pass
            # Retry once; if another racer beat us, raise immediately
            if not _try_create():
                raise LockHeldError(f"Lock held (mtime {age:.0f}s ago): {LOCK_FILE}")
        else:
            raise LockHeldError(f"Lock held (mtime {age:.0f}s ago): {LOCK_FILE}")

    try:
        yield
    finally:
        try:
            if LOCK_FILE.read_bytes() == my_pid:
                LOCK_FILE.unlink()
        except (FileNotFoundError, OSError):
            pass
