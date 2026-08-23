"""Measure cached selector resolution against a representative result session."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audible_deals import constants  # noqa: E402
from audible_deals.selectors import resolve_selectors  # noqa: E402


def _session(candidate_count: int) -> dict:
    candidates = [
        {
            "asin": f"B{index:09d}",
            "title": f"Representative Audiobook {index}",
            "locale": "us",
            "payload": "x" * 480,
        }
        for index in range(candidate_count)
    ]
    return {
        "version": 2,
        "producer": "find",
        "locale": "us",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "title": "Representative cached results",
        "source": {"command": "deals find --deep"},
        "candidates": candidates,
        "baseline_recipe": {},
        "current_recipe": {},
        "visible_asins": [item["asin"] for item in candidates],
        "constraints": {},
        "ranking_context": {},
        "legacy": False,
    }


def _measure(selectors: tuple[str, ...], runs: int, warmups: int) -> dict[str, float]:
    for _ in range(warmups):
        resolve_selectors(selectors)
    samples = []
    for _ in range(runs):
        started = time.perf_counter_ns()
        resolve_selectors(selectors)
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    ordered = sorted(samples)
    return {
        "median_ms": statistics.median(ordered),
        "min_ms": ordered[0],
        "p95_ms": ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=int, default=3000)
    parser.add_argument("--selectors", type=int, default=20)
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if (
        args.candidates < 1
        or args.selectors < 1
        or args.selectors > args.candidates
        or args.runs < 1
        or args.warmups < 0
    ):
        parser.error("counts must be positive and selectors cannot exceed candidates")

    with tempfile.TemporaryDirectory(prefix="audible-selector-benchmark-") as root:
        constants.LAST_RESULTS_FILE = Path(root) / "last_results.json"
        constants.LAST_RESULTS_FILE.write_text(json.dumps(_session(args.candidates)))
        many = tuple(f"@{index}" for index in range(1, args.selectors + 1))
        results = {
            "single": _measure(("@1",), args.runs, args.warmups),
            "many": _measure(many, args.runs, args.warmups),
        }

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
        return
    print(
        f"{args.candidates} cached candidates · {args.selectors} selectors · "
        f"{args.runs} measured runs"
    )
    for name, result in results.items():
        print(
            f"{name:<8} median {result['median_ms']:>8.2f}ms · "
            f"min {result['min_ms']:>8.2f}ms · p95 {result['p95_ms']:>8.2f}ms"
        )


if __name__ == "__main__":
    main()
