"""Per-marketplace, per-ASIN price history and analysis, stored in ``state.db``."""

from __future__ import annotations

import datetime
import json
import logging
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from audible_deals import db, wishlist
from audible_deals.constants import _ASIN_RE
from audible_deals.product import Product

logger = logging.getLogger(__name__)

# Price entries as consumed by analysis code: read-only, so both the mutable
# lists loaded from the database and the frozen copies stored on result objects
# fit.
HistoryEntries = Sequence[Mapping[str, Any]]
HistoryMap = Mapping[str, HistoryEntries]

_MAX_HISTORY_ENTRIES = 365


def history_key(asin: str, locale: str) -> str:
    """Return the in-memory key for a marketplace-specific ASIN history."""
    return f"{locale}:{asin}"


def _as_float(value) -> float | None:
    """Return value as a float if it is numeric, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _entry(date: str, price: float, title: str) -> dict:
    return {"date": date, "price": price, "title": title}


def _observation_day(observation_date: datetime.date | str | None) -> str:
    if observation_date is None:
        return datetime.date.today().isoformat()
    if isinstance(observation_date, datetime.datetime):
        return observation_date.date().isoformat()
    if isinstance(observation_date, datetime.date):
        return observation_date.isoformat()
    if isinstance(observation_date, str):
        return datetime.date.fromisoformat(observation_date).isoformat()
    raise TypeError("observation_date must be a date or ISO date string")


_TRIM = """
DELETE FROM price_history
 WHERE asin = :asin AND locale = :locale
   AND date < (SELECT date FROM price_history
                WHERE asin = :asin AND locale = :locale
                ORDER BY date DESC LIMIT 1 OFFSET :keep)
"""

_UPSERT_PRODUCT = """
INSERT INTO products (asin, locale, title, last_price, last_seen)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (asin, locale) DO UPDATE SET
    title = CASE
        WHEN excluded.title <> ''
         AND (products.title = '' OR excluded.last_seen >= products.last_seen)
        THEN excluded.title ELSE products.title END,
    last_price = CASE
        WHEN excluded.last_seen >= products.last_seen
        THEN excluded.last_price ELSE products.last_price END,
    last_seen = MAX(products.last_seen, excluded.last_seen)
