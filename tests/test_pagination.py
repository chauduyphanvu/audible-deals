"""Shared page-fetching helpers."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from audible_deals.constants import MAX_PAGE_SIZE
from audible_deals.pagination import page_count, prefetch_in_order, read_serially


class TestPageCount:
    @pytest.mark.parametrize(
        ("total", "max_pages", "expected"),
        [
            (0, 10, 1),  # unreported total: just the first page
            (1, 10, 1),
            (MAX_PAGE_SIZE, 10, 1),
            (MAX_PAGE_SIZE + 1, 10, 2),
            (10 * MAX_PAGE_SIZE, 3, 3),  # capped by the caller's page limit
            (10**30, 4, 4),
        ],
    )
    def test_pages_needed(self, total, max_pages, expected):
        assert page_count(total, max_pages) == expected


class TestPrefetchInOrder:
    def test_yields_results_in_page_order_whatever_the_completion_order(self):
        def fetch(page):
            time.sleep(0.03 * (5 - page))  # later pages finish first
            return f"page-{page}"

        with ThreadPoolExecutor(max_workers=4) as pool:
            got = list(prefetch_in_order(pool, fetch, range(1, 5)))

        assert got == [(page, f"page-{page}") for page in range(1, 5)]

    def test_window_bounds_how_far_ahead_it_fetches(self):
        started = []
        release = threading.Event()

        def fetch(page):
            started.append(page)
            assert release.wait(10)
            return page

        with ThreadPoolExecutor(max_workers=8) as pool:
            pages = prefetch_in_order(pool, fetch, range(1, 20), window=3)
            release.set()
            assert next(pages) == (1, 1)
            # The window held pages 1-3; the fourth is requested only after
            # the caller has come back for more.
            assert max(started) <= 3
            assert [page for page, _ in pages] == list(range(2, 20))

    def test_unbounded_window_starts_every_page_before_the_first_result(self):
        started = []
        all_started = threading.Event()

        def fetch(page):
            started.append(page)
            if len(started) == 5:
                all_started.set()
            assert all_started.wait(10)
            return page

        with ThreadPoolExecutor(max_workers=5) as pool:
            assert list(prefetch_in_order(pool, fetch, range(1, 6))) == [
                (page, page) for page in range(1, 6)
            ]

    def test_a_failing_page_raises_only_when_its_turn_comes(self):
        def fetch(page):
            if page == 3:
                raise RuntimeError("page 3 failed")
            return page

        with ThreadPoolExecutor(max_workers=4) as pool:
            pages = prefetch_in_order(pool, fetch, range(1, 6))
            assert next(pages) == (1, 1)
            assert next(pages) == (2, 2)
            with pytest.raises(RuntimeError, match="page 3 failed"):
                next(pages)

    def test_a_failure_after_an_early_stop_is_never_seen(self):
        def fetch(page):
            if page == 3:
                raise RuntimeError("never read")
            return page

        with ThreadPoolExecutor(max_workers=4) as pool:
            for page, _ in prefetch_in_order(pool, fetch, range(1, 6)):
                if page == 2:
                    break  # the caller found the last page

    def test_does_not_materialize_the_page_range(self):
        # A hostile total_results can describe 10**1000 pages.
        with ThreadPoolExecutor(max_workers=2) as pool:
            pages = prefetch_in_order(
                pool, lambda page: page, range(2, 10**1000), window=2
            )
            assert [next(pages)[0], next(pages)[0]] == [2, 3]
            pages.close()

    def test_no_pages_yields_nothing(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert list(prefetch_in_order(pool, lambda page: page, [])) == []


class TestReadSerially:
    def test_stops_on_the_first_short_page(self):
        calls = []

        def fetch(page):
            calls.append(page)
            return f"p{page}", 50 if page < 3 else 7

        assert list(read_serially(fetch, 1, 50)) == [(1, "p1"), (2, "p2"), (3, "p3")]
        assert calls == [1, 2, 3]

    def test_an_empty_first_page_ends_the_listing(self):
        assert list(read_serially(lambda page: ([], 0), 0, 50)) == [(0, [])]

    def test_honours_the_starting_page(self):
        calls = []

        def fetch(page):
            calls.append(page)
            return page, 0

        assert list(read_serially(fetch, 5, 50)) == [(5, 5)]
        assert calls == [5]

    def test_is_lazy(self):
        calls = []

        def fetch(page):
            calls.append(page)
            return page, 50

        pages = read_serially(fetch, 1, 50)
        next(pages)
        assert calls == [1]
