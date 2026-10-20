"""One-time import of the retired JSON state files into ``state.db``.

Earlier releases kept price history, the seen and dismissed sets, the last
result session and the tracking state in JSON files under the data directory.
On the first open of ``state.db`` this module reads them all, writes them in a
single transaction, and then renames each source to ``<name>.migrated`` (the
``history`` directory becomes ``history.migrated``). Nothing is ever deleted.

Guarantees:

* Nothing is written to ``state.db`` unless every source was read first; a read
  error aborts with the files untouched, and the next run retries.
* The import is one ``BEGIN IMMEDIATE`` transaction that also records a marker
  in ``meta``. Processes racing to open a fresh database are serialized by an
  advisory file lock, and the marker is re-checked inside the transaction, so
  the data is imported exactly once.
* The transaction is committed *before* the files are renamed. If a rename
  fails (permissions, a held file on Windows) the remaining names are recorded
  and retried on the next open; the data is never imported twice.
* Content that cannot be used (a corrupt file, a malformed entry) is skipped
  and counted, never fatal; the source is still kept as ``*.migrated``.

Choices worth knowing about, because they differ from the old loaders:

* History entries need an ISO date and a finite numeric price; anything else is
  skipped. Two entries for one date collapse to the later one in the file.
* A legacy top-level list history carried no marketplace, so (as before) it is
  not assigned to one; the file is kept under ``history.migrated`` untouched.
* One bad entry in ``refresh_eligibility.json`` no longer discards the whole
  file; valid entries are imported.
* Extra, unknown keys in the old track/monitor state files are not carried.
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard

from audible_deals import constants, paths
from audible_deals.db import (
    META_JSON_IMPORT,
    META_JSON_RENAMES,
    StateDatabaseError,
    meta_get,
    meta_set,
    transaction,
)
from audible_deals.logging_setup import NOTE
from audible_deals.state_store import RUN_HISTORY_MAX, insert_track_run
from audible_deals.storage import read_text

if TYPE_CHECKING:
    import sqlite3

logger = logging.getLogger(__name__)

META_KEY = META_JSON_IMPORT
META_RENAMES = META_JSON_RENAMES
_SUFFIX = ".migrated"

_SIMPLE_FILES = (
    "seen_asins.json",
    "dismissed_asins.json",
    "refresh_eligibility.json",
    "monitor_state.json",
    "last_results.json",
    "notify_state.json",
    "track_state.json",
)
# Disposable caches: not imported, but moved aside like everything else.
_CACHE_FILES = ("taste_cache.json",)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@dataclass
class Imported:
    """Everything parsed from the old files, ready to write."""

    history: list[tuple[str, str, str, float, str]] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    dismissed: set[str] = field(default_factory=set)
    eligibility: list[tuple[str, str, str]] = field(default_factory=list)
    monitors: dict[str, dict[str, Any]] = field(default_factory=dict)
    session: tuple[str, str] | None = None
    notify: list[tuple[str, float, str]] = field(default_factory=list)
    refresh_cursors: list[tuple[str, str, str]] = field(default_factory=list)
    track_values: dict[str, Any] = field(default_factory=dict)
    runs: list[dict[str, Any]] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def skip(self, what: str, count: int = 1) -> None:
        if count:
            self.skipped[what] = self.skipped.get(what, 0) + count


def _is_number(value: object) -> TypeGuard[int | float]:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _text(value: str) -> str:
    """*value* with anything SQLite cannot store (lone surrogates) replaced."""
    if value.isascii():
        return value
    return value.encode("utf-8", "replace").decode("utf-8")


def _iso_date(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError:
        return None


_SQLITE_INT_LIMIT = 2**62


def _as_int(value: object) -> int:
    """A count from the old file; anything that is not a sane number is 0."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)) and math.isfinite(value):
        number = int(value)
        return number if abs(number) < _SQLITE_INT_LIMIT else 0
    return 0


