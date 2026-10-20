"""The live "best so far" view and what a Ctrl-C during a scan keeps."""

from __future__ import annotations

import io
import json

import pytest
from click.testing import CliRunner
from rich.console import Console

from audible_deals.catalog_workflow import (
    ScanReport,
    bind_catalog_categories,
    build_find_scan_plan,
    execute_catalog_scan,
)
from audible_deals.cli import cli
from audible_deals.cli import helpers as cli_helpers
from audible_deals.client import CatalogSearchResult
from audible_deals.presentation import terminal
from audible_deals.presentation.live import LiveResults
from audible_deals.presentation.products import ProductDisplayOptions
from audible_deals.price_history import load_price_history
from audible_deals.result_models import CatalogScanProgress
from audible_deals.settings import Settings
from tests.conftest import make_product, make_raw


def cheap(n: int, price: float = 3.0):
    return make_product(
        asin=f"B0LIVE{n:04d}",
        title=f"Live {n}",
        price=price,
        series_name="",
        series_position="",
    )


def make_live(rank=None, *, rows=3, width=100):
    return LiveResults(
        rank or (lambda products: sorted(products, key=lambda p: p.price or 0)),
        ProductDisplayOptions(max_price=10.0),
        rows=rows,
        width=lambda: width,
    )


class TestLiveResults:
    def test_nothing_yet_shows_a_waiting_line(self):
        text = make_live().view().plain
        assert "Looking through 0 titles" in text

    def test_shows_the_best_rows_so_far_and_updates_as_pages_arrive(self):
        live = make_live(rows=2)
        live.add([cheap(1, 9.0), cheap(2, 8.0), cheap(3, 7.0)])
        console = Console(width=100, force_terminal=False, record=True)
        console.print(live.view())
        first = console.export_text()
        assert "Best 2 so far" in first
        assert "Live 3" in first and "Live 2" in first and "Live 1" not in first

        live.add([cheap(4, 1.0)])
        console = Console(width=100, force_terminal=False, record=True)
        console.print(live.view())
        assert "Live 4" in console.export_text()

    def test_products_seen_twice_are_counted_once(self):
        live = make_live()
        live.add([cheap(1)])
        live.add([cheap(1), cheap(2)])
        assert live.seen == 2

    def test_a_ranking_failure_freezes_the_preview_but_never_raises(self):
        calls = []

        def rank(products):
            calls.append(len(products))
            if len(calls) == 2:
                raise RuntimeError("history unavailable")
            return products

        live = make_live(rank)
        live.add([cheap(1)])
        live.add([cheap(2)])  # raises inside, swallowed
        live.add([cheap(3)])  # no longer ranks

        assert calls == [1, 2]
        assert live.seen == 3
        console = Console(width=100, force_terminal=False, record=True)
        console.print(live.view())
        assert "Live 1" in console.export_text()

    def test_narrow_terminals_get_one_line_per_title(self):
        live = make_live(width=60)
        live.add([cheap(1, 4.5)])
        console = Console(width=60, force_terminal=False, record=True)
        console.print(live.view())
        text = console.export_text()
        assert "@1 Live 1" in text and "$4.50" in text

    def test_titles_cannot_inject_terminal_controls(self):
        live = make_live(width=60)
        live.add([make_product(asin="B0EVIL0001", title="Evil\x1b[2Jtitle", price=1.0)])
        console = Console(width=60, force_terminal=False, record=True)
        console.print(live.view())
        assert "\x1b" not in console.export_text()


def interactive_console():
    stream = io.StringIO()
    return (
        Console(
            file=stream,
            force_terminal=True,
            force_interactive=True,
            width=100,
            height=40,
            color_system=None,
        ),
        stream,
    )


