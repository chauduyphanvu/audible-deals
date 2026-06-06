"""Per-ASIN price history: recording, analysis, and wishlist/ATL alerts."""

from __future__ import annotations

import datetime
import json
import logging
import statistics
from pathlib import Path

from audible_deals import constants, wishlist
from audible_deals.product import Product
from audible_deals.constants import _ASIN_RE, _atomic_write

logger = logging.getLogger(__name__)

_MAX_HISTORY_ENTRIES = 365


def record_prices(products: list[Product]) -> None:
    """Append today's prices to per-ASIN history files.

    Batches writes: reads all existing files, updates in-memory,
    then writes only changed files.
    """
    priced = [p for p in products if p.price is not None]
    if not priced:
        logger.debug("record_prices: no priced products (input=%d)", len(products))
        return
    constants.HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    today = datetime.date.today().isoformat()
    to_write: dict[Path, list[dict]] = {}
    skipped_today = 0
    bad_asin = 0
    corrupt = 0

    for p in priced:
        if not _ASIN_RE.fullmatch(p.asin):
            bad_asin += 1
            continue
        hist_file = constants.HISTORY_DIR / f"{p.asin}.json"
        entries: list[dict] = []
        if hist_file.exists():
            try:
                entries = json.loads(hist_file.read_text())
            except json.JSONDecodeError:
                logger.warning("history at %s is corrupt, resetting", hist_file)
                corrupt += 1
                entries = []
        if entries and entries[-1].get("date") == today:
            skipped_today += 1
            continue
        entries.append({"date": today, "price": round(p.price, 2), "title": p.title})
        to_write[hist_file] = entries[-_MAX_HISTORY_ENTRIES:]

    for path, entries in to_write.items():
        _atomic_write(path, json.dumps(entries))

    logger.debug(
        "record_prices: priced=%d wrote=%d skipped_today=%d bad_asin=%d corrupt=%d",
        len(priced),
        len(to_write),
        skipped_today,
        bad_asin,
        corrupt,
    )


def has_price_history() -> bool:
    """Return True if the price history directory exists."""
    return constants.HISTORY_DIR.exists()


def load_price_history(asin: str) -> list[dict]:
    """Load price history entries for a single ASIN.

    Returns an empty list if the file doesn't exist or is corrupt.
    """
    hist_file = constants.HISTORY_DIR / f"{asin}.json"
    if not hist_file.exists():
        return []
    try:
        entries = json.loads(hist_file.read_text())
        return entries if isinstance(entries, list) else []
    except (json.JSONDecodeError, OSError):
        logger.warning(
            "price history at %s is corrupt or unreadable", hist_file, exc_info=True
        )
        return []


def _numeric_prices(entries: list[dict]) -> list[float]:
    """Extract the numeric prices from history entries, in order."""
    return [
        float(e["price"]) for e in entries if isinstance(e.get("price"), (int, float))
    ]


def _latest_title(entries: list[dict]) -> str:
    """Return the title from the most recent entry that has one."""
    for e in reversed(entries):
        if e.get("title"):
            return e["title"]
    return ""


def _atl_latest(entries: list[dict]) -> tuple[float, float] | None:
    """Return (latest, prev_min) when the latest price is at the all-time low.

    Requires ≥2 numeric entries and a numeric latest price; returns None
    otherwise, or when the latest price is above the previous minimum.
    """
    if not entries:
        return None
    last_price = entries[-1].get("price")
    if not isinstance(last_price, (int, float)):
        return None
    prices = _numeric_prices(entries)
    if len(prices) < 2:
        return None
    latest = float(last_price)
    prev_min = min(prices[:-1])
    if latest > prev_min:
        return None
    return latest, prev_min


def price_history_context(products: list[Product]) -> tuple[set[str], dict[str, int]]:
    """Compute (atl_asins, hist_context) for priced products from their histories.

    atl_asins: products at or below their all-time tracked low.
    hist_context: percent of current price vs the historical median (≥3 entries).
    """
    atl_asins: set[str] = set()
    hist_context: dict[str, int] = {}
    for p in products:
        if p.price is None:
            continue
        numeric = _numeric_prices(load_price_history(p.asin))
        if not numeric:
            continue
        if p.price <= min(numeric):
            atl_asins.add(p.asin)
        if len(numeric) >= 3:
            median = statistics.median(numeric)
            if median > 0:
                hist_context[p.asin] = round((p.price - median) / median * 100)
    return atl_asins, hist_context


