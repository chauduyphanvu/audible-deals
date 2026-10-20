"""Result-session and cumulative seen/dismissed-ASIN persistence in ``state.db``."""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any

import click

from audible_deals import db, result_models

logger = logging.getLogger(__name__)


def load_seen_asins() -> set[str]:
    """Load cumulative seen ASINs for exclusion."""
    return {row[0] for row in db.state().execute("SELECT asin FROM seen")}


def save_seen_asins(new_asins: set[str]) -> None:
    """Add ASINs to the cumulative seen set (best effort: a failure only warns)."""
    if not new_asins:
        return
    try:
        with db.transaction(db.state()) as conn:
            cursor = conn.executemany(
                "INSERT OR IGNORE INTO seen (asin) VALUES (?)",
                [(asin,) for asin in sorted(new_asins)],
            )
        logger.debug("saved seen ASINs (+%d new)", cursor.rowcount)
    except Exception:
        logger.warning("failed to record seen ASINs", exc_info=True)


def merge_seen_asins(
    skip_asins: set[str] | None, exclude_seen: bool
) -> set[str] | None:
    """Merge previously-seen ASINs into the skip set when requested."""
    if not exclude_seen:
        return skip_asins
    seen = load_seen_asins()
    if skip_asins is None:
        return seen
    return skip_asins | seen


def _clear(table: str) -> bool:
    with db.transaction(db.state()) as conn:
        removed = conn.execute(f"DELETE FROM {table}").rowcount  # noqa: S608
    return removed > 0


def clear_seen_asins() -> bool:
    """Forget every seen ASIN. Returns True if there was anything to forget."""
    cleared = _clear("seen")
    logger.debug("cleared seen ASINs: %s", cleared)
    return cleared


def load_dismissed_asins() -> set[str]:
    """Load globally dismissed ASINs for exclusion."""
    return {row[0] for row in db.state().execute("SELECT asin FROM dismissed")}


def save_dismissed_asins(new_asins: set[str]) -> None:
    """Add ASINs to the cumulative dismissed set."""
    if not new_asins:
        return
    with db.transaction(db.state()) as conn:
        cursor = conn.executemany(
            "INSERT OR IGNORE INTO dismissed (asin) VALUES (?)",
            [(asin,) for asin in sorted(new_asins)],
        )
    logger.debug("saved dismissed ASINs (+%d new)", cursor.rowcount)


def clear_dismissed_asins() -> bool:
    """Forget every dismissed ASIN. Returns True if there was anything to forget."""
    cleared = _clear("dismissed")
    logger.debug("cleared dismissed ASINs: %s", cleared)
    return cleared


def _read_cache_data() -> Any:
    row = (
        db.state().execute("SELECT body FROM result_sessions WHERE slot = 1").fetchone()
    )
    if row is None:
        raise click.ClickException(
            "No cached results found. Run 'deals find', 'deals search', "
            "'deals for-me', or 'deals series' first."
        )
    try:
        return json.loads(row[0])
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Could not read last results cache: {exc}") from exc


def load_result_session() -> result_models.ResultSession:
    """Load and validate a result session, normalizing both legacy cache shapes."""
    try:
        return result_models.ResultSession.from_dict(_read_cache_data())
    except result_models.ResultSessionValidationError as exc:
        raise click.ClickException(str(exc)) from None


def _store_session(body: str) -> None:
    saved_at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    with db.transaction(db.state()) as conn:
        conn.execute(
            "INSERT INTO result_sessions (slot, body, saved_at) VALUES (1, ?, ?) "
            "ON CONFLICT (slot) DO UPDATE SET body = excluded.body, saved_at = excluded.saved_at",
            (body, saved_at),
        )


def save_result_session(session: result_models.ResultSession) -> None:
    """Atomically persist a complete result session."""
    _store_session(json.dumps(session.to_dict(), ensure_ascii=False, allow_nan=False))
    logger.debug(
        "saved result session producer=%s candidates=%d visible=%d",
        session.producer,
        len(session.candidates),
        len(session.visible_asins),
    )


def load_last_results() -> tuple[str, list[dict]]:
    """Compatibility reader returning the current, limit-applied session view."""
    session = load_result_session()
    return session.title, session.visible_results


def save_last_results(title: str, serialized: list[dict]) -> None:
    """Compatibility writer for callers that only have a limited legacy result list."""
    _store_session(
        json.dumps(
            {"title": title, "results": serialized}, ensure_ascii=False, allow_nan=False
        )
    )


def update_session_view(ordered_asins: list[str], *, sort: str | None = None) -> None:
    """Persist the visible selector order and an optional interactive sort key."""
    with db.transaction(db.state()):
        session = load_result_session()
        session.visible_asins = list(ordered_asins)
        if sort is not None:
            session.current_recipe = result_models.RecipePatch(sort=sort).merge(
                session.current_recipe
            )
        save_result_session(session)


def clear_last_results() -> bool:
    """Delete the last-results cache. Returns True if there was one."""
    cleared = _clear("result_sessions")
    logger.debug("cleared last results cache: %s", cleared)
    return cleared