def _read_json(
    path: Path, expected: type | tuple[type, ...], out: Imported
) -> Any | None:
    """Parse *path*; an unusable document is noted and skipped.

    An unreadable file (permissions, I/O error) aborts the whole import so the
    files stay where they are and the next run can try again.
    """
    try:
        text = read_text(path)
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        out.notes.append(f"{path.name} is not valid text ({exc.reason}); not imported")
        return None
    except OSError as exc:
        raise StateDatabaseError(
            path,
            f"Cannot migrate {path}: {exc.strerror or exc}. Fix the permissions and "
            "run the command again; your files were not changed.",
        ) from exc
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        out.notes.append(
            f"{path.name} is not valid JSON (line {exc.lineno}, column {exc.colno}); "
            "not imported"
        )
        return None
    if not isinstance(data, expected):
        out.notes.append(f"{path.name} does not hold the expected data; not imported")
        return None
    return data


def _parse_history(directory: Path, out: Imported) -> None:
    try:
        files = sorted(directory.glob("*.json"))
    except OSError as exc:
        raise StateDatabaseError(
            directory,
            f"Cannot migrate {directory}: {exc}. Your files were not changed.",
        ) from exc
    for file in files:
        asin = _text(file.stem)
        data = _read_json(file, (dict, list), out)
        if data is None:
            out.skip("history files that were empty or unreadable")
            continue
        if isinstance(data, list):
            # Pre-marketplace format: prices have no locale and are not guessed at.
            out.skip("legacy unscoped history files")
            continue
        if not constants._ASIN_RE.fullmatch(asin):
            out.skip("history files with an invalid ASIN")
            continue
        markets = data.get("marketplaces")
        if not isinstance(markets, dict):
            out.skip("history files with an unexpected shape")
            continue
        for locale, entries in markets.items():
            if not isinstance(locale, str) or not isinstance(entries, list):
                out.skip("history marketplaces with an unexpected shape")
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    out.skip("history entries without a valid date and price")
                    continue
                date = _iso_date(entry.get("date"))
                price = entry.get("price")
                if date is None or not _is_number(price):
                    out.skip("history entries without a valid date and price")
                    continue
                title = entry.get("title")
                out.history.append(
                    (
                        asin,
                        _text(locale),
                        date,
                        float(price),
                        _text(title) if isinstance(title, str) else "",
                    )
                )


def _parse_asin_list(path: Path, out: Imported) -> set[str]:
    data = _read_json(path, list, out)
    if not data:
        return set()
    # A list may repeat an ASIN (it is a set; the repeats merge silently); only
    # entries that are not text at all are unusable.
    out.skip(
        f"{path.stem} entries that were not text",
        sum(1 for item in data if not isinstance(item, str)),
    )
    return {_text(item) for item in data if isinstance(item, str)}


def _parse_eligibility(path: Path, out: Imported) -> None:
    data = _read_json(path, dict, out)
    if data is None:
        return
    if data.get("version") != 1 or not isinstance(data.get("marketplaces"), dict):
        out.notes.append(f"{path.name} has an unsupported version; not imported")
        return
    for locale, entries in data["marketplaces"].items():
        if locale not in constants.LOCALE_DOMAIN or not isinstance(entries, dict):
            out.skip("refresh-eligibility marketplaces that were invalid")
            continue
        for asin, observed in entries.items():
            date = _iso_date(observed)
            if (
                not isinstance(asin, str)
                or not constants._ASIN_RE.fullmatch(asin)
                or not date
            ):
                out.skip("refresh-eligibility entries that were invalid")
                continue
            out.eligibility.append((locale, asin, date))


def _backfill_eligibility(out: Imported) -> None:
    """Recreate the eligibility set from history for already-seen ASINs.

    This is what the old code did the first time it found no eligibility file.
    """
    latest: dict[tuple[str, str], str] = {}
    for asin, locale, date, _price, _title in out.history:
        if asin in out.seen and locale in constants.LOCALE_DOMAIN:
            key = (locale, asin)
            if date > latest.get(key, ""):
                latest[key] = date
    out.eligibility = [
        (loc, asin, date) for (loc, asin), date in sorted(latest.items())
    ]


def _parse_monitor_state(path: Path, out: Imported) -> None:
    data = _read_json(path, dict, out)
    if data is None:
        return
    if data.get("version") != 1:
        out.notes.append(f"{path.name} has an unsupported version; not imported")
        return
    monitors = data.get("monitors")
    if not isinstance(monitors, dict):
        return
    for name, raw in monitors.items():
        products = raw.get("products", {}) if isinstance(raw, dict) else None
        if not isinstance(name, str) or not isinstance(products, dict):
            out.skip("monitor snapshots that were malformed")
            continue
        valid = {
            _text(asin): product
            for asin, product in products.items()
            if isinstance(asin, str) and isinstance(product, dict)
        }
        out.skip("monitor products that were malformed", len(products) - len(valid))
        out.monitors[_text(name)] = {
            "initialized": bool(raw.get("initialized")),
            "last_success": _text(raw["last_success"])
            if isinstance(raw.get("last_success"), str)
            else None,
            "last_error": _text(raw["last_error"])
            if isinstance(raw.get("last_error"), str)
            else None,
            "products": valid,
        }


