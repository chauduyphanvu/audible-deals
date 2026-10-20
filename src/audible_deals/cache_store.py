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


def get(namespace: str, key: str, *, max_age_s: float | None = None) -> Any | None:
    """The cached value, or ``None`` when absent, stale (older than *max_age_s*) or unreadable."""
    try:
        row = (
            db.cache()
            .execute(
                "SELECT value, stored_at FROM cache_entries WHERE namespace = ? AND key = ?",
                (namespace, key),
            )
            .fetchone()
        )
        if row is None:
            logger.debug("cache miss: %s/%s", namespace, key)
            return None
        age = time.time() - row[1]
        if max_age_s is not None and age >= max_age_s:
            logger.debug("cache stale: %s/%s (age=%.0fs)", namespace, key, age)
            return None
        logger.debug("cache hit: %s/%s (age=%.0fs)", namespace, key, age)
        return json.loads(row[0])
    except Exception:
        logger.debug("cache read failed: %s/%s", namespace, key, exc_info=True)
        return None


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


def clear(namespace: str | None = None) -> int:
    """Drop cached entries (all, or one namespace). Returns how many were removed."""
    with db.transaction(db.cache()) as conn:
        if namespace is None:
            return conn.execute("DELETE FROM cache_entries").rowcount
        return conn.execute(
            "DELETE FROM cache_entries WHERE namespace = ?", (namespace,)
        ).rowcount


def summary() -> dict[str, tuple[int, float]]:
    """``{namespace: (entries, newest stored_at)}`` for ``deals cache info``."""
    rows = db.cache().execute(
        "SELECT namespace, COUNT(*), MAX(stored_at) FROM cache_entries GROUP BY namespace"
    )
    return {namespace: (count, newest) for namespace, count, newest in rows}
