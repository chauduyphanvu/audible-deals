"""The foreground loop behind ``deals track run --every`` and ``deals watch --every``.

For people without a scheduler (or on a machine that is always on): run one
check, wait, run it again. The loop knows nothing about what a check is;
everything it needs is passed in, so tests drive it with a fake clock.

* A check that finds the shared run lock held (a scheduled ``track run``,
  ``notify`` or ``recap`` is working) is skipped, not counted as a failure: the
  loop just waits its normal interval.
* A check that fails is reported and retried with exponential backoff and
  jitter: the wait doubles with each consecutive failure, up to one hour (or
  the interval itself when that is longer), and any success resets it.
* Ctrl-C and SIGTERM end the loop cleanly.
"""

from __future__ import annotations

import contextlib
import random
import signal
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TypeVar

from audible_deals.locking import LockHeldError

T = TypeVar("T")

#: The longest wait caused by failures alone.
BACKOFF_CAP_SECONDS = 3600.0

#: Jitter keeps many machines from retrying together; it only ever shortens
#: the wait, so the cap really is a cap.
_JITTER_LOW = 0.8


def backoff_seconds(
    failures: int, interval_s: float, *, cap_s: float = BACKOFF_CAP_SECONDS
) -> float:
    """The wait (before jitter) after *failures* failures in a row.

    The first failure retries after one interval, then 2x, 4x, ... until the
    cap. An interval longer than the cap is never shortened by a failure.
    """
    if failures < 1:
        return float(interval_s)
    ceiling = max(float(interval_s), cap_s)
    return min(ceiling, float(interval_s) * 2 ** min(failures - 1, 40))


@dataclass(frozen=True)
class LoopClock:
    """Time, as the loop uses it (replaced in tests)."""

    sleep: Callable[[float], None] = time.sleep
    uniform: Callable[[float, float], float] = random.uniform


def _noop(*args: object) -> None:
    return None


def run_every(
    run_once: Callable[[], T],
    *,
    interval_s: float,
    clock: LoopClock | None = None,
    on_result: Callable[[T], None] = _noop,
    on_busy: Callable[[Exception], None] = _noop,
    on_failure: Callable[[Exception, int, float], None] = _noop,
    on_wait: Callable[[float, bool], None] = _noop,
    max_runs: int | None = None,
) -> int:
    """Call *run_once* every *interval_s* seconds until interrupted.

    ``on_result(result)`` follows a successful check, ``on_busy(error)`` a check
    skipped because the run lock was held, ``on_failure(error, in_a_row, wait)`` a
    failed one, and ``on_wait(seconds, backing_off)`` runs before each sleep.
    Returns the number of checks made (it stops by itself only after *max_runs*).
    """
    clock = clock or LoopClock()
    failures = 0
    runs = 0
    try:
        while True:
            try:
                result = run_once()
            except LockHeldError as exc:
                wait = float(interval_s)
                on_busy(exc)
            except Exception as exc:
                failures += 1
                wait = backoff_seconds(failures, interval_s) * clock.uniform(
                    _JITTER_LOW, 1.0
                )
                on_failure(exc, failures, wait)
            else:
                failures = 0
                wait = float(interval_s)
                on_result(result)
            runs += 1
            if max_runs is not None and runs >= max_runs:
                return runs
            on_wait(wait, failures > 0)
            clock.sleep(wait)
    except KeyboardInterrupt:
        return runs


@contextlib.contextmanager
def stop_on_sigterm() -> Iterator[None]:
    """Make SIGTERM (a service manager stopping the loop) behave like Ctrl-C."""
    if threading.current_thread() is not threading.main_thread() or not hasattr(
        signal, "SIGTERM"
    ):
        yield
        return

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def human_seconds(seconds: float) -> str:
    """``45s``, ``12m``, ``1h 5m``."""
    total = max(0, round(seconds))
    if total < 60:
        return f"{total}s"
    minutes, _ = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"
