"""Disposable caches in ``cache.db`` (category lists, the taste profile).

Everything here can be rebuilt from Audible, so a failure to read or write
never fails a command: a miss is returned and the caller fetches fresh data.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from audible_deals import db

logger = logging.getLogger(__name__)


def get_entry(
    namespace: str, key: str, *, max_age_s: float | None = None
) -> tuple[Any, float] | None:
    """``(value, age in seconds)``, or ``None`` when absent, stale or unreadable."""
    try:
        with db.exclusive():
            row = (
                db.cache()
                .execute(
                    "SELECT value, stored_at FROM cache_entries "
                    "WHERE namespace = ? AND key = ?",
                    (namespace, key),
                )
                .fetchone()
            )
        if row is None:
            logger.debug("cache miss: %s/%s", namespace, key)
            return None
        age = max(0.0, time.time() - row[1])
        if max_age_s is not None and age >= max_age_s:
            logger.debug("cache stale: %s/%s (age=%.0fs)", namespace, key, age)
            return None
        logger.debug("cache hit: %s/%s (age=%.0fs)", namespace, key, age)
        return json.loads(row[0]), age
    except Exception:
        logger.debug("cache read failed: %s/%s", namespace, key, exc_info=True)
        return None


def has_entry(namespace: str, key: str, *, max_age_s: float | None = None) -> bool:
    """Whether a fresh entry exists, without reading or decoding its value."""
    try:
        with db.exclusive():
            row = (
                db.cache()
                .execute(
                    "SELECT stored_at FROM cache_entries "
                    "WHERE namespace = ? AND key = ?",
                    (namespace, key),
                )
                .fetchone()
            )
    except Exception:
        logger.debug("cache probe failed: %s/%s", namespace, key, exc_info=True)
        return False
    if row is None:
        return False
    return max_age_s is None or max(0.0, time.time() - row[0]) < max_age_s


def get(namespace: str, key: str, *, max_age_s: float | None = None) -> Any | None:
    """The cached value, or ``None`` when absent, stale (older than *max_age_s*) or unreadable."""
    entry = get_entry(namespace, key, max_age_s=max_age_s)
    return None if entry is None else entry[0]


def put(namespace: str, key: str, value: Any) -> None:
    try:
        with db.transaction(db.cache()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache_entries (namespace, key, value, stored_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    namespace,
                    key,
                    json.dumps(value, ensure_ascii=False, allow_nan=False),
                    time.time(),
                ),
            )
        logger.debug("cache saved: %s/%s", namespace, key)
    except Exception:
        logger.debug("cache write failed: %s/%s", namespace, key, exc_info=True)


def prune(prefix: str, *, max_age_s: float, keep: int) -> int:
    """Trim the namespaces starting with *prefix*.

    Drops entries older than *max_age_s*, then everything but the newest *keep*.
    Returns how many were removed; never raises.
    """
    scope = {"n": len(prefix), "prefix": prefix}
    try:
        with db.transaction(db.cache()) as conn:
            removed = conn.execute(
                "DELETE FROM cache_entries WHERE substr(namespace, 1, :n) = :prefix "
                "AND stored_at < :cutoff",
                {**scope, "cutoff": time.time() - max_age_s},
            ).rowcount
            removed += conn.execute(
                "DELETE FROM cache_entries WHERE substr(namespace, 1, :n) = :prefix "
                "AND (namespace, key) NOT IN (SELECT namespace, key FROM cache_entries "
                "WHERE substr(namespace, 1, :n) = :prefix "
                "ORDER BY stored_at DESC LIMIT :keep)",
                {**scope, "keep": keep},
            ).rowcount
        return removed
    except Exception:
        logger.debug("cache prune failed: %s", prefix, exc_info=True)
        return 0


def clear(namespace: str | None = None) -> int:
    """Drop cached entries (all, or one namespace). Returns how many were removed."""
    with db.transaction(db.cache()) as conn:
        if namespace is None:
            return conn.execute("DELETE FROM cache_entries").rowcount
        return conn.execute(
            "DELETE FROM cache_entries WHERE namespace = ?", (namespace,)
        ).rowcount


def compact() -> None:
    """Give the file's free pages back to the disk.

    Deleting rows leaves their pages inside ``cache.db`` (SQLite reuses them
    but does not shrink the file), and the write-ahead log can hold a second
    copy, so after a clear the file would stay as large as it was. Best effort:
    the cache is disposable, and a failure only means the file keeps its size.
    """
    try:
        with db.exclusive():
            conn = db.cache()
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        logger.debug("cache compaction failed", exc_info=True)


def summary() -> dict[str, tuple[int, float]]:
    """``{namespace: (entries, newest stored_at)}`` for ``deals cache info``."""
    with db.exclusive():
        rows = (
            db.cache()
            .execute(
                "SELECT namespace, COUNT(*), MAX(stored_at) FROM cache_entries "
                "GROUP BY namespace"
            )
            .fetchall()
        )
    return {namespace: (count, newest) for namespace, count, newest in rows}
