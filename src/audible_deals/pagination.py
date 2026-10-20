"""Page-fetching helpers shared by the catalog, library and wishlist readers.

These know nothing about Audible: they only schedule ``fetch(page)`` calls and
hand results back in page order. The client owns the worker pool (and aborting
requests that are still retrying when a scan is cancelled).
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import TypeVar

from audible_deals.constants import MAX_PAGE_SIZE

#: How many requests the client keeps in flight at once.
MAX_CONCURRENT_FETCHES = 4

T = TypeVar("T")


def page_count(total: int, max_pages: int) -> int:
    """How many pages of ``MAX_PAGE_SIZE`` items to read for a reported *total*.

    An unreported (zero) total means "just the first page".
    """
    return min(max_pages, math.ceil(total / MAX_PAGE_SIZE)) if total else 1


def prefetch_in_order(
    pool: ThreadPoolExecutor,
    fetch: Callable[[int], T],
    pages: Iterable[int],
    *,
    window: int | None = None,
) -> Iterator[tuple[int, T]]:
    """Run ``fetch(page)`` for *pages* on *pool* and yield ``(page, result)`` in order.

    *window* caps how many pages are queued, running or finished-but-unread at
    once (``None`` queues them all). A page that fails raises when its turn
    comes, so a speculative page the caller never reaches cannot fail the scan.
    Stopping early is the caller's ``break``/``close()``; the pool's owner
    cancels whatever is still queued.
    """
    source = iter(pages)
    order: deque[int] = deque()
    pending: dict[Future[T], int] = {}
    ready: dict[int, Future[T]] = {}

    def submit_next() -> bool:
        page = next(source, None)
        if page is None:
            return False
        order.append(page)
        pending[pool.submit(fetch, page)] = page
        return True

    if window is None:
        while submit_next():
            pass
    else:
        for _ in range(window):
            if not submit_next():
                break
    while order:
        page = order.popleft()
        while page not in ready:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                ready[pending.pop(future)] = future
        yield page, ready.pop(page).result()
        submit_next()


def read_serially(
    fetch: Callable[[int], tuple[T, int]], start: int, page_size: int
) -> Iterator[tuple[int, T]]:
    """Read pages one after another until a short page ends the listing.

    ``fetch(page)`` returns ``(result, raw item count)``; a page with fewer
    than *page_size* items is the last one.
    """
    page = start
    while True:
        result, count = fetch(page)
        yield page, result
        if count < page_size:
            return
        page += 1
