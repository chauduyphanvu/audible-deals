"""SQLite storage (stdlib ``sqlite3``): ``state.db`` for records, ``cache.db`` for caches.

``state.db`` holds everything the tool must not lose: price history, the seen
and dismissed sets, the last result session and tracking state.  ``cache.db``
holds disposable data (category lists, the taste profile) and can be deleted
at any time.  Files the user edits by hand (config, profiles, wishlist,
monitors, auth) stay JSON.

Design notes:

* One lazily opened connection per database per process, keyed by the resolved
  path, so a changed ``DEALS_HOME`` (tests, the scheduler) reopens cleanly.
* WAL journal, ``busy_timeout`` of 5 s and foreign keys on.  Writers use
  ``BEGIN IMMEDIATE`` through :func:`transaction`, which is what replaces the
  old per-file advisory locks for the state that lives here.
* Schema changes are ordered migrations tracked in ``PRAGMA user_version``.
* A database whose header cannot be read is moved aside to
  ``<name>.corrupt-<timestamp>`` and recreated; the one-time import of the old
  JSON files lives in :mod:`audible_deals.json_import` and runs on first open.

``sqlite3`` is imported lazily: opening a database is not free, and commands
such as ``--version`` and ``--help`` never need one.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from audible_deals import paths
from audible_deals.locking import advisory_lock
from audible_deals.storage import StateFileError

if TYPE_CHECKING:
    import sqlite3

logger = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 5000

#: ``meta`` keys the JSON import maintains. They live here so that opening a
#: database that is already set up needs one query and no import of the importer.
META_JSON_IMPORT = "json_import"
META_JSON_RENAMES = "json_import_renames"

Kind = Literal["state", "cache"]


class StateDatabaseError(StateFileError):
    """A database could not be opened or used; the message says what to do."""


# ---------------------------------------------------------------------------
# Schemas. Each entry is one migration; the index + 1 is its ``user_version``.
# ---------------------------------------------------------------------------

_STATE_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID""",
        """CREATE TABLE price_history (
            asin TEXT NOT NULL,
            locale TEXT NOT NULL,
            date TEXT NOT NULL,
            price REAL NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (locale, asin, date)
        ) WITHOUT ROWID""",
        "CREATE INDEX price_history_by_locale_date ON price_history (locale, date)",
        """CREATE TABLE products (
            asin TEXT NOT NULL,
            locale TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            last_price REAL,
            last_seen TEXT,
            PRIMARY KEY (locale, asin)
        ) WITHOUT ROWID""",
        "CREATE TABLE seen (asin TEXT PRIMARY KEY) WITHOUT ROWID",
        "CREATE TABLE dismissed (asin TEXT PRIMARY KEY) WITHOUT ROWID",
        """CREATE TABLE refresh_eligibility (
            locale TEXT NOT NULL,
            asin TEXT NOT NULL,
            surfaced_on TEXT NOT NULL,
            PRIMARY KEY (locale, asin)
        ) WITHOUT ROWID""",
        """CREATE INDEX refresh_eligibility_by_date
            ON refresh_eligibility (locale, surfaced_on)""",
        """CREATE TABLE monitor_state (
            name TEXT PRIMARY KEY,
            initialized INTEGER NOT NULL DEFAULT 0,
            last_success TEXT,
            last_error TEXT
        ) WITHOUT ROWID""",
        """CREATE TABLE monitor_products (
            monitor TEXT NOT NULL REFERENCES monitor_state (name) ON DELETE CASCADE,
            asin TEXT NOT NULL,
            price REAL,
            data TEXT NOT NULL,
            PRIMARY KEY (monitor, asin)
        ) WITHOUT ROWID""",
        """CREATE TABLE result_sessions (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            body TEXT NOT NULL,
            saved_at TEXT NOT NULL
        )""",
        """CREATE TABLE notify_state (
            asin TEXT PRIMARY KEY,
            price REAL NOT NULL,
            date TEXT NOT NULL
        ) WITHOUT ROWID""",
        """CREATE TABLE refresh_cursors (
            locale TEXT PRIMARY KEY,
            surfaced_on TEXT NOT NULL,
            asin TEXT NOT NULL
        ) WITHOUT ROWID""",
        # A NULL counter means the run did not record it (a failed run, or one
        # from before monitors existed); the status table shows those as "-".
        """CREATE TABLE track_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            duration_s REAL,
            wishlist_checked INTEGER,
            author_watches_checked INTEGER,
            extra_tracked_checked INTEGER,
            hits INTEGER,
            suppressed INTEGER,
            webhook_sent INTEGER,
            monitors_checked INTEGER,
            monitors_scheduled INTEGER,
            monitor_events INTEGER,
            monitor_failures TEXT,
            error TEXT
        )""",
        """CREATE TABLE track_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID""",
    ),
    # 2: the product index behind "known candidates": the last-seen product
    # (as JSON) and its categories, so a later scan can re-price titles it did
    # not return. Kept apart from `products` so the lookups that only need
    # titles and prices (recap, history) keep reading narrow rows. Titles
    # recorded before this schema have no index row until they are seen again.
    (
        """CREATE TABLE product_index (
            locale TEXT NOT NULL,
            asin TEXT NOT NULL,
            data TEXT NOT NULL,
            PRIMARY KEY (locale, asin),
            FOREIGN KEY (locale, asin) REFERENCES products (locale, asin)
                ON DELETE CASCADE
        ) WITHOUT ROWID""",
        """CREATE TABLE product_categories (
            locale TEXT NOT NULL,
            asin TEXT NOT NULL,
            category_id TEXT NOT NULL,
            PRIMARY KEY (locale, asin, category_id),
            FOREIGN KEY (locale, asin) REFERENCES products (locale, asin)
                ON DELETE CASCADE
        ) WITHOUT ROWID""",
        """CREATE INDEX product_categories_by_category
            ON product_categories (locale, category_id, asin)""",
    ),
)

