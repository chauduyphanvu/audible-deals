"""Persistent state management for audible-deals.

Handles reading and writing of wishlist, profiles, config, seen ASINs,
last results cache, and price history files.
"""

from __future__ import annotations

import datetime
import json as json_mod
import logging
import statistics
from pathlib import Path

import click

from audible_deals.client import Product
from audible_deals.constants import (
    _ASIN_RE,
    _CONFIG_SCHEMA,
    _atomic_write,
    ALL_SORT_OPTIONS,
    CONFIG_FILE,
    HISTORY_DIR,
    LAST_RESULTS_FILE,
    LOCALE_DOMAIN,
    NOTIFY_STATE_FILE,
    PROFILES_FILE,
    SEEN_ASINS_FILE,
    WISHLIST_FILE,
)

logger = logging.getLogger(__name__)


def _load_json_file(path: Path, expected_type: type, desc: str):
    """Load a JSON file, returning an empty expected_type if missing, corrupt, or wrong shape."""
    if path.exists():
        try:
            data = json_mod.loads(path.read_text())
            if isinstance(data, expected_type):
                logger.debug("loaded %s (%d) from %s", desc, len(data), path)
                return data
            logger.warning(
                "%s at %s is not a %s, ignoring", desc, path, expected_type.__name__
            )
        except (json_mod.JSONDecodeError, KeyError, OSError):
            logger.warning("%s at %s is corrupt, ignoring", desc, path, exc_info=True)
    return expected_type()


def _save_json_file(path: Path, data, desc: str) -> None:
    _atomic_write(path, json_mod.dumps(data, indent=2, ensure_ascii=False))
    logger.debug("saved %s (%d) to %s", desc, len(data), path)


# ---------------------------------------------------------------------------
# Wishlist
# ---------------------------------------------------------------------------


def load_wishlist() -> list[dict]:
    return _load_json_file(WISHLIST_FILE, list, "wishlist")


def save_wishlist(items: list[dict]) -> None:
    _save_json_file(WISHLIST_FILE, items, "wishlist")


