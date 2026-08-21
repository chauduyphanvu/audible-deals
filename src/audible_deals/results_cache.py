"""Atomic result-session and cumulative seen-ASIN persistence."""

from __future__ import annotations

import json
import logging
from typing import Any

import click

from audible_deals import constants
from audible_deals import result_models
from audible_deals.storage import _atomic_write, load_json_file

logger = logging.getLogger(__name__)


def load_seen_asins() -> set[str]:
    """Load cumulative seen ASINs for exclusion."""
    return set(load_json_file(constants.SEEN_ASINS_FILE, list, "seen ASINs"))


def save_seen_asins(new_asins: set[str]) -> None:
    """Append ASINs to the cumulative seen-ASINs file."""
    if not new_asins:
        return
    existing = load_seen_asins()
    if new_asins <= existing:
        logger.debug("save_seen_asins: no new asins (%d already seen)", len(existing))
        return
    merged = sorted(existing | new_asins)
    try:
        _atomic_write(constants.SEEN_ASINS_FILE, json.dumps(merged))
        logger.debug(
            "saved seen ASINs (%d total, +%d new)",
            len(merged),
            len(merged) - len(existing),
        )
    except Exception:
        logger.warning(
            "failed to write seen-asins at %s", constants.SEEN_ASINS_FILE, exc_info=True
        )


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


def clear_seen_asins() -> bool:
    """Delete the cumulative seen-ASINs file. Returns True if deleted."""
    try:
        constants.SEEN_ASINS_FILE.unlink()
        logger.debug("cleared seen-asins: %s", constants.SEEN_ASINS_FILE)
        return True
    except FileNotFoundError:
        return False


def _read_cache_data() -> Any:
    if not constants.LAST_RESULTS_FILE.exists():
        raise click.ClickException(
            "No cached results found. Run 'deals find', 'deals search', "
            "'deals for-me', or 'deals series' first."
        )
    try:
        return json.loads(constants.LAST_RESULTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise click.ClickException(f"Could not read last results cache: {exc}")


def load_result_session() -> result_models.ResultSession:
    """Load and validate a result session, normalizing both legacy cache shapes."""
    try:
        return result_models.ResultSession.from_dict(_read_cache_data())
    except result_models.ResultSessionValidationError as exc:
        raise click.ClickException(str(exc)) from None


def save_result_session(session: result_models.ResultSession) -> None:
    """Atomically persist a complete result session."""
    _atomic_write(
        constants.LAST_RESULTS_FILE,
        json.dumps(session.to_dict(), ensure_ascii=False),
    )
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
    cache_obj = {"title": title, "results": serialized}
    _atomic_write(
        constants.LAST_RESULTS_FILE, json.dumps(cache_obj, ensure_ascii=False)
    )


def update_session_view(ordered_asins: list[str], *, sort: str | None = None) -> None:
    """Persist the visible selector order and an optional interactive sort key."""
    session = load_result_session()
    session.visible_asins = list(ordered_asins)
    if sort is not None:
        session.current_recipe = result_models.RecipePatch(sort=sort).merge(
            session.current_recipe
        )
    save_result_session(session)


def clear_last_results() -> bool:
    """Delete the last-results cache. Returns True if deleted."""
    try:
        constants.LAST_RESULTS_FILE.unlink()
        logger.debug("cleared last results cache: %s", constants.LAST_RESULTS_FILE)
        return True
    except FileNotFoundError:
        return False