def _parse_notify(path: Path, out: Imported) -> None:
    data = _read_json(path, dict, out)
    for asin, entry in (data or {}).items():
        date = _iso_date(entry.get("date")) if isinstance(entry, dict) else None
        if (
            not isinstance(asin, str)
            or date is None
            or not _is_number(entry.get("price"))
        ):
            out.skip("notify-state entries that were malformed")
            continue
        out.notify.append((_text(asin), float(entry["price"]), date))


_RUN_INTS = (
    "wishlist_checked",
    "author_watches_checked",
    "extra_tracked_checked",
    "hits",
    "suppressed",
    "monitors_checked",
    "monitors_scheduled",
    "monitor_events",
)


def _run_record(raw: dict[str, Any]) -> dict[str, Any]:
    """The recognized fields a run record actually has (others stay NULL)."""
    record: dict[str, Any] = {
        "at": _text(raw["at"]) if isinstance(raw.get("at"), str) else ""
    }
    if "duration_s" in raw:
        record["duration_s"] = (
            float(raw["duration_s"]) if _is_number(raw["duration_s"]) else 0.0
        )
    for key in _RUN_INTS:
        if key in raw:
            record[key] = _as_int(raw[key])
    if "webhook_sent" in raw:
        record["webhook_sent"] = bool(raw["webhook_sent"])
    if "monitor_failures" in raw:
        failures = raw["monitor_failures"]
        record["monitor_failures"] = (
            [_text(str(item)) for item in failures]
            if isinstance(failures, (list, tuple))
            else []
        )
    if isinstance(raw.get("error"), str):
        record["error"] = _text(raw["error"])
    return record


def _parse_track_state(path: Path, out: Imported) -> None:
    data = _read_json(path, dict, out)
    if data is None:
        return
    cursors = data.get("refresh_cursors")
    for locale, cursor in cursors.items() if isinstance(cursors, dict) else ():
        date = (
            _iso_date(cursor.get("surfaced_on")) if isinstance(cursor, dict) else None
        )
        asin = cursor.get("asin") if isinstance(cursor, dict) else None
        if not isinstance(locale, str) or date is None or not isinstance(asin, str):
            out.skip("refresh cursors that were malformed")
            continue
        out.refresh_cursors.append((_text(locale), date, _text(asin)))
    out.track_values["monitor_cursor"] = _as_int(data.get("monitor_cursor"))
    if data.get("auth_error_notified"):
        out.track_values["auth_error_notified"] = True
    if isinstance(data.get("install"), dict):
        out.track_values["install"] = data["install"]
    history = data.get("run_history")
    if "run_history" not in data:
        last = data.get("last_run")
        history = [last] if isinstance(last, dict) else []
    if not isinstance(history, list):
        history = []
    runs = [item for item in history if isinstance(item, dict)]
    out.skip("run-history records that were malformed", len(history) - len(runs))
    out.runs = [_run_record(item) for item in runs[:RUN_HISTORY_MAX]]  # newest first


def _read_session(path: Path, out: Imported) -> None:
    try:
        text = read_text(path)
    except FileNotFoundError:
        return
    except UnicodeDecodeError as exc:
        out.notes.append(f"{path.name} is not valid text ({exc.reason}); not imported")
        return
    except OSError as exc:
        raise StateDatabaseError(
            path,
            f"Cannot migrate {path}: {exc.strerror or exc}. Your files were not changed.",
        ) from exc
    try:
        json.loads(text)
    except json.JSONDecodeError:
        out.notes.append(f"{path.name} is not valid JSON; not imported")
        return
    modified = datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.UTC)
    out.session = (text, modified.isoformat(timespec="seconds"))