def partition_wishlist(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split wishlist entries into (asin_items, author_items)."""
    return (
        [i for i in items if i.get("asin")],
        [i for i in items if i.get("type") == "author"],
    )


def wishlist_entry(product: Product, max_price: float | None) -> dict:
    """Build a wishlist dict from a Product."""
    return {
        "asin": product.asin,
        "title": product.title,
        "max_price": max_price,
        "added": datetime.date.today().isoformat(),
    }


# ---------------------------------------------------------------------------
# Saved search profiles
# ---------------------------------------------------------------------------


def load_profiles() -> dict[str, dict]:
    return _load_json_file(PROFILES_FILE, dict, "profiles")


def save_profiles(profiles: dict[str, dict]) -> None:
    _save_json_file(PROFILES_FILE, profiles, "profiles")


# ---------------------------------------------------------------------------
# Global defaults config
# ---------------------------------------------------------------------------


def load_config() -> dict:
    return _load_json_file(CONFIG_FILE, dict, "config")


def save_config(cfg: dict) -> None:
    _save_json_file(CONFIG_FILE, cfg, "config")


# ---------------------------------------------------------------------------
# Notify cooldown state
# ---------------------------------------------------------------------------


def load_notify_state() -> dict:
    return _load_json_file(NOTIFY_STATE_FILE, dict, "notify state")


def save_notify_state(state: dict) -> None:
    _save_json_file(NOTIFY_STATE_FILE, state, "notify state")


def coerce_config_value(key: str, raw: str):
    """Coerce a raw string value to the type declared in _CONFIG_SCHEMA."""
    typ = _CONFIG_SCHEMA[key]
    if typ is bool:
        if raw.lower() in ("true", "1", "yes"):
            return True
        elif raw.lower() in ("false", "0", "no"):
            return False
        raise click.ClickException(
            f"Invalid boolean value for '{key}': {raw!r}. Use true/false."
        )
    if key == "sort":
        if raw not in ALL_SORT_OPTIONS:
            raise click.ClickException(
                f"Invalid sort value '{raw}'. Valid: {', '.join(sorted(ALL_SORT_OPTIONS))}"
            )
        return raw
    if key == "locale":
        if raw not in LOCALE_DOMAIN:
            raise click.ClickException(
                f"Invalid locale '{raw}'. Valid: {', '.join(sorted(LOCALE_DOMAIN))}"
            )
        return raw
    try:
        return typ(raw)
    except (ValueError, TypeError) as e:
        raise click.ClickException(
            f"Invalid value for '{key}' (expected {typ.__name__}): {e}"
        )


def validate_config_key(key: str) -> str:
    """Normalize and validate a config key. Returns the snake_case key or raises."""
    norm = key.replace("-", "_")
    if norm not in _CONFIG_SCHEMA:
        valid = ", ".join(sorted(k.replace("_", "-") for k in _CONFIG_SCHEMA))
        raise click.ClickException(f"Unknown config key '{key}'. Valid keys: {valid}")
    return norm


# ---------------------------------------------------------------------------
# Seen ASINs
# ---------------------------------------------------------------------------


def load_seen_asins() -> set[str]:
    """Load cumulative seen ASINs for exclusion."""
    return set(_load_json_file(SEEN_ASINS_FILE, list, "seen ASINs"))


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
        _atomic_write(SEEN_ASINS_FILE, json_mod.dumps(merged))
        logger.debug(
            "saved seen ASINs (%d total, +%d new)",
            len(merged),
            len(merged) - len(existing),
        )
    except Exception:
        logger.warning(
            "failed to write seen-asins at %s", SEEN_ASINS_FILE, exc_info=True
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


# ---------------------------------------------------------------------------
# Last results cache
# ---------------------------------------------------------------------------


def load_last_results() -> tuple[str, list[dict]]:
    """Load the last results cache from disk.

    Returns (title, products) where title is the original query context.
    Raises click.ClickException if the cache is missing or corrupt.
    Handles backward compatibility with the old plain-list format.
    """
    if not LAST_RESULTS_FILE.exists():
        raise click.ClickException(
            "No cached results found. Run 'deals find' or 'deals search' first."
        )
    try:
        data = json_mod.loads(LAST_RESULTS_FILE.read_text())
    except (json_mod.JSONDecodeError, OSError) as e:
        raise click.ClickException(f"Could not read last results cache: {e}")
    if isinstance(data, dict) and "results" in data:
        return data.get("title", "Last results"), data["results"]
    # Backward compat: old format is a plain list
    if isinstance(data, list):
        return "Last results", data
    raise click.ClickException("Last results cache is corrupt.")


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


# ---------------------------------------------------------------------------
# Price history
# ---------------------------------------------------------------------------

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
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    today = datetime.date.today().isoformat()
    to_write: dict[Path, list[dict]] = {}
    skipped_today = 0
    bad_asin = 0
    corrupt = 0

    for p in priced:
        if not _ASIN_RE.fullmatch(p.asin):
            bad_asin += 1
            continue
        hist_file = HISTORY_DIR / f"{p.asin}.json"
        entries: list[dict] = []
        if hist_file.exists():
            try:
                entries = json_mod.loads(hist_file.read_text())
            except json_mod.JSONDecodeError:
                logger.warning("history at %s is corrupt, resetting", hist_file)
                corrupt += 1
                entries = []
        if entries and entries[-1].get("date") == today:
            skipped_today += 1
            continue
        entries.append({"date": today, "price": round(p.price, 2), "title": p.title})
        to_write[hist_file] = entries[-_MAX_HISTORY_ENTRIES:]

    for path, entries in to_write.items():
        _atomic_write(path, json_mod.dumps(entries))

    logger.debug(
        "record_prices: priced=%d wrote=%d skipped_today=%d bad_asin=%d corrupt=%d",
        len(priced),
        len(to_write),
        skipped_today,
        bad_asin,
        corrupt,
    )


# ---------------------------------------------------------------------------
# Last results cache — write / clear
# ---------------------------------------------------------------------------


def save_last_results(title: str, serialized: list[dict]) -> None:
    """Write serialized products to the last-results cache."""
    cache_obj = {"title": title, "results": serialized}
    _atomic_write(LAST_RESULTS_FILE, json_mod.dumps(cache_obj, ensure_ascii=False))
    logger.debug(
        "saved last results (%d items, title=%r) to %s",
        len(serialized),
        title,
        LAST_RESULTS_FILE,
    )


def clear_last_results() -> bool:
    """Delete the last-results cache. Returns True if deleted."""
    try:
        LAST_RESULTS_FILE.unlink()
        logger.debug("cleared last results cache: %s", LAST_RESULTS_FILE)
        return True
    except FileNotFoundError:
        return False


def clear_seen_asins() -> bool:
    """Delete the cumulative seen-ASINs file. Returns True if deleted."""
    try:
        SEEN_ASINS_FILE.unlink()
        logger.debug("cleared seen-asins: %s", SEEN_ASINS_FILE)
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Price history — read / scan
# ---------------------------------------------------------------------------


def has_price_history() -> bool:
    """Return True if the price history directory exists."""
    return HISTORY_DIR.exists()


def load_price_history(asin: str) -> list[dict]:
    """Load price history entries for a single ASIN.

    Returns an empty list if the file doesn't exist or is corrupt.
    """
    hist_file = HISTORY_DIR / f"{asin}.json"
    if not hist_file.exists():
        return []
    try:
        entries = json_mod.loads(hist_file.read_text())
        return entries if isinstance(entries, list) else []
    except (json_mod.JSONDecodeError, OSError):
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


def _wishlist_with_history():
    """Yield (item, price history entries) for wishlist items with valid ASINs."""
    for item in load_wishlist():
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


def load_all_price_histories() -> dict[str, list[dict]]:
    """Load price history for every ASIN in the history directory.

    Returns a dict keyed by ASIN; empty ASINs (corrupt or no entries) are skipped.
    Returns {} when the directory doesn't exist.
    """
    if not HISTORY_DIR.exists():
        return {}
    result: dict[str, list[dict]] = {}
    for hist_file in HISTORY_DIR.glob("*.json"):
        entries = load_price_history(hist_file.stem)
        if entries:
            result[hist_file.stem] = entries
    return result


def delete_price_histories(asins: list[str]) -> int:
    """Delete the history files for the given ASINs. Returns the number removed."""
    removed = 0
    for asin in asins:
        try:
            (HISTORY_DIR / f"{asin}.json").unlink()
            removed += 1
        except FileNotFoundError:
            pass
    return removed


def purge_stale_history(days: int, dry_run: bool = False) -> tuple[int, list[str]]:
    """Delete history files whose most-recent entry is older than *days* days ago.

    Corrupt files and files with no parseable last-entry date are skipped.
    Returns (count, asins_affected).
    """
    if not HISTORY_DIR.exists():
        return 0, []

    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    count = 0
    affected: list[str] = []

    for hist_file in HISTORY_DIR.glob("*.json"):
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
    wishlist = load_wishlist()
    wishlist_by_asin = {item["asin"]: item for item in wishlist if "asin" in item}
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
