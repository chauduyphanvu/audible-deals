"""Compare concurrent library pagination with the former serial implementation."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audible_deals.client import DealsClient, _parse_api_products  # noqa: E402
from audible_deals.constants import (  # noqa: E402
    CATALOG_RESPONSE_GROUPS,
    MAX_PAGE_SIZE,
)


_DEFAULT_CASES = (
    (1, False),
    (2, False),
    (3, False),
    (20, False),
    (20, True),
    (100, False),
)


class _DelayedLibraryTransport:
    def __init__(self, total_results: int, latency_ms: float):
        self.total_results = total_results
        self.latency_s = latency_ms / 1_000
        self.request_count = 0
        self.max_concurrency = 0
        self._active = 0
        self._lock = threading.Lock()

    def request(self, path: str, **params) -> dict:
        assert path == "1.0/library"
        page = params["page"]
        with self._lock:
            self.request_count += 1
            self._active += 1
            self.max_concurrency = max(self.max_concurrency, self._active)
        try:
            time.sleep(self.latency_s)
            start = (page - 1) * MAX_PAGE_SIZE
            remaining = max(0, self.total_results - start)
            count = min(MAX_PAGE_SIZE, remaining)
            items = [
                {"asin": f"B{start + index:09d}", "title": "Benchmark Book"}
                for index in range(count)
            ]
            return {"items": items, "total_results": self.total_results}
        finally:
            with self._lock:
                self._active -= 1

    def cancel(self) -> None:
        pass

    def reset_abort(self) -> None:
        pass


def _serial_library_pages(client: DealsClient) -> Iterator[tuple[list, int]]:
    page = 1
    while True:
        response = client._transport.request(
            "1.0/library",
            num_results=MAX_PAGE_SIZE,
            page=page,
            response_groups=CATALOG_RESPONSE_GROUPS,
        )
        items = response.get("items", [])
        yield _parse_api_products(items, client.locale), page
        if len(items) < MAX_PAGE_SIZE:
            return
        page += 1


def _run(
    page_count: int,
    latency_ms: float,
    *,
    exact_final_page: bool,
    serial: bool,
) -> tuple[float, int, int]:
    total_results = (
        page_count * MAX_PAGE_SIZE
        if exact_final_page
        else (page_count - 1) * MAX_PAGE_SIZE + 1
    )
    transport = _DelayedLibraryTransport(total_results, latency_ms)
    client = object.__new__(DealsClient)
    client.locale = "us"
    client._transport = transport
    pages = _serial_library_pages(client) if serial else client.get_library_pages()

    started = time.perf_counter_ns()
    products = [product for page, _ in pages for product in page]
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    if len(products) != total_results:
        raise RuntimeError(
            f"expected {total_results} products, received {len(products)}"
        )
    return elapsed_ms, transport.request_count, transport.max_concurrency


def _summarize(measured: list[tuple[float, int, int]]) -> dict[str, float | int]:
    samples = sorted(item[0] for item in measured)
    return {
        "mean_ms": statistics.fmean(samples),
        "median_ms": statistics.median(samples),
        "min_ms": samples[0],
        "p95_ms": samples[max(0, math.ceil(len(samples) * 0.95) - 1)],
        "stdev_ms": statistics.stdev(samples) if len(samples) > 1 else 0.0,
        "requests": measured[-1][1],
        "max_concurrency": measured[-1][2],
    }


def _measure_case(
    page_count: int,
    latency_ms: float,
    runs: int,
    warmups: int,
    *,
    exact_final_page: bool,
) -> dict:
    run_args = (page_count, latency_ms)
    run_kwargs = {"exact_final_page": exact_final_page}
    for _ in range(warmups):
        _run(*run_args, **run_kwargs, serial=True)
        _run(*run_args, **run_kwargs, serial=False)

    serial_measured = []
    concurrent_measured = []
    for index in range(runs):
        if index % 2:
            concurrent_measured.append(_run(*run_args, **run_kwargs, serial=False))
            serial_measured.append(_run(*run_args, **run_kwargs, serial=True))
        else:
            serial_measured.append(_run(*run_args, **run_kwargs, serial=True))
            concurrent_measured.append(_run(*run_args, **run_kwargs, serial=False))

    serial_result = _summarize(serial_measured)
    concurrent_result = _summarize(concurrent_measured)
    serial_median = float(serial_result["median_ms"])
    concurrent_median = float(concurrent_result["median_ms"])
    return {
        "serial": serial_result,
        "concurrent": concurrent_result,
        "median_reduction_pct": (serial_median - concurrent_median)
        / serial_median
        * 100,
    }


def _case_name(page_count: int, exact_final_page: bool) -> str:
    page_label = "page" if page_count == 1 else "pages"
    ending = "exact" if exact_final_page else "partial"
    return f"{page_count}-{page_label}-{ending}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pages",
        type=int,
        action="append",
        help="Page count to benchmark; repeat for a custom matrix.",
    )
    parser.add_argument(
        "--include-exact",
        action="store_true",
        help="Also measure cases whose final page is exactly full.",
    )
    parser.add_argument("--latency-ms", type=float, default=10.0)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.latency_ms < 0 or args.runs < 1 or args.warmups < 0:
        parser.error("runs must be positive and latency/warmups must be non-negative")
    if args.pages is not None and any(page_count < 1 for page_count in args.pages):
        parser.error("page counts must be positive")

    cases = list(
        _DEFAULT_CASES if args.pages is None else ((page, False) for page in args.pages)
    )
    if args.include_exact:
        page_counts = args.pages or [page_count for page_count, _ in cases]
        cases.extend((page_count, True) for page_count in page_counts)
    cases = list(dict.fromkeys(cases))
    results = {
        _case_name(page_count, exact): _measure_case(
            page_count,
            args.latency_ms,
            args.runs,
            args.warmups,
            exact_final_page=exact,
        )
        for page_count, exact in cases
    }

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
        return

    print(f"{args.latency_ms:.1f}ms transport latency · {args.runs} measured runs")
    print(f"{'case':<20} {'serial':>11} {'concurrent':>12} {'reduction':>11}")
    for name, result in results.items():
        print(
            f"{name:<20} "
            f"{result['serial']['median_ms']:>9.2f}ms "
            f"{result['concurrent']['median_ms']:>10.2f}ms "
            f"{result['median_reduction_pct']:>10.2f}%"
        )


if __name__ == "__main__":
    main()
