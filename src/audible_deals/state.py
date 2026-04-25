"""Persistent state management for audible-deals.

Handles reading and writing of wishlist, profiles, config, seen ASINs,
last results cache, and price history files.
"""

from __future__ import annotations

import datetime
import json as json_mod
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
    PROFILES_FILE,
    SEEN_ASINS_FILE,
    WISHLIST_FILE,
)

# ---------------------------------------------------------------------------
# Wishlist
# ---------------------------------------------------------------------------


def load_wishlist() -> list[dict]:
    if WISHLIST_FILE.exists():
        try:
            data = json_mod.loads(WISHLIST_FILE.read_text())
            if isinstance(data, list):
                return data
        except (json_mod.JSONDecodeError, KeyError):
            pass
    return []


def save_wishlist(items: list[dict]) -> None:
    _atomic_write(WISHLIST_FILE, json_mod.dumps(items, indent=2, ensure_ascii=False))


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
    if PROFILES_FILE.exists():
        try:
            data = json_mod.loads(PROFILES_FILE.read_text())
            if isinstance(data, dict):
                return data
        except (json_mod.JSONDecodeError, KeyError):
            pass
    return {}


def save_profiles(profiles: dict[str, dict]) -> None:
    _atomic_write(PROFILES_FILE, json_mod.dumps(profiles, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Global defaults config
# ---------------------------------------------------------------------------


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            data = json_mod.loads(CONFIG_FILE.read_text())
            if isinstance(data, dict):
                return data
        except (json_mod.JSONDecodeError, KeyError, OSError):
            pass
    return {}


def save_config(cfg: dict) -> None:
    _atomic_write(CONFIG_FILE, json_mod.dumps(cfg, indent=2, ensure_ascii=False))


def coerce_config_value(key: str, raw: str):
    """Coerce a raw string value to the type declared in _CONFIG_SCHEMA."""
    typ = _CONFIG_SCHEMA[key]
    if typ is bool:
        if raw.lower() in ("true", "1", "yes"):
            return True
        elif raw.lower() in ("false", "0", "no"):
            return False
        raise click.ClickException(f"Invalid boolean value for '{key}': {raw!r}. Use true/false.")
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
        raise click.ClickException(f"Invalid value for '{key}' (expected {typ.__name__}): {e}")


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
    try:
        data = json_mod.loads(SEEN_ASINS_FILE.read_text())
        if isinstance(data, list):
            return set(data)
    except (json_mod.JSONDecodeError, OSError, KeyError, TypeError):
        pass
    return set()


def save_seen_asins(new_asins: set[str]) -> None:
    """Append ASINs to the cumulative seen-ASINs file."""
    if not new_asins:
        return
    existing = load_seen_asins()
    if new_asins <= existing:
        return
    merged = sorted(existing | new_asins)
    try:
        _atomic_write(SEEN_ASINS_FILE, json_mod.dumps(merged))
    except Exception:
        pass


def merge_seen_asins(skip_asins: set[str] | None, exclude_seen: bool) -> set[str] | None:
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


def resolve_last_references(refs: tuple[int, ...]) -> list[tuple[str, str]]:
    """Convert 1-indexed position references to (asin, description) tuples from the last results cache."""
    title, data = load_last_results()
    results: list[tuple[str, str]] = []
    for ref in refs:
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

_history_dir_created = False


def record_prices(products: list[Product]) -> None:
    """Append today's prices to per-ASIN history files.

    Batches writes: reads all existing files, updates in-memory,
    then writes only changed files.
    """
    global _history_dir_created
    priced = [p for p in products if p.price is not None]
    if not priced:
        return
    if not _history_dir_created:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        _history_dir_created = True

    today = datetime.date.today().isoformat()
    to_write: dict[Path, list[dict]] = {}

    for p in priced:
        if not _ASIN_RE.fullmatch(p.asin):
            continue
        hist_file = HISTORY_DIR / f"{p.asin}.json"
        entries: list[dict] = []
        if hist_file.exists():
            try:
                entries = json_mod.loads(hist_file.read_text())
            except json_mod.JSONDecodeError:
                entries = []
        if entries and entries[-1].get("date") == today:
            continue
        entries.append({"date": today, "price": round(p.price, 2), "title": p.title})
        to_write[hist_file] = entries[-365:]

    for path, entries in to_write.items():
        _atomic_write(path, json_mod.dumps(entries))


# ---------------------------------------------------------------------------
# Last results cache — write / clear
# ---------------------------------------------------------------------------


def save_last_results(title: str, serialized: list[dict]) -> None:
    """Write serialized products to the last-results cache."""
    cache_obj = {"title": title, "results": serialized}
    _atomic_write(LAST_RESULTS_FILE, json_mod.dumps(cache_obj, ensure_ascii=False))


def clear_last_results() -> bool:
    """Delete the last-results cache. Returns True if deleted."""
    try:
        LAST_RESULTS_FILE.unlink()
        return True
    except FileNotFoundError:
        return False


def clear_seen_asins() -> bool:
    """Delete the cumulative seen-ASINs file. Returns True if deleted."""
    try:
        SEEN_ASINS_FILE.unlink()
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
        return []


def scan_price_changes(
    days: int,
) -> tuple[list[tuple[str, str, float, float]], list[tuple[str, str, float]]]:
    """Scan history files for price drops and newly tracked items.

    Returns (drops, new_items) where:
      drops = [(asin, title, old_price, new_price), ...]
      new_items = [(asin, title, current_price), ...]
    """
    if not HISTORY_DIR.exists():
        return [], []

    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    drops: list[tuple[str, str, float, float]] = []
    new_items: list[tuple[str, str, float]] = []

    for hist_file in HISTORY_DIR.glob("*.json"):
        asin = hist_file.stem
        try:
            entries = json_mod.loads(hist_file.read_text())
        except json_mod.JSONDecodeError:
            continue
        if not entries:
            continue

        title = ""
        for e in reversed(entries):
            if e.get("title"):
                title = e["title"]
                break

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

    return drops, new_items


def find_wishlist_hits() -> list[dict]:
    """Find wishlist items whose latest tracked price is at or below target.

    Returns matching wishlist entry dicts.
    """
    wishlist_items = load_wishlist()
    hits: list[dict] = []
    for item in wishlist_items:
        if not _ASIN_RE.fullmatch(item.get("asin", "")):
            continue
        entries = load_price_history(item["asin"])
        if entries and item.get("max_price") is not None and entries[-1]["price"] <= item["max_price"]:
            hits.append(item)
    return hits