class TestCatalogScanProgressLive:
    PLAN = bind_catalog_categories(build_find_scan_plan("", pages=2), [""])

    def _drive(self, live, monkeypatch, console):
        monkeypatch.setattr(terminal, "console", console)
        with terminal.catalog_scan_progress(self.PLAN, "Scanning", live=live) as update:
            update(
                CatalogScanProgress(
                    "Scanning", 2, 1, 2, new_products=(cheap(1), cheap(2))
                )
            )
            update(CatalogScanProgress("Scanning", 2, 2, 3, new_products=(cheap(3),)))

    def test_a_terminal_gets_the_live_table_and_it_is_erased_afterwards(
        self, monkeypatch
    ):
        console, stream = interactive_console()
        self._drive(make_live(), monkeypatch, console)

        written = stream.getvalue()
        assert "Best 3 so far" in written
        assert "Live 1" in written
        # transient: the cursor moved back over the frame and cleared it
        assert "\x1b[2K" in written or "\x1b[J" in written

    def test_without_a_terminal_it_is_the_plain_progress_bar(self, monkeypatch):
        stream = io.StringIO()
        console = Console(file=stream, force_terminal=False, width=100)
        self._drive(make_live(), monkeypatch, console)

        assert "so far" not in stream.getvalue()

    def test_a_scan_with_json_output_never_goes_live(self, monkeypatch):
        console, stream = interactive_console()
        monkeypatch.setattr(terminal, "console", console)
        with terminal.catalog_scan_progress(
            self.PLAN, "Scanning", disable=True, live=make_live()
        ) as update:
            update(CatalogScanProgress("Scanning", 2, 1, 1, new_products=(cheap(1),)))

        assert stream.getvalue() == ""

    def test_the_cached_page_count_is_shown_on_the_bar(self, monkeypatch):
        console, stream = interactive_console()
        monkeypatch.setattr(terminal, "console", console)
        with terminal.catalog_scan_progress(
            self.PLAN, "Scanning", live=make_live()
        ) as update:
            update(CatalogScanProgress("Scanning", 2, 1, 1, cached=1))

        assert "1 cached" in stream.getvalue()


class TestWhenToStream:
    SETTINGS = Settings(max_price=5.0)

    def _live(self, monkeypatch, *, interactive=True, **overrides):
        console, _ = interactive_console()
        if not interactive:
            console = Console(file=io.StringIO(), force_terminal=False)
        monkeypatch.setattr(cli_helpers, "console", console)
        arguments = {
            "currency": "$",
            "credit_price": None,
            "skip_asins": None,
            "exclude_category_ids": set(),
            "json_flag": False,
            "quiet": False,
        }
        arguments.update(overrides)
        settings = overrides.pop("settings", self.SETTINGS)
        arguments.pop("settings", None)
        return cli_helpers._live_results(settings, **arguments)

    def test_a_terminal_streams(self, monkeypatch):
        assert self._live(monkeypatch) is not None

    @pytest.mark.parametrize("flag", ["json_flag", "quiet"])
    def test_json_and_quiet_do_not_stream(self, monkeypatch, flag):
        assert self._live(monkeypatch, **{flag: True}) is None

    def test_interactive_mode_does_not_stream(self, monkeypatch):
        settings = Settings(max_price=5.0, interactive=True)
        assert self._live(monkeypatch, settings=settings) is None

    def test_redirected_output_does_not_stream(self, monkeypatch):
        assert self._live(monkeypatch, interactive=False) is None

    def test_the_preview_uses_the_real_filters(self, monkeypatch):
        live = self._live(monkeypatch)
        live.add([cheap(1, 4.0), cheap(2, 9.0), cheap(3, 2.0)])
        console = Console(width=100, force_terminal=False, record=True)
        console.print(live.view())
        text = console.export_text()
        # max price 5: the $9 title is filtered out, the cheapest comes first.
        assert "Live 3" in text and "Live 1" in text and "Live 2" not in text


class FakeClient:
    """search_segments that delivers some pages, then the user presses Ctrl-C."""

    def __init__(self, pages_before_interrupt: int, *, interrupt: bool = True):
        self.pages_before_interrupt = pages_before_interrupt
        self.interrupt = interrupt

    def search_segments(self, requests, page_callback=None):
        for number in range(1, self.pages_before_interrupt + 1):
            page_callback(0, [cheap(number * 10 + i) for i in range(3)], number, 200)
        if self.interrupt:
            raise KeyboardInterrupt
        return [
            CatalogSearchResult(
                tuple(
                    ([cheap(number * 10 + i) for i in range(3)], number, 200)
                    for number in range(1, self.pages_before_interrupt + 1)
                )
            )
        ]


