"""Cold scan versus cached re-scan against a fake transport with per-request latency.

Runs ``execute_catalog_scan`` (the same code ``deals find``/``search`` use) for a
single-sort and a ``--deep`` plan:

* no cache      what the scan cost before the catalog cache existed;
* cold cache    an empty cache: every page is fetched and stored;
* warm cache    an immediate re-run: every page comes from ``cache.db``;
* refresh       ``--refresh``: every page is fetched again and the entries replaced.

Nothing contacts Audible; the data directory is a temporary one.
"""

from __future__ import annotations

import argparse
import functools
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_HOME = tempfile.mkdtemp(prefix="deals-bench-")
os.environ["DEALS_HOME"] = _HOME

from audible_deals import db, paths  # noqa: E402
from audible_deals.catalog_workflow import (  # noqa: E402
    bind_catalog_categories,
    build_find_scan_plan,
    execute_catalog_scan,
)
from audible_deals.client import DealsClient  # noqa: E402
from audible_deals.constants import MAX_PAGE_SIZE  # noqa: E402
from audible_deals.scan_cache import ScanCache  # noqa: E402


def raw_product(page: int, index: int, sort_by: str) -> dict:
    asin = f"B0{abs(hash((page, index, sort_by))) % 10**8:08d}"
    return {
        "asin": asin,
        "title": f"Title {page}-{index} {sort_by}",
        "subtitle": "A Novel",
        "authors": [{"name": f"Author {index % 40}"}],
        "narrators": [{"name": f"Narrator {index % 25}"}],
        "publisher_name": "Publisher",
        "runtime_length_min": 300 + index * 7,
        "language": "english",
        "release_date": "2024-03-01",
        "price": {
            "lowest_price": {"base": 3.0 + index % 9},
            "list_price": {"base": 19.95},
        },
        "rating": {
            "overall_distribution": {
                "display_average_rating": "4.4",
                "num_ratings": str(100 + index),
            }
        },
        "category_ladders": [
            {"ladder": [{"id": "1", "name": "Fiction"}, {"id": "2", "name": "Fantasy"}]}
        ],
        "series": [{"title": f"Series {index % 12}", "sequence": "1", "asin": "S1"}],
        "plans": [{"plan_name": "US Minerva"}],
    }


class FakeTransport:
    """Answers catalog searches after a fixed delay, like a network round trip."""

    def __init__(self, latency_s: float, total: int = 100_000):
        self.latency_s = latency_s
        self.total = total
        self.requests = 0
        self._lock = threading.Lock()

    def request(self, endpoint: str, **params) -> dict:
        with self._lock:
            self.requests += 1
        time.sleep(self.latency_s)
        page = params.get("page", 0)
        sort_by = params.get("products_sort_by", "")
        return {
            "products": [raw_product(page, i, sort_by) for i in range(MAX_PAGE_SIZE)],
            "total_results": self.total,
        }

    def cancel(self) -> None:
        pass

    def reset_abort(self) -> None:
        pass

    def close(self) -> None:
        pass


def timed(fn):
    start = time.perf_counter()
    result = fn()
    return time.perf_counter() - start, result


def scan(client: DealsClient, pages: int, deep: bool, cache: ScanCache | None) -> int:
    plan = bind_catalog_categories(
        build_find_scan_plan("", pages=pages, deep=deep), [""]
    )
    return len(execute_catalog_scan(client, plan, cache=cache))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pages", type=int, default=10)
    parser.add_argument("--latency-ms", type=float, default=120.0)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    print(
        f"fake transport: {args.latency_ms:g} ms per request, 4 workers, "
        f"{args.pages} pages per sort, median of {args.runs} runs"
    )
    print(f"{'plan':<14}{'case':<14}{'seconds':>9}{'requests':>10}{'products':>10}")
    for deep in (False, True):
        label = "deep" if deep else "single sort"
        client = DealsClient(auth_file=Path(_HOME) / "auth.json")
        transport = FakeTransport(args.latency_ms / 1000)
        client._transport = transport  # type: ignore[assignment]
        rows = []
        for case in ("no cache", "cold cache", "warm cache", "refresh"):
            samples, requests, products = [], 0, 0
            for _ in range(args.runs):
                if case in ("no cache", "cold cache"):
                    db.close_all()
                    for name in ("cache.db", "cache.db-wal", "cache.db-shm"):
                        (Path(_HOME) / name).unlink(missing_ok=True)
                if case == "warm cache":
                    scan(
                        client, args.pages, deep, ScanCache(7200)
                    )  # make sure it is warm
                cache = (
                    None
                    if case == "no cache"
                    else ScanCache(7200, refresh=case == "refresh")
                )
                before = transport.requests
                seconds, products = timed(
                    functools.partial(scan, client, args.pages, deep, cache)
                )
                requests = transport.requests - before
                samples.append(seconds)
            rows.append((case, statistics.median(samples), requests, products))
        for case, seconds, requests, products in rows:
            print(f"{label:<14}{case:<14}{seconds:>9.3f}{requests:>10}{products:>10}")
        cold = next(r for r in rows if r[0] == "cold cache")[1]
        warm = next(r for r in rows if r[0] == "warm cache")[1]
        print(f"{'':<14}warm cache is {cold / warm:.0f}x faster than a cold scan")
    entries, average = (
        db.cache()
        .execute("SELECT COUNT(*), AVG(LENGTH(value)) FROM cache_entries")
        .fetchone()
    )
    db.close_all()  # checkpoints the WAL into the main file
    size = db.file_size(paths.cache_db_file())
    print(
        f"cache.db after the runs: {entries} entries of about "
        f"{average / 1000:.0f} kB each, {size / 1_000_000:.1f} MB on disk"
    )


if __name__ == "__main__":
    main()