_CACHE_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE cache_entries (
            namespace TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            stored_at REAL NOT NULL,
            PRIMARY KEY (namespace, key)
        ) WITHOUT ROWID""",
    ),
)

_MIGRATIONS: dict[Kind, tuple[tuple[str, ...], ...]] = {
    "state": _STATE_MIGRATIONS,
    "cache": _CACHE_MIGRATIONS,
}

#: Tables a user-facing summary lists, in display order.
STATE_TABLES = (
    "price_history",
    "products",
    "seen",
    "dismissed",
    "refresh_eligibility",
    "monitor_state",
    "monitor_products",
    "result_sessions",
    "notify_state",
    "track_runs",
)


def schema_version(kind: Kind = "state") -> int:
    """The newest schema this build knows for *kind*."""
    return len(_MIGRATIONS[kind])


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


@dataclass
class _Slot:
    path: Path
    conn: sqlite3.Connection


_slots: dict[Kind, _Slot] = {}
_guard = threading.RLock()


def _path_for(kind: Kind) -> Path:
    return paths.state_db_file() if kind == "state" else paths.cache_db_file()


def _is_corruption(exc: Exception) -> bool:
    import sqlite3

    # "file is not a database" / "database disk image is malformed" arrive as the
    # plain DatabaseError; locked, read-only and cannot-open are OperationalError.
    return isinstance(exc, sqlite3.DatabaseError) and not isinstance(
        exc, sqlite3.OperationalError
    )


def _connect(path: Path) -> sqlite3.Connection:
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path,
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,  # autocommit; transactions are explicit
        check_same_thread=False,
    )
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        _enable_wal(conn)
        conn.execute("PRAGMA synchronous = NORMAL")
    except BaseException:
        conn.close()
        raise
    return conn


def _enable_wal(conn: sqlite3.Connection) -> None:
    import sqlite3

    for attempt in range(5):
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError:
            # Another process is switching modes right now; give it a moment.
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _setup_pending(conn: sqlite3.Connection, kind: Kind, path: Path) -> bool:
    version = _user_version(conn)
    latest = schema_version(kind)
    if version > latest:
        raise StateDatabaseError(
            path,
            f"{path} was written by a newer audible-deals "
            f"(schema {version}, this version understands {latest}). "
            "Upgrade audible-deals; nothing was changed.",
        )
    if version < latest:
        return True
    if kind == "state":
        keys = {
            row[0]
            for row in conn.execute(
                "SELECT key FROM meta WHERE key IN (?, ?)",
                (META_JSON_IMPORT, META_JSON_RENAMES),
            )
        }
        return META_JSON_IMPORT not in keys or META_JSON_RENAMES in keys
    return False


def _apply_schema(conn: sqlite3.Connection, kind: Kind) -> None:
    migrations = _MIGRATIONS[kind]
    for target in range(_user_version(conn) + 1, len(migrations) + 1):
        with transaction(conn):
            if _user_version(conn) >= target:
                continue  # another process finished this step while we waited
            for statement in migrations[target - 1]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {target}")
        logger.debug("%s.db migrated to schema %d", kind, target)


def _prepare(conn: sqlite3.Connection, kind: Kind, path: Path) -> None:
    if not _setup_pending(conn, kind, path):
        return
    with advisory_lock(paths.migration_lock_file(), wait=True):
        _apply_schema(conn, kind)
        if kind == "state":
            from audible_deals import json_import

            json_import.ensure_imported(conn)


def _sidecars(path: Path) -> list[Path]:
    return [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm")]


def _quarantine(path: Path, kind: Kind, reason: Exception) -> None:
    """Move an unreadable database (and its WAL files) out of the way."""
    stamp = time.strftime("%Y%m%dT%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    counter = 0
    while target.exists():
        counter += 1
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{counter}")
    moved = False
    for suffix in ("", "-wal", "-shm"):
        source = path.with_name(path.name + suffix)
        try:
            os.replace(source, target.with_name(target.name + suffix))
        except FileNotFoundError:
            continue  # another process already moved it
        except OSError as exc:
            raise StateDatabaseError(
                path,
                f"{path} is unreadable ({reason}) and could not be moved aside "
                f"({exc}). Move or delete it, then try again.",
            ) from exc
        moved = moved or not suffix
    if kind == "state":
        if moved:
            logger.warning(
                "%s was unreadable (%s); moved it to %s and started a fresh database",
                path.name,
                reason,
                target.name,
            )
        return
    logger.debug("cache database %s was unreadable (%s); recreated", path, reason)
    # A cache is disposable: keeping a quarantined copy is pointless.
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            target.with_name(target.name + suffix).unlink()


def _open(kind: Kind, path: Path) -> sqlite3.Connection:
    import sqlite3

    last: Exception | None = None
    for _attempt in range(2):
        conn: sqlite3.Connection | None = None
        try:
            conn = _connect(path)
            _prepare(conn, kind, path)
            return conn
        except StateDatabaseError:
            if conn is not None:
                conn.close()
            raise
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            if not _is_corruption(exc):
                raise StateDatabaseError(
                    path,
                    f"Cannot open {path}: {exc}. If another deals process is running, "
                    "wait for it to finish and try again.",
                ) from exc
            last = exc
            _quarantine(path, kind, exc)
    raise StateDatabaseError(
        path, f"{path} is unreadable ({last}) even after recreating it."
    )


def _get(kind: Kind) -> sqlite3.Connection:
    path = _path_for(kind)
    with _guard:
        slot = _slots.get(kind)
        if slot is not None and slot.path == path:
            return slot.conn
        if slot is not None:
            with contextlib.suppress(Exception):
                slot.conn.close()
            del _slots[kind]
        conn = _open(kind, path)
        _slots[kind] = _Slot(path, conn)
        return conn


def state() -> sqlite3.Connection:
    """The process-wide ``state.db`` connection (opened and migrated on demand)."""
    return _get("state")


def cache() -> sqlite3.Connection:
    """The process-wide ``cache.db`` connection (opened on demand)."""
    return _get("cache")


def close_all() -> None:
    """Close every open connection (tests, interpreter exit)."""
    with _guard:
        for slot in _slots.values():
            with contextlib.suppress(Exception):
                slot.conn.close()
        _slots.clear()


atexit.register(close_all)


@contextlib.contextmanager
def exclusive() -> Iterator[None]:
    """Hold the process-wide connection lock for a block of reads.

    The connections are shared between threads and sqlite3 does not serialize
    statements on one connection (identical SQL from two threads can trip over
    the shared statement cache), so threads that read outside a transaction
    take this first. ``transaction`` already holds it.
    """
    with _guard:
        yield


@contextlib.contextmanager
def transaction(
    conn: sqlite3.Connection, *, write: bool = True
) -> Iterator[sqlite3.Connection]:
    """Run a block atomically: ``BEGIN IMMEDIATE`` for writes, snapshot reads otherwise.

    Joins an already open transaction on the same connection instead of nesting.
    """
    import sqlite3

    with _guard:
        if conn.in_transaction:
            yield conn
            return
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        except sqlite3.OperationalError as exc:
            raise StateDatabaseError(
                Path(_describe(conn)),
                f"The database is busy ({exc}). Another deals process may be "
                "running; try again in a moment.",
            ) from exc
        try:
            yield conn
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def _describe(conn: sqlite3.Connection) -> str:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return str(row[2] or ":memory:")
    return ":memory:"


def chunked(items: Sequence, size: int = 500) -> Iterator[Sequence]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ---------------------------------------------------------------------------
# meta helpers and diagnostics
# ---------------------------------------------------------------------------


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def table_counts(conn: sqlite3.Connection, tables: Sequence[str]) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
        for table in tables
    }


def quick_check(conn: sqlite3.Connection) -> list[str]:
    """``PRAGMA quick_check`` results; ``["ok"]`` when the database is healthy."""
    return [str(row[0]) for row in conn.execute("PRAGMA quick_check")]


def file_size(path: Path) -> int:
    """Bytes used by a database file and its WAL sidecars."""
    total = 0
    for candidate in (path, *_sidecars(path)):
        with contextlib.suppress(OSError):
            total += candidate.stat().st_size
    return total