def _history_entries(asin: str, histories: dict[str, list[dict]] | None) -> list[dict]:
    """Entries from a preloaded map when given, else from disk."""
    if histories is not None:
        return histories.get(asin, [])
    return load_price_history(asin)


def hist_percentiles(
    products: list[Product], histories: dict[str, list[dict]] | None = None
) -> dict[str, int]:
    """Percentile rank (0–100) of current price within its history (≥5 entries required)."""
    result: dict[str, int] = {}
    for p in products:
        if p.price is None:
            continue
        numeric = _numeric_prices(_history_entries(p.asin, histories))
        if len(numeric) < 5:
            continue
        result[p.asin] = round(
            100 * sum(1 for h in numeric if h < p.price) / len(numeric)
        )
    return result


def price_drop_pcts(
    products: list[Product], histories: dict[str, list[dict]] | None = None
) -> dict[str, float]:
    """Percent drop from last tracked price (negative means price went up).

    Skips entries dated today so same-day re-runs don't compare a price against itself.
    If all entries are from today, the ASIN is omitted (no reference price available).
    """
    result: dict[str, float] = {}
    today_iso = datetime.date.today().isoformat()
    for p in products:
        if p.price is None:
            continue
        entries = _history_entries(p.asin, histories)
        prior = [e for e in entries if e.get("date") != today_iso]
        numeric = _numeric_prices(prior)
        if not numeric:
            continue
        last = numeric[-1]
        if last <= 0:
            continue
        result[p.asin] = (last - p.price) / last * 100
    return result


def scan_price_changes(
    days: int,
    histories: dict[str, list[dict]] | None = None,
) -> tuple[list[tuple[str, str, float, float]], list[tuple[str, str, float]]]:
    """Scan history files for price drops and newly tracked items.

    Accepts preloaded histories to avoid re-reading the directory.
    Returns (drops, new_items) where:
      drops = [(asin, title, old_price, new_price), ...]
      new_items = [(asin, title, current_price), ...]
    """
    if histories is None:
        histories = load_all_price_histories()

    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    drops: list[tuple[str, str, float, float]] = []
    new_items: list[tuple[str, str, float]] = []

    for asin, entries in histories.items():
        title = _latest_title(entries)
        recent = [e for e in entries if e["date"] >= cutoff]
        if not recent:
            continue

        if entries[0]["date"] >= cutoff and len(entries) == len(recent):
            if len(entries) >= 2:
                old_price = entries[0]["price"]
                new_price = entries[-1]["price"]
                if new_price < old_price:
                    drops.append((asin, title, old_price, new_price))
                continue
            new_items.append((asin, title, entries[-1]["price"]))
            continue

        before = [e for e in entries if e["date"] < cutoff]
        if before and recent:
            old_price = before[-1]["price"]
            new_price = recent[-1]["price"]
            if new_price < old_price:
                drops.append((asin, title, old_price, new_price))

    logger.debug(
        "scan_price_changes days=%d drops=%d new=%d",
        days,
        len(drops),
        len(new_items),
    )
    return drops, new_items


def load_all_price_histories() -> dict[str, list[dict]]:
    """Load price history for every ASIN in the history directory.

    Returns a dict keyed by ASIN; empty ASINs (corrupt or no entries) are skipped.
    Returns {} when the directory doesn't exist.
    """
    if not constants.HISTORY_DIR.exists():
        return {}
    result: dict[str, list[dict]] = {}
    for hist_file in constants.HISTORY_DIR.glob("*.json"):
        entries = load_price_history(hist_file.stem)
        if entries:
            result[hist_file.stem] = entries
    return result