class TestInterruptedScan:
    PLAN = bind_catalog_categories(build_find_scan_plan("", pages=4), [""])

    def test_ctrl_c_returns_what_was_gathered_and_flags_the_report(self):
        report = ScanReport()
        events = []

        products = execute_catalog_scan(
            FakeClient(2),
            self.PLAN,
            events.append,
            report=report,
            allow_partial=True,
        )

        assert len(products) == 6
        assert report.interrupted
        assert (report.pages_done, report.pages_total) == (2, 4)
        assert events[-1].description == "Scan interrupted"

    def test_ctrl_c_before_the_first_page_returns_nothing(self):
        report = ScanReport()

        assert (
            execute_catalog_scan(
                FakeClient(0), self.PLAN, report=report, allow_partial=True
            )
            == []
        )
        assert report.interrupted

    def test_without_allow_partial_the_interrupt_propagates(self):
        with pytest.raises(KeyboardInterrupt):
            execute_catalog_scan(FakeClient(2), self.PLAN)

    def test_a_scan_that_finishes_is_not_marked_interrupted(self):
        report = ScanReport()

        products = execute_catalog_scan(
            FakeClient(2, interrupt=False), self.PLAN, report=report, allow_partial=True
        )

        assert len(products) == 6
        assert not report.interrupted

    def test_pages_events_carry_their_products_for_a_live_view(self):
        events = []
        execute_catalog_scan(FakeClient(2, interrupt=False), self.PLAN, events.append)

        page_events = [e for e in events if e.new_products]
        assert [len(e.new_products) for e in page_events] == [3, 3]


class TestInterruptedCommands:
    """Ctrl-C mid-scan through the real client: partial results, exit 130."""

    def _api(self, api, interrupt_on_page: int):
        def get(endpoint, **params):
            if endpoint == "1.0/catalog/categories":
                return {"categories": []}
            page = params.get("page", 0)
            if page == interrupt_on_page:
                raise KeyboardInterrupt
            return {
                "products": [
                    make_raw(
                        f"B0P{page}{index:06d}",
                        price={
                            "lowest_price": {"base": 3.0},
                            "list_price": {"base": 20.0},
                        },
                        runtime_length_min=600,
                        language="english",
                    )
                    for index in range(3)
                ],
                "total_results": 400,
            }

        api.get_mock.side_effect = get

    ARGS = ("find", "--max-price", "10", "--pages", "6", "-n", "20")

    def test_find_shows_the_partial_results_saves_the_session_and_exits_130(self, api):
        self._api(api, interrupt_on_page=1)

        result = CliRunner().invoke(cli, list(self.ARGS))

        assert result.exit_code == 130, result.output
        text = " ".join(result.output.split())
        assert "Scan interrupted after 1/" in text
        assert "pages; showing what was gathered so far" in text
        assert "B0P0000000" in result.output  # page 1 is in the table
        # the session was saved, so @N selectors work
        last = CliRunner().invoke(cli, ["last", "--json"])
        assert last.exit_code == 0, last.output
        assert {item["asin"] for item in json.loads(last.stdout)} >= {"B0P0000000"}

    def test_the_gathered_prices_are_recorded_in_the_history(self, api):
        self._api(api, interrupt_on_page=1)
        CliRunner().invoke(cli, list(self.ARGS))

        assert [e["price"] for e in load_price_history("B0P0000000")] == [3.0]

    def test_json_output_stays_valid_and_the_notice_goes_to_stderr(self, api):
        self._api(api, interrupt_on_page=1)

        result = CliRunner().invoke(cli, [*self.ARGS, "--json"])

        assert result.exit_code == 130
        assert json.loads(result.stdout)[0]["asin"] == "B0P0000000"
        assert "Scan interrupted" in result.stderr
        assert "Scan interrupted" not in result.stdout

    def test_search_behaves_the_same(self, api):
        self._api(api, interrupt_on_page=1)

        result = CliRunner().invoke(
            cli, ["search", "dragon", "--max-price", "10", "--pages", "6"]
        )

        assert result.exit_code == 130, result.output
        assert "Scan interrupted" in " ".join(result.output.split())

    def test_pages_gathered_before_the_interrupt_are_cached_for_the_next_run(self, api):
        self._api(api, interrupt_on_page=1)
        CliRunner().invoke(cli, list(self.ARGS))
        first_calls = api.get_mock.call_count
        self._api(api, interrupt_on_page=99)

        again = CliRunner().invoke(cli, list(self.ARGS))

        assert again.exit_code == 0, again.output
        # The pages that finished before the interrupt are reused: the first
        # page is not requested again, the one that was interrupted is.
        again_pages = [
            call.kwargs["page"]
            for call in api.get_mock.call_args_list[first_calls:]
            if call.args[0] == "1.0/catalog/products"
        ]
        assert 0 not in again_pages
        assert 1 in again_pages
        assert len(again_pages) < 6

    def test_other_commands_still_abort_on_ctrl_c(self, api):
        api.get_mock.side_effect = KeyboardInterrupt

        result = CliRunner().invoke(cli, ["library", "--json"])

        # Only find/search turn Ctrl-C into partial results; the rest abort.
        assert result.exit_code == 1
        assert "Aborted" in result.output
