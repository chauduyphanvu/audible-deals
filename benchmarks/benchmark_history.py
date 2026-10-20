"""Compare price-history storage costs on a large tracked catalog.

Populates a throwaway data directory with SIZE tracked ASINs and DAYS days of
history, then times a handful of commands the way users run them: each read is
a fresh interpreter (cold start, as a CLI invocation is), reporting wall time
and peak memory.

Run it against any source tree, so two releases can be compared:

    python benchmarks/benchmark_history.py --src src                # this tree
    python benchmarks/benchmark_history.py --src /path/to/old/src   # another one

The tree is selected with PYTHONPATH; the same script works for the JSON-file
storage of 0.11 and the SQLite storage that replaced it.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

POPULATE = r"""
import datetime, sys, time
from audible_deals.price_history import record_prices
from audible_deals.product import Product

size, days = int(sys.argv[1]), int(sys.argv[2])
today = datetime.date.today()
started = time.perf_counter()
for back in range(days - 1, -1, -1):
    day = today - datetime.timedelta(days=back)
    products = []
    for i in range(size):
        # about a fifth of the titles are on sale on any given day
        sale = (i + back) % 5 == 0
        price = round((9.99 + (i % 17)) * (0.4 if sale else 1.0), 2)
        products.append(Product(asin=f"B0{i:08d}", title=f"Title {i}", price=price))
    record_prices(products, observation_date=day)
print(time.perf_counter() - started)
"""

READS = {
    "recap --json": ["recap", "--json"],
    "recap --atl-all --json": ["recap", "--atl-all", "--json"],
    "history <asin> --json": ["history", "B000000042", "--json"],
    "history --all --json": ["history", "--all", "--json"],
}

PROBE = r"""
import io, json, resource, sys, time, contextlib
from click.testing import CliRunner
from audible_deals.cli import cli

args = json.loads(sys.argv[1])
started = time.perf_counter()
result = CliRunner().invoke(cli, args, catch_exceptions=False)
elapsed = time.perf_counter() - started
assert result.exit_code == 0, result.output[:300]
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({"seconds": elapsed, "rss_kb": rss, "bytes": len(result.output)}))
"""


def run_python(code: str, args: list[str], env: dict[str, str]) -> str:
    out = subprocess.run(
        [sys.executable, "-c", code, *args], env=env, capture_output=True, text=True
    )
    if out.returncode:
        raise SystemExit(f"failed: {out.stderr[-600:]}")
    return out.stdout.strip().splitlines()[-1]


def du(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--src", required=True, help="source tree to measure (PYTHONPATH)"
    )
    parser.add_argument("--size", type=int, default=5000, help="tracked ASINs")
    parser.add_argument("--days", type=int, default=30, help="days of history")
    parser.add_argument("--runs", type=int, default=5, help="measured runs per read")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        env = {
            **os.environ,
            "PYTHONPATH": str(Path(args.src).resolve()),
            "HOME": str(home),
            "DEALS_HOME": str(home / "data"),
            "COLUMNS": "200",
        }
        populate = float(run_python(POPULATE, [str(args.size), str(args.days)], env))
        # one more write on a day that already exists: the common daily no-op
        started = time.perf_counter()
        run_python(
            POPULATE.replace("range(days - 1, -1, -1)", "range(0, 1)"),
            [str(args.size), "1"],
            env,
        )
        rewrite = time.perf_counter() - started
        results: dict[str, dict[str, float]] = {}
        for name, command in READS.items():
            run_python(PROBE, [json.dumps(command)], env)  # warm the page cache
            samples = [
                json.loads(run_python(PROBE, [json.dumps(command)], env))
                for _ in range(args.runs)
            ]
            results[name] = {
                "median_s": statistics.median(s["seconds"] for s in samples),
                "peak_rss_mb": max(s["rss_kb"] for s in samples) / 1024,
            }
        report = {
            "asins": args.size,
            "days": args.days,
            "populate_s": populate,
            "same_day_rewrite_process_s": rewrite,
            # SQLite storage uses DEALS_HOME; the JSON-file releases ignored it and used ~/.config
            "bytes_on_disk": max(du(home / "data"), du(home / ".config")),
            "reads": results,
        }
    if args.json:
        print(json.dumps(report, indent=2))
        return
    print(
        f"{args.size} ASINs x {args.days} days: populate {populate:.1f}s, "
        f"{report['bytes_on_disk'] / 1e6:.1f} MB on disk"
    )
    print(f"{'command':<28}{'median':>10}{'peak RSS':>12}")
    for name, row in results.items():
        print(
            f"{name:<28}{row['median_s'] * 1000:>8.0f}ms{row['peak_rss_mb']:>10.0f}MB"
        )


if __name__ == "__main__":
    main()