def delete_price_histories(asins: list[str]) -> int:
    """Delete the history files for the given ASINs. Returns the number removed."""
    removed = 0
    for asin in asins:
        try:
            (constants.HISTORY_DIR / f"{asin}.json").unlink()
            removed += 1
        except FileNotFoundError:
            pass
    return removed


def purge_stale_history(days: int, dry_run: bool = False) -> tuple[int, list[str]]:
    """Delete history files whose most-recent entry is older than *days* days ago.

    Corrupt files and files with no parseable last-entry date are skipped.
    Returns (count, asins_affected).
    """
    if not constants.HISTORY_DIR.exists():
        return 0, []

    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    count = 0
    affected: list[str] = []

    for hist_file in constants.HISTORY_DIR.glob("*.json"):
        asin = hist_file.stem
        raw = load_price_history(asin)
        if not raw:
            logger.warning("history at %s has no entries, skipping purge", hist_file)
            continue
        last_date_str = raw[-1].get("date")
        if not last_date_str:
            logger.warning(
                "history at %s has no parseable date, skipping purge", hist_file
            )
            continue
        try:
            last_date = datetime.date.fromisoformat(last_date_str)
        except ValueError:
            logger.warning(
                "history at %s has invalid date %r, skipping purge",
                hist_file,
                last_date_str,
            )
            continue
        if last_date >= cutoff:
            continue
        count += 1
        affected.append(asin)
        if not dry_run:
            try:
                hist_file.unlink()
            except FileNotFoundError:
                pass

    logger.debug(
        "purge_stale_history days=%d dry_run=%s removed=%d", days, dry_run, count
    )
    return count, affected


def _wishlist_with_history():
    """Yield (item, price history entries) for wishlist items with valid ASINs."""
    for item in wishlist.load_wishlist():
        if _ASIN_RE.fullmatch(item.get("asin", "")):
            yield item, load_price_history(item["asin"])


def find_wishlist_hits() -> list[dict]:
    """Find wishlist items whose latest tracked price is at or below target.

    Returns matching wishlist entry dicts.
    """
    hits: list[dict] = []
    for item, entries in _wishlist_with_history():
        if (
            entries
            and item.get("max_price") is not None
            and entries[-1]["price"] <= item["max_price"]
        ):
            hits.append(item)
    return hits


def find_wishlist_atl_hits() -> list[dict]:
    """Find wishlist items whose latest tracked price is at their all-time low.

    Requires ≥2 numeric history entries and the chronologically-latest entry
    to have a numeric price. Returns list of dicts with keys: asin, title, price, target.
    """
    hits: list[dict] = []
    for item, entries in _wishlist_with_history():
        atl = _atl_latest(entries)
        if atl is None:
            continue
        latest, _ = atl
        hits.append(
            {
                "asin": item["asin"],
                "title": item.get("title", "") or _latest_title(entries),
                "price": latest,
                "target": item.get("max_price"),
            }
        )
    return hits


def find_all_atl_hits(
    limit: int = 20, histories: dict[str, list[dict]] | None = None
) -> list[dict]:
    """Find all tracked ASINs whose latest price is at their all-time low.

    Requires ≥2 numeric history entries and the latest entry to have a numeric
    price. Target is filled from the wishlist when the ASIN is present, else None.
    Accepts preloaded histories to avoid re-reading the directory. Returns up to
    *limit* dicts sorted by drop magnitude (how far below the previous minimum)
    descending.
    """
    items = wishlist.load_wishlist()
    wishlist_by_asin = {item["asin"]: item for item in items if "asin" in item}
    if histories is None:
        histories = load_all_price_histories()

    scored: list[tuple[float, dict]] = []
    for asin, entries in histories.items():
        atl = _atl_latest(entries)
        if atl is None:
            continue
        latest, prev_min = atl
        wl_item = wishlist_by_asin.get(asin)
        scored.append(
            (
                prev_min - latest,
                {
                    "asin": asin,
                    "title": _latest_title(entries),
                    "price": latest,
                    "target": wl_item.get("max_price") if wl_item else None,
                },
            )
        )

    scored.sort(key=lambda x: x[0], reverse=True)
    return [hit for _, hit in scored[:limit]]
