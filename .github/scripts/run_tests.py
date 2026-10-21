"""Run the test suite as one pytest process per CPU, each on its own test files.

pytest-xdist would make every worker import and assertion-rewrite the whole
suite, which on a cold CI runner costs about three times the CPU of a serial
run. Splitting by file keeps that work to a single pass, so the runner's cores
cut the wall time without adding much CPU.

Usage: python .github/scripts/run_tests.py [pytest args...]
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def cpu_count() -> int:
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0))
    return os.cpu_count() or 1


def shard(files: list[Path], count: int) -> list[list[Path]]:
    # Largest file first onto the lightest shard. File size tracks collection
    # and run time closely enough to keep the shards within a second or so.
    shards: list[tuple[int, list[Path]]] = [(0, []) for _ in range(count)]
    for path in sorted(files, key=lambda p: p.stat().st_size, reverse=True):
        i = min(range(count), key=lambda j: shards[j][0])
        size, group = shards[i]
        shards[i] = (size + path.stat().st_size, [*group, path])
    return [group for _, group in shards if group]


def main() -> int:
    # Same discovery rules as pytest's defaults, so no test file is skipped.
    files = sorted(
        {*Path("tests").rglob("test_*.py"), *Path("tests").rglob("*_test.py")}
    )
    if not files:
        print("no test files found under tests/", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        runs = []
        for i, group in enumerate(shard(files, min(cpu_count(), len(files)))):
            log = open(Path(tmp, f"shard{i}.log"), "w+")
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-p",
                    "no:cacheprovider",
                    f"--basetemp={Path(tmp, f'shard{i}')}",
                    *sys.argv[1:],
                    *map(str, group),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            runs.append((proc, log))

        status = 0
        for i, (proc, log) in enumerate(runs):
            proc.wait()
            log.seek(0)
            print(f"===== shard {i + 1}/{len(runs)} =====", flush=True)
            sys.stdout.write(log.read())
            log.close()
            status = status or proc.returncode
    return status


if __name__ == "__main__":
    sys.exit(main())
