"""Measure end-to-end latency for representative CLI startup paths."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "version": ("--version",),
    "root-help": ("--help",),
    "config-help": ("config", "--help"),
    "find-help": ("find", "--help"),
    "completions-zsh": ("completions", "zsh"),
}
_EMPTY_CONFIG_RUNNER = (
    "import audible_deals.cli as cli_module; "
    "import audible_deals.config_store as config_store; "
    "config_store.load_config = lambda: {}; "
    "cli_module.load_config = lambda: {}; "
    "cli_module.cli(prog_name='deals')"
)
_ROOT_ONLY_CASES = {"version", "root-help"}


def _run(command: list[str], env: dict[str, str]) -> float:
    started = time.perf_counter_ns()
    subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    return (time.perf_counter_ns() - started) / 1_000_000


def _summarize(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "median_ms": statistics.median(ordered),
        "mean_ms": statistics.fmean(ordered),
        "min_ms": ordered[0],
        "p95_ms": ordered[p95_index],
        "stdev_ms": statistics.stdev(ordered) if len(ordered) > 1 else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--case", action="append", choices=CASES)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.runs < 1 or args.warmups < 0:
        parser.error("--runs must be positive and --warmups must be non-negative")

    selected = args.case or list(CASES)
    command_prefix = [sys.executable, "-m", "audible_deals"]
    env = os.environ.copy()
    source_path = str(ROOT / "src")
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        source_path + os.pathsep + existing_pythonpath
        if existing_pythonpath
        else source_path
    )

    results: dict[str, dict[str, float]] = {}
    for name in selected:
        if name in _ROOT_ONLY_CASES:
            command = [*command_prefix, *CASES[name]]
        else:
            command = [sys.executable, "-c", _EMPTY_CONFIG_RUNNER, *CASES[name]]
        for _ in range(args.warmups):
            _run(command, env)
        results[name] = _summarize([_run(command, env) for _ in range(args.runs)])

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
        return

    print(f"Python {sys.version.split()[0]} · {args.runs} measured runs")
    print(f"{'case':<16} {'median':>10} {'min':>10} {'p95':>10} {'stdev':>10}")
    for name, result in results.items():
        print(
            f"{name:<16} "
            f"{result['median_ms']:>8.2f}ms "
            f"{result['min_ms']:>8.2f}ms "
            f"{result['p95_ms']:>8.2f}ms "
            f"{result['stdev_ms']:>8.2f}ms"
        )


if __name__ == "__main__":
    main()