def _discover() -> list[Path]:
    """Existing legacy sources, in rename order (``history`` last)."""
    found: list[Path] = []
    for name in (*_SIMPLE_FILES, *_CACHE_FILES):
        path = paths.data_dir() / name
        if path.is_file():
            found.append(path)
    found.extend(paths.legacy_category_cache_files())
    history = paths.legacy_history_dir()
    if history.is_dir():
        found.append(history)
    return found


def parse_sources() -> tuple[Imported, list[Path]]:
    """Read every legacy source (no database access)."""
    sources = _discover()
    out = Imported()
    data_dir = paths.data_dir()
    history = paths.legacy_history_dir()
    if history in sources:
        _parse_history(history, out)
    out.seen = _parse_asin_list(data_dir / "seen_asins.json", out)
    out.dismissed = _parse_asin_list(data_dir / "dismissed_asins.json", out)
    eligibility = data_dir / "refresh_eligibility.json"
    if eligibility.is_file():
        _parse_eligibility(eligibility, out)
    elif out.history and out.seen:
        _backfill_eligibility(out)
    _parse_monitor_state(data_dir / "monitor_state.json", out)
    _read_session(data_dir / "last_results.json", out)
    _parse_notify(data_dir / "notify_state.json", out)
    _parse_track_state(data_dir / "track_state.json", out)
    return out, sources


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

_PRODUCTS_FROM_HISTORY = """
INSERT OR REPLACE INTO products (asin, locale, title, last_price, last_seen)
SELECT h.asin, h.locale,
       COALESCE((SELECT t.title FROM price_history t
                  WHERE t.asin = h.asin AND t.locale = h.locale AND t.title <> ''
                  ORDER BY t.date DESC LIMIT 1), ''),
       h.price, h.date
  FROM price_history h
 WHERE h.date = (SELECT MAX(m.date) FROM price_history m
                  WHERE m.asin = h.asin AND m.locale = h.locale)
"""


