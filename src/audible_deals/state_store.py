"""Automation state in ``state.db``: monitor baselines, notify cooldowns, tracking.

These are the records that background tracking reads and writes between runs.
Monitors' *definitions* stay in ``monitors.json`` (the user edits them); only
what a run learns lives here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from audible_deals import db
from audible_deals.automation_models import MonitorSnapshot

logger = logging.getLogger(__name__)

RUN_HISTORY_MAX = 10

# ---------------------------------------------------------------------------
# Monitor baselines
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonitorSummary:
    """A monitor's health without its (potentially large) product list."""

    initialized: bool
    last_success: str | None
    last_error: str | None
    product_count: int


def load_monitor_snapshot(name: str) -> MonitorSnapshot:
    """The saved baseline for *name*, or an empty uninitialized one."""
    conn = db.state()
    with db.transaction(conn, write=False):
        row = conn.execute(
            "SELECT initialized, last_success, last_error FROM monitor_state WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return MonitorSnapshot()
        products = {
            asin: json.loads(data)
            for asin, data in conn.execute(
                "SELECT asin, data FROM monitor_products WHERE monitor = ?", (name,)
            )
        }
    return MonitorSnapshot(
        initialized=bool(row[0]),
        products=products,
        last_success=row[1],
        last_error=row[2],
    )


def monitor_summaries() -> dict[str, MonitorSummary]:
    """Health of every monitor that has saved state, keyed by name."""
    rows = db.state().execute(
        "SELECT s.name, s.initialized, s.last_success, s.last_error, "
        "       (SELECT COUNT(*) FROM monitor_products p WHERE p.monitor = s.name) "
        "  FROM monitor_state s"
    )
    return {
        name: MonitorSummary(bool(initialized), last_success, last_error, count)
        for name, initialized, last_success, last_error, count in rows
    }


def save_monitor_snapshot(name: str, snapshot: MonitorSnapshot) -> None:
    """Replace the baseline for *name* atomically."""
    with db.transaction(db.state()) as conn:
        conn.execute("DELETE FROM monitor_products WHERE monitor = ?", (name,))
        conn.execute(
            "INSERT INTO monitor_state (name, initialized, last_success, last_error) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET "
            "initialized = excluded.initialized, last_success = excluded.last_success, "
            "last_error = excluded.last_error",
            (
                name,
                int(snapshot.initialized),
                snapshot.last_success,
                snapshot.last_error,
            ),
        )
        for batch in db.chunked(list(snapshot.products.items()), 500):
            conn.executemany(
                "INSERT INTO monitor_products (monitor, asin, price, data) VALUES (?, ?, ?, ?)",
                [
                    (
                        name,
                        asin,
                        float(product["price"])
                        if isinstance(product.get("price"), (int, float))
                        and not isinstance(product.get("price"), bool)
                        else None,
                        json.dumps(product, ensure_ascii=False, allow_nan=False),
                    )
                    for asin, product in batch
                ],
            )


def record_monitor_failure(name: str, message: str) -> None:
    """Remember the latest error for *name* without touching its baseline."""
    with db.transaction(db.state()) as conn:
        conn.execute(
            "INSERT INTO monitor_state (name, initialized, last_error) VALUES (?, 0, ?) "
            "ON CONFLICT (name) DO UPDATE SET last_error = excluded.last_error",
            (name, message),
        )


def delete_monitor_state(name: str) -> None:
    """Forget *name*'s baseline (its products go with it)."""
    with db.transaction(db.state()) as conn:
        conn.execute("DELETE FROM monitor_state WHERE name = ?", (name,))


# ---------------------------------------------------------------------------
# Notification cooldowns
# ---------------------------------------------------------------------------


def load_notify_state() -> dict[str, dict[str, Any]]:
    """Last alert per ASIN: ``{asin: {"price": float, "date": "YYYY-MM-DD"}}``."""
    rows = db.state().execute("SELECT asin, price, date FROM notify_state")
    return {asin: {"price": price, "date": date} for asin, price, date in rows}


def save_notify_state(state: dict[str, dict[str, Any]]) -> None:
    """Replace the cooldown records with *state*."""
    rows = []
    for asin, entry in state.items():
        price = entry.get("price") if isinstance(entry, dict) else None
        date = entry.get("date") if isinstance(entry, dict) else None
        if (
            isinstance(price, (int, float))
            and not isinstance(price, bool)
            and isinstance(date, str)
        ):
            rows.append((asin, float(price), date))
    with db.transaction(db.state()) as conn:
        conn.execute("DELETE FROM notify_state")
        conn.executemany(
            "INSERT INTO notify_state (asin, price, date) VALUES (?, ?, ?)", rows
        )


# ---------------------------------------------------------------------------
# Tracking state
# ---------------------------------------------------------------------------

RUN_COLUMNS = (
    "at",
    "duration_s",
    "wishlist_checked",
    "author_watches_checked",
    "extra_tracked_checked",
    "hits",
    "suppressed",
    "webhook_sent",
    "monitors_checked",
    "monitors_scheduled",
    "monitor_events",
    "monitor_failures",
    "error",
)


def insert_track_run(conn: Any, record: dict[str, Any]) -> None:
    """Insert one run record; keys it lacks are stored as NULL."""
    values: dict[str, Any] = {column: record.get(column) for column in RUN_COLUMNS}
    if values["webhook_sent"] is not None:
        values["webhook_sent"] = int(bool(values["webhook_sent"]))
    if values["monitor_failures"] is not None:
        values["monitor_failures"] = json.dumps(list(values["monitor_failures"]))
    conn.execute(
        f"INSERT INTO track_runs ({', '.join(RUN_COLUMNS)}) "  # noqa: S608
        f"VALUES ({', '.join(':' + column for column in RUN_COLUMNS)})",
        values,
    )


def _run_dict(row: tuple) -> dict[str, Any]:
    record = {
        column: value
        for column, value in zip(RUN_COLUMNS, row, strict=True)
        if value is not None
    }
    record.setdefault("error", None)
    if "webhook_sent" in record:
        record["webhook_sent"] = bool(record["webhook_sent"])
    if "monitor_failures" in record:
        try:
            record["monitor_failures"] = json.loads(record["monitor_failures"])
        except json.JSONDecodeError:
            record["monitor_failures"] = []
    return record


@dataclass
class TrackState:
    """What ``deals track run`` carries from one run to the next."""

    refresh_cursors: dict[str, dict[str, str]] = field(default_factory=dict)
    monitor_cursor: int = 0
    auth_error_notified: bool = False
    #: Recent runs as plain records, newest first.
    runs: list[dict[str, Any]] = field(default_factory=list)

    def add_run(self, record: dict[str, Any]) -> None:
        self.runs.insert(0, record)
        del self.runs[RUN_HISTORY_MAX:]


def _track_value(conn: Any, key: str) -> Any:
    row = conn.execute("SELECT value FROM track_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


def load_track_state() -> TrackState:
    conn = db.state()
    with db.transaction(conn, write=False):
        cursors = {
            locale: {"surfaced_on": surfaced_on, "asin": asin}
            for locale, surfaced_on, asin in conn.execute(
                "SELECT locale, surfaced_on, asin FROM refresh_cursors"
            )
        }
        cursor = _track_value(conn, "monitor_cursor")
        notified = _track_value(conn, "auth_error_notified")
        runs = [
            _run_dict(row)
            for row in conn.execute(
                f"SELECT {', '.join(RUN_COLUMNS)} FROM track_runs "  # noqa: S608
                "ORDER BY id DESC LIMIT ?",
                (RUN_HISTORY_MAX,),
            )
        ]
    return TrackState(
        refresh_cursors=cursors,
        monitor_cursor=cursor if isinstance(cursor, int) else 0,
        auth_error_notified=bool(notified),
        runs=runs,
    )


def save_track_state(state: TrackState) -> None:
    """Persist cursors, flags and run history (the install record is separate)."""
    with db.transaction(db.state()) as conn:
        conn.execute("DELETE FROM refresh_cursors")
        conn.executemany(
            "INSERT INTO refresh_cursors (locale, surfaced_on, asin) VALUES (?, ?, ?)",
            [
                (locale, cursor["surfaced_on"], cursor["asin"])
                for locale, cursor in state.refresh_cursors.items()
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO track_state (key, value) VALUES ('monitor_cursor', ?)",
            (json.dumps(int(state.monitor_cursor)),),
        )
        if state.auth_error_notified:
            conn.execute(
                "INSERT OR REPLACE INTO track_state (key, value) VALUES ('auth_error_notified', 'true')"
            )
        else:
            conn.execute("DELETE FROM track_state WHERE key = 'auth_error_notified'")
        conn.execute("DELETE FROM track_runs")
        for record in reversed(state.runs[:RUN_HISTORY_MAX]):  # oldest first
            insert_track_run(conn, record)


def load_install_record() -> dict[str, Any] | None:
    """The record ``deals track install`` saved, if any."""
    value = _track_value(db.state(), "install")
    return value if isinstance(value, dict) else None


def save_install_record(record: dict[str, Any] | None) -> None:
    """Set (or with ``None`` clear) the install record, leaving the rest alone."""
    with db.transaction(db.state()) as conn:
        if record is None:
            conn.execute("DELETE FROM track_state WHERE key = 'install'")
        else:
            conn.execute(
                "INSERT OR REPLACE INTO track_state (key, value) VALUES ('install', ?)",
                (json.dumps(record, ensure_ascii=False, allow_nan=False),),
            )
