"""Last-results cache and cumulative seen-ASINs persistence."""

from __future__ import annotations

import json
import logging

import click

from audible_deals import constants
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
    """Merge previously-seen ASINs into the skip set when --exclude-seen is active."""
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


def load_last_results() -> tuple[str, list[dict]]:
    """Load the last results cache from disk.

    Returns (title, products) where title is the original query context.
    Raises click.ClickException if the cache is missing or corrupt.
    Handles backward compatibility with the old plain-list format.
    """
    if not constants.LAST_RESULTS_FILE.exists():
        raise click.ClickException(
            "No cached results found. Run 'deals find' or 'deals search' first."
        )
    try:
        data = json.loads(constants.LAST_RESULTS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise click.ClickException(f"Could not read last results cache: {e}")
    if isinstance(data, dict) and "results" in data:
        return data.get("title", "Last results"), data["results"]
    # Backward compat: old format is a plain list
    if isinstance(data, list):
        return "Last results", data
    raise click.ClickException("Last results cache is corrupt.")


def save_last_results(title: str, serialized: list[dict]) -> None:
    """Write serialized products to the last-results cache."""
    cache_obj = {"title": title, "results": serialized}
    _atomic_write(
        constants.LAST_RESULTS_FILE, json.dumps(cache_obj, ensure_ascii=False)
    )
    logger.debug(
        "saved last results (%d items, title=%r) to %s",
        len(serialized),
        title,
        constants.LAST_RESULTS_FILE,
    )


def reorder_last_results(ordered_asins: list[str]) -> None:
    """Reorder the last-results cache so entries match the given ASIN order.

    Entries for ASINs in ordered_asins come first (in that order); remaining
    entries follow in their original relative order. Best-effort: silently
    ignores any error.
    """
    try:
        title, data = load_last_results()
        by_asin = {item["asin"]: item for item in data if "asin" in item}
        reordered = [by_asin[a] for a in ordered_asins if a in by_asin]
        seen = set(ordered_asins)
        reordered.extend(item for item in data if item.get("asin") not in seen)
        save_last_results(title, reordered)
    except Exception:
        pass


def clear_last_results() -> bool:
    """Delete the last-results cache. Returns True if deleted."""
    try:
        constants.LAST_RESULTS_FILE.unlink()
        logger.debug("cleared last results cache: %s", constants.LAST_RESULTS_FILE)
        return True
    except FileNotFoundError:
        return False


def _expand_ref_string(ref: str | int, label: str = "--last") -> list[int]:
    """Expand a single ref (int or string like '1-3,5') into a flat list of ints."""
    if isinstance(ref, int):
        return [ref]
    expanded: list[int] = []
    for part in str(ref).split(","):
        part = part.strip()
        if not part:
            raise click.ClickException(f"Invalid {label} value: empty part in {ref!r}.")
        if "-" in part:
            halves = part.split("-", 1)
            try:
                start, end = int(halves[0]), int(halves[1])
            except ValueError:
                raise click.ClickException(
                    f"Invalid {label} range {part!r}: must be two integers separated by '-'."
                )
            if start > end:
                raise click.ClickException(
                    f"Invalid {label} range {part!r}: start must not exceed end."
                )
            if end - start >= 1000:
                raise click.ClickException(
                    f"Invalid {label} range {part!r}: width must be under 1000."
                )
            expanded.extend(range(start, end + 1))
        else:
            try:
                expanded.append(int(part))
            except ValueError:
                raise click.ClickException(
                    f"Invalid {label} value {part!r}: must be an integer or range like '1-3'."
                )
    return expanded


def resolve_last_references(refs: tuple[str | int, ...]) -> list[tuple[str, str]]:
    """Convert 1-indexed position references to (asin, description) tuples from the last results cache."""
    title, data = load_last_results()
    flat: list[int] = []
    for ref in refs:
        flat.extend(_expand_ref_string(ref))
    results: list[tuple[str, str]] = []
    for ref in flat:
        if ref < 1 or ref > len(data):
            raise click.ClickException(
                f"--last {ref} is out of range (cache has {len(data)} result(s))."
            )
        item = data[ref - 1]
        asin = item["asin"]
        item_title = item.get("title", asin)
        desc = f"Result #{ref} from '{title}': {item_title} ({asin})"
        results.append((asin, desc))
    return results