def _write(conn: sqlite3.Connection, data: Imported) -> dict[str, int]:
    from audible_deals.db import chunked

    for batch in chunked(data.history, 1000):
        conn.executemany(
            "INSERT OR REPLACE INTO price_history (asin, locale, date, price, title) "
            "VALUES (?, ?, ?, ?, ?)",
            batch,
        )
    conn.execute(_PRODUCTS_FROM_HISTORY)
    conn.executemany(
        "INSERT OR IGNORE INTO seen (asin) VALUES (?)",
        [(a,) for a in sorted(data.seen)],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO dismissed (asin) VALUES (?)",
        [(a,) for a in sorted(data.dismissed)],
    )
    conn.executemany(
        "INSERT INTO refresh_eligibility (locale, asin, surfaced_on) VALUES (?, ?, ?) "
        "ON CONFLICT (locale, asin) DO UPDATE SET surfaced_on = MAX(surfaced_on, excluded.surfaced_on)",
        data.eligibility,
    )
    products = 0
    for name, snapshot in data.monitors.items():
        conn.execute(
            "INSERT OR REPLACE INTO monitor_state (name, initialized, last_success, last_error) "
            "VALUES (?, ?, ?, ?)",
            (
                name,
                int(snapshot["initialized"]),
                snapshot["last_success"],
                snapshot["last_error"],
            ),
        )
        rows = [
            (
                name,
                asin,
                float(p["price"]) if _is_number(p.get("price")) else None,
                # json.loads accepted NaN/Infinity in the old file, so store what was there
                json.dumps(p),
            )
            for asin, p in snapshot["products"].items()
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO monitor_products (monitor, asin, price, data) VALUES (?, ?, ?, ?)",
            rows,
        )
        products += len(rows)
    if data.session is not None:
        conn.execute(
            "INSERT OR REPLACE INTO result_sessions (slot, body, saved_at) VALUES (1, ?, ?)",
            data.session,
        )
    conn.executemany(
        "INSERT OR REPLACE INTO notify_state (asin, price, date) VALUES (?, ?, ?)",
        data.notify,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO refresh_cursors (locale, surfaced_on, asin) VALUES (?, ?, ?)",
        data.refresh_cursors,
    )
    for key, value in data.track_values.items():
        conn.execute(
            "INSERT OR REPLACE INTO track_state (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )
    for run in reversed(data.runs):  # oldest first, so ids grow with time
        insert_track_run(conn, run)
    return {
        "history_entries": int(
            conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        ),
        "history_asins": int(
            conn.execute("SELECT COUNT(DISTINCT asin) FROM price_history").fetchone()[0]
        ),
        "seen": len(data.seen),
        "dismissed": len(data.dismissed),
        "refresh_eligibility": len(data.eligibility),
        "monitors": len(data.monitors),
        "monitor_products": products,
        "result_session": int(data.session is not None),
        "notify_state": len(data.notify),
        "track_runs": len(data.runs),
    }


# ---------------------------------------------------------------------------
# Renaming
# ---------------------------------------------------------------------------


def _migrated_name(source: Path) -> Path:
    target = source.with_name(source.name + _SUFFIX)
    counter = 0
    while target.exists():
        counter += 1
        target = source.with_name(f"{source.name}{_SUFFIX}.{counter}")
    return target


def _rename_all(sources: list[Path]) -> list[Path]:
    """Rename each source to ``*.migrated``; return those that could not be."""
    failed: list[Path] = []
    for source in sources:
        if not source.exists():
            continue
        try:
            os.rename(source, _migrated_name(source))
        except OSError as exc:
            logger.warning("could not rename %s after migrating it: %s", source, exc)
            failed.append(source)
    return failed


def _retry_renames(conn: sqlite3.Connection) -> None:
    try:
        names = json.loads(meta_get(conn, META_RENAMES) or "[]")
    except json.JSONDecodeError:
        names = []
    pending = [paths.data_dir() / str(name) for name in names]
    failed = _rename_all(pending)
    with transaction(conn):
        if failed:
            meta_set(conn, META_RENAMES, json.dumps([p.name for p in failed]))
        else:
            conn.execute("DELETE FROM meta WHERE key = ?", (META_RENAMES,))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _import(conn: sqlite3.Connection) -> tuple[dict[str, Any] | None, list[Path]]:
    """Read everything, then write it and the marker in one transaction."""
    data, sources = parse_sources()
    with transaction(conn):
        if meta_get(conn, META_KEY) is not None:  # lost a race despite the lock
            return None, []
        counts = _write(conn, data) if sources else {}
        summary = {
            "at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "sources": [source.name for source in sources],
            "counts": counts,
            "skipped": data.skipped,
            "notes": data.notes,
        }
        meta_set(conn, META_KEY, json.dumps(summary, ensure_ascii=False))
    return summary, sources


def ensure_imported(conn: sqlite3.Connection) -> None:
    """Import the legacy files if that has not happened. Call under the migration lock."""
    if meta_get(conn, META_KEY) is not None:
        if meta_get(conn, META_RENAMES) is not None:
            _retry_renames(conn)
        return
    try:
        summary, sources = _import(conn)
    except StateDatabaseError:
        raise
    except Exception as exc:
        # Nothing was committed and no file was touched: the next run retries.
        raise StateDatabaseError(
            paths.state_db_file(),
            f"Could not move your saved data into state.db: {type(exc).__name__}: {exc}. "
            "Your files were not changed; please report this.",
        ) from exc
    if summary is None:  # another process finished the import first
        return
    failed = _rename_all(sources)
    if failed:
        with transaction(conn):
            meta_set(conn, META_RENAMES, json.dumps([p.name for p in failed]))
    if sources:
        _announce(summary, failed)


def _announce(summary: dict[str, Any], failed: list[Path]) -> None:
    counts = summary["counts"]
    logger.log(
        NOTE,
        "Moved your saved data into state.db (%d price entries for %d titles, %d seen, "
        "%d dismissed); the old files were renamed to *%s and can be deleted once you "
        "are happy.",
        counts.get("history_entries", 0),
        counts.get("history_asins", 0),
        counts.get("seen", 0),
        counts.get("dismissed", 0),
        _SUFFIX,
    )
    for note in summary["notes"]:
        logger.warning("Migration: %s", note)
    for what, count in summary["skipped"].items():
        logger.warning(
            "Migration skipped %d unusable %s; 'deals doctor' has the details",
            count,
            what,
        )
    if failed:
        logger.warning(
            "%d old file(s) could not be renamed yet; this will be retried next time",
            len(failed),
        )


def summary(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The recorded result of the import, or ``None`` if it has not run."""
    raw = meta_get(conn, META_KEY)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    pending = meta_get(conn, META_RENAMES)
    data["renames_pending"] = json.loads(pending) if pending else []
    return data