"""


def _product_json(product: Product) -> str | None:
    """The product as the index stores it (``None`` if it cannot be encoded)."""
    try:
        # Product fields are plain values and lists of strings, so its __dict__
        # serializes as it is (dataclasses.asdict deep-copies it, 3x slower).
        return json.dumps(vars(product), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return None


def record_prices(
    products: list[Product], observation_date: datetime.date | str | None = None
) -> None:
    """Append observed prices to the history (one entry per ASIN, marketplace and day).

    The first price seen on a day wins; later sightings the same day are
    ignored. Each ASIN keeps its newest 365 entries per marketplace. The
    newest sighting of each product is also kept in the product index.
    """
    observed_on = _observation_day(observation_date)
    priced = [(p, price) for p in products if (price := _as_float(p.price)) is not None]
    if not priced:
        logger.debug("record_prices: no priced products (input=%d)", len(products))
        return

    rows = []
    bad_asin = 0
    for p, price in priced:
        if not _ASIN_RE.fullmatch(p.asin):
            bad_asin += 1
            continue
        rows.append((p, round(price, 2)))

    written = 0
    with db.transaction(db.state()) as conn:
        for p, price in rows:
            asin, locale, title = p.asin, p.locale, p.title or ""
            cursor = conn.execute(
                "INSERT OR IGNORE INTO price_history (asin, locale, date, price, title) "
                "VALUES (?, ?, ?, ?, ?)",
                (asin, locale, observed_on, price, title),
            )
            if not cursor.rowcount:
                continue  # already recorded for this day
            written += 1
            conn.execute(
                _TRIM,
                {"asin": asin, "locale": locale, "keep": _MAX_HISTORY_ENTRIES - 1},
            )
            previous = conn.execute(
                "SELECT last_seen FROM products WHERE asin = ? AND locale = ?",
                (asin, locale),
            ).fetchone()
            newest = (
                previous is None or previous[0] is None or observed_on >= previous[0]
            )
            conn.execute(_UPSERT_PRODUCT, (asin, locale, title, price, observed_on))
            if newest and (data := _product_json(p)) is not None:
                conn.execute(
                    "INSERT INTO product_index (locale, asin, data) VALUES (?, ?, ?) "
                    "ON CONFLICT (locale, asin) DO UPDATE SET data = excluded.data",
                    (locale, asin, data),
                )
                conn.execute(
                    "DELETE FROM product_categories WHERE locale = ? AND asin = ?",
                    (locale, asin),
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO product_categories (locale, asin, category_id) "
                    "VALUES (?, ?, ?)",
                    [(locale, asin, cid) for cid in p.category_ids if cid],
                )

    logger.debug(
        "record_prices: priced=%d wrote=%d skipped_today=%d bad_asin=%d",
        len(priced),
        written,
        len(rows) - written,
        bad_asin,
    )


def has_price_history(locale: str = "us") -> bool:
    """Return whether *locale* has at least one recorded price."""
    row = (
        db.state()
        .execute(
            "SELECT EXISTS (SELECT 1 FROM price_history WHERE locale = ?)", (locale,)
        )
        .fetchone()
    )
    return bool(row[0])


def load_price_history(asin: str, locale: str = "us") -> list[dict]:
    """Load the price history entries (oldest first) for one ASIN and marketplace."""
    rows = db.state().execute(
        "SELECT date, price, title FROM price_history "
        "WHERE asin = ? AND locale = ? ORDER BY date",
        (asin, locale),
    )
    return [_entry(*row) for row in rows]


def load_price_histories(
    keys: Iterable[tuple[str, str]],
) -> dict[str, list[dict]]:
    """Load many histories in a few queries.

    *keys* are ``(asin, locale)`` pairs. The result maps :func:`history_key` to
    the entries, with an empty list for pairs that have no history.
    """
    wanted: dict[str, set[str]] = {}
    for asin, locale in keys:
        wanted.setdefault(locale, set()).add(asin)
    result: dict[str, list[dict]] = {
        history_key(asin, locale): []
        for locale, asins in wanted.items()
        for asin in asins
    }
    conn = db.state()
    for locale, asins in wanted.items():
        for batch in db.chunked(sorted(asins), 500):
            marks = ",".join("?" * len(batch))
            rows = conn.execute(
                "SELECT asin, date, price, title FROM price_history "  # noqa: S608
                f"WHERE locale = ? AND asin IN ({marks}) ORDER BY asin, date",
                (locale, *batch),
            )
            for asin, date, price, title in rows:
                result[history_key(asin, locale)].append(_entry(date, price, title))
    return result


def load_product_histories(products: Iterable[Product]) -> dict[str, list[dict]]:
    """Histories for the priced *products*, keyed by :func:`history_key`."""
    return load_price_histories(
        (product.asin, product.locale)
        for product in products
        if product.price is not None
    )


def _numeric_prices(entries: HistoryEntries) -> list[float]:
    """Extract the numeric prices from history entries, in order."""
    return [
        price
        for entry in entries
        if (price := _as_float(entry.get("price"))) is not None
    ]


def _latest_title(entries: HistoryEntries) -> str:
    """Return the title from the most recent entry that has one."""
    for e in reversed(entries):
        if isinstance(e.get("title"), str) and e["title"]:
            return e["title"]
    return ""


def _atl_latest(entries: HistoryEntries) -> tuple[float, float] | None:
    """Return (latest, prev_min) when the latest price is at the all-time low.

    Requires ≥2 numeric entries and a numeric latest price; returns None
    otherwise, or when the latest price is above the previous minimum.
    """
    if not entries:
        return None
    last_price = entries[-1].get("price")
    latest = _as_float(last_price)
    if latest is None:
        return None
    prices = _numeric_prices(entries)
    if len(prices) < 2:
        return None
    prev_min = min(prices[:-1])
    if latest > prev_min:
        return None
    return latest, prev_min


def price_history_context(
    products: list[Product], histories: HistoryMap | None = None
) -> tuple[set[str], dict[str, int]]:
    """Compute (atl_asins, hist_context) for priced products from their histories.

    atl_asins: products at or below all previously recorded prices (ignores today's entry).
    hist_context: percent of current price vs the historical median (≥3 entries).
    """
    atl_asins: set[str] = set()
    hist_context: dict[str, int] = {}
    today_iso = datetime.date.today().isoformat()
    for p in products:
        if p.price is None:
            continue
        entries = _history_entries(p, histories)
        prior = [e for e in entries if e.get("date") != today_iso]
        numeric_prior = _numeric_prices(prior)
        numeric_all = _numeric_prices(entries)
        if numeric_prior and p.price <= min(numeric_prior):
            atl_asins.add(p.asin)
        if len(numeric_all) >= 3:
            median = statistics.median(numeric_all)
            if median > 0:
                hist_context[p.asin] = round((p.price - median) / median * 100)
    return atl_asins, hist_context


def _history_entries(p: Product, histories: HistoryMap | None) -> HistoryEntries:
    """Entries from a preloaded map when given, else from disk."""
    if histories is not None:
        return histories.get(history_key(p.asin, p.locale), [])
    return load_price_history(p.asin, p.locale)


def hist_percentiles(
    products: list[Product], histories: HistoryMap | None = None
) -> dict[str, int]:
    """Percentile rank (0–100) of current price within its history (≥5 entries required)."""
    result: dict[str, int] = {}
    for p in products:
        if p.price is None:
            continue
        numeric = _numeric_prices(_history_entries(p, histories))
        if len(numeric) < 5:
            continue
        result[p.asin] = round(
            100 * sum(1 for h in numeric if h < p.price) / len(numeric)
        )
    return result


def price_drop_pcts(
    products: list[Product], histories: HistoryMap | None = None
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
        entries = _history_entries(p, histories)
        prior = [e for e in entries if e.get("date") != today_iso]
        numeric = _numeric_prices(prior)
        if not numeric:
            continue
        last = numeric[-1]
        if last <= 0:
            continue
        result[p.asin] = (last - p.price) / last * 100
    return result


def _scan_changes(
    histories: Mapping[str, HistoryEntries],
    cutoff: str,
    titles: Mapping[str, str] | None = None,
) -> tuple[list[tuple[str, str, float, float]], list[tuple[str, str, float]]]:
    drops: list[tuple[str, str, float, float]] = []
    new_items: list[tuple[str, str, float]] = []

    for asin, entries in histories.items():
        title = (titles or {}).get(asin) or _latest_title(entries)
        recent = [
            e for e in entries if isinstance(e.get("date"), str) and e["date"] >= cutoff
        ]
        if not recent:
            continue

        first_date = entries[0].get("date") if entries else None
        all_within_window = (
            isinstance(first_date, str)
            and first_date >= cutoff
            and len(entries) == len(recent)
        )
        if all_within_window:
            if len(entries) >= 2:
                old_price = _as_float(entries[0].get("price"))
                new_price = _as_float(entries[-1].get("price"))
                if (
                    old_price is not None
                    and new_price is not None
                    and new_price < old_price
                ):
                    drops.append((asin, title, old_price, new_price))
                elif new_price is not None:
                    new_items.append((asin, title, new_price))
                continue
            last_price = _as_float(entries[-1].get("price"))
            if last_price is not None:
                new_items.append((asin, title, last_price))
            continue

        before = [
            e for e in entries if isinstance(e.get("date"), str) and e["date"] < cutoff
        ]
        if before and recent:
            old_price = _as_float(before[-1].get("price"))
            new_price = _as_float(recent[-1].get("price"))
            if (
                old_price is not None
                and new_price is not None
                and new_price < old_price
            ):
                drops.append((asin, title, old_price, new_price))
    return drops, new_items


# What `_scan_changes` needs from each ASIN that has an entry inside the window:
# how many entries there are, the first and last of them, and the newest entry
# before the window (if any). One row per ASIN instead of every entry.
_WINDOW_SUMMARY = """
WITH bounds AS (
    SELECT asin, COUNT(*) AS n, MIN(date) AS first_date, MAX(date) AS last_date
      FROM price_history WHERE locale = :locale AND date >= :cutoff GROUP BY asin
)
SELECT b.asin, b.n, b.first_date,
       (SELECT price FROM price_history
         WHERE locale = :locale AND asin = b.asin AND date = b.first_date),
       b.last_date,
       (SELECT price FROM price_history
         WHERE locale = :locale AND asin = b.asin AND date = b.last_date),
       prior.date,
       (SELECT price FROM price_history
         WHERE locale = :locale AND asin = b.asin AND date = prior.date)
  FROM bounds b
  LEFT JOIN (SELECT asin, MAX(date) AS date FROM price_history
              WHERE locale = :locale AND date < :cutoff GROUP BY asin) prior
    ON prior.asin = b.asin
"""


def _window_entries(
    rows: Iterable[tuple],
) -> dict[str, list[dict]]:
    """Stand-in histories that give `_scan_changes` the same answers as the full ones.

    The comparison only looks at the newest entry before the window, or else at
    the first and last entries inside it, so those are all that is rebuilt.
    """
    windowed: dict[str, list[dict]] = {}
    for (
        asin,
        count,
        first_date,
        first_price,
        last_date,
        last_price,
        prior_date,
        prior_price,
    ) in rows:
        if prior_date is not None:
            entries = [
                _entry(prior_date, prior_price, ""),
                _entry(last_date, last_price, ""),
            ]
        elif count >= 2:
            entries = [
                _entry(first_date, first_price, ""),
                _entry(last_date, last_price, ""),
            ]
        else:
            entries = [_entry(first_date, first_price, "")]
        windowed[asin] = entries
    return windowed


def scan_price_changes(
    days: int,
    histories: Mapping[str, HistoryEntries] | None = None,
    locale: str = "us",
) -> tuple[list[tuple[str, str, float, float]], list[tuple[str, str, float]]]:
    """Scan price history for drops and newly tracked items within *days*.

    Without *histories* the database is queried for just the window plus each
    ASIN's last price before it, which is all the comparison needs. Passing
    preloaded *histories* runs the same comparison over them.
    Returns (drops, new_items) where:
      drops = [(asin, title, old_price, new_price), ...]
      new_items = [(asin, title, current_price), ...]
    """
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    if histories is not None:
        drops, new_items = _scan_changes(histories, cutoff)
    else:
        conn = db.state()
        with db.transaction(conn, write=False):
            summary = conn.execute(
                _WINDOW_SUMMARY, {"locale": locale, "cutoff": cutoff}
            ).fetchall()
            titles = dict(
                conn.execute(
                    "SELECT asin, title FROM products WHERE locale = ?", (locale,)
                )
            )
        windowed = _window_entries(summary)
        drops, new_items = _scan_changes(windowed, cutoff, titles)
    logger.debug(
        "scan_price_changes days=%d drops=%d new=%d",
        days,
        len(drops),
        len(new_items),
    )
    return drops, new_items


def load_all_price_histories(locale: str = "us") -> dict[str, list[dict]]:
    """Load the price history of every ASIN tracked in *locale*, keyed by ASIN."""
    result: dict[str, list[dict]] = {}
    for asin, date, price, title in db.state().execute(
        "SELECT asin, date, price, title FROM price_history "
        "WHERE locale = ? ORDER BY asin, date",
        (locale,),
    ):
        result.setdefault(asin, []).append(_entry(date, price, title))
    return result


def purge_stale_history(
    days: int,
    dry_run: bool = False,
    locale: str = "us",
    asins: list[str] | None = None,
) -> tuple[int, list[str]]:
    """Delete the history of ASINs whose most recent entry is older than *days* days.

    Only the given marketplace is considered. Returns (count, asins_affected).
    """
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    conn = db.state()
    with db.transaction(conn, write=not dry_run):
        stale = [
            row[0]
            for row in conn.execute(
                "SELECT asin FROM price_history WHERE locale = ? "
                "GROUP BY asin HAVING MAX(date) < ? ORDER BY asin",
                (locale, cutoff),
            )
        ]
        if asins is not None:
            wanted = set(asins)
            stale = [asin for asin in stale if asin in wanted]
        if not dry_run:
            for batch in db.chunked(stale, 500):
                marks = ",".join("?" * len(batch))
                for table in ("price_history", "products"):
                    conn.execute(
                        f"DELETE FROM {table} WHERE locale = ? AND asin IN ({marks})",  # noqa: S608
                        (locale, *batch),
                    )
    logger.debug(
        "purge_stale_history days=%d dry_run=%s removed=%d", days, dry_run, len(stale)
    )
    return len(stale), stale


def _wishlist_with_history(locale: str):
    """Yield (item, price history entries) for wishlist items with valid ASINs."""
    asin_items, _ = wishlist.partition_wishlist(wishlist.load_wishlist())
    histories = load_price_histories(
        (item["asin"], item.get("locale", locale)) for item in asin_items
    )
    for item in asin_items:
        yield item, histories[history_key(item["asin"], item.get("locale", locale))]


def find_wishlist_hits(locale: str = "us") -> list[dict]:
    """Find wishlist items whose latest tracked price is at or below target.

    Returns matching wishlist entry dicts.
    """
    hits: list[dict] = []
    for item, entries in _wishlist_with_history(locale):
        last = entries[-1].get("price") if entries else None
        max_price = item.get("max_price")
        last_price = _as_float(last)
        target = _as_float(max_price)
        if last_price is not None and target is not None and last_price <= target:
            hits.append(item)
    return hits


def find_wishlist_atl_hits(locale: str = "us") -> list[dict]:
    """Find wishlist items whose latest tracked price is at their all-time low.

    Requires ≥2 numeric history entries and the chronologically-latest entry
    to have a numeric price. Returns list of dicts with keys: asin, title, price, target.
    """
    hits: list[dict] = []
    for item, entries in _wishlist_with_history(locale):
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


_ATL_ROWS = """
SELECT asin, price, prev_min FROM (
    SELECT l.asin AS asin, h.price AS price,
           (SELECT MIN(p.price) FROM price_history p
             WHERE p.asin = l.asin AND p.locale = :locale AND p.date < l.last_date) AS prev_min
      FROM (SELECT asin, MAX(date) AS last_date FROM price_history
             WHERE locale = :locale GROUP BY asin HAVING COUNT(*) >= 2) l
      JOIN price_history h
        ON h.asin = l.asin AND h.locale = :locale AND h.date = l.last_date
)
WHERE price <= prev_min
ORDER BY prev_min - price DESC, asin
"""


def find_all_atl_hits(
    limit: int = 20,
    histories: Mapping[str, HistoryEntries] | None = None,
    locale: str = "us",
) -> list[dict]:
    """Find all tracked ASINs whose latest price is at their all-time low.

    Requires ≥2 numeric history entries and the latest entry to have a numeric
    price. Target is filled from the wishlist when the ASIN is present, else None.
    Accepts preloaded histories; otherwise the database is asked directly.
    Returns up to *limit* dicts sorted by drop magnitude (how far below the
    previous minimum) descending.
    """
    items, _ = wishlist.partition_wishlist(wishlist.load_wishlist())
    wishlist_by_asin = {item["asin"]: item for item in items}

    scored: list[tuple[float, dict]] = []
    if histories is None:
        conn = db.state()
        with db.transaction(conn, write=False):
            rows = conn.execute(_ATL_ROWS, {"locale": locale}).fetchmany(limit)
            titles = dict(
                conn.execute(
                    "SELECT asin, title FROM products WHERE locale = ?", (locale,)
                )
            )
        candidates = [
            (asin, latest, prev_min, titles.get(asin, ""))
            for asin, latest, prev_min in rows
        ]
    else:
        candidates = []
        for asin, entries in histories.items():
            atl = _atl_latest(entries)
            if atl is not None:
                candidates.append((asin, atl[0], atl[1], _latest_title(entries)))
    for asin, latest, prev_min, title in candidates:
        wl_item = wishlist_by_asin.get(asin)
        scored.append(
            (
                prev_min - latest,
                {
                    "asin": asin,
                    "title": title,
                    "price": latest,
                    "target": wl_item.get("max_price") if wl_item else None,
                },
            )
        )

    scored.sort(key=lambda x: x[0], reverse=True)
    return [hit for _, hit in scored[:limit]]
