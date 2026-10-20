"""The full-screen results browser, driven with Textual's pilot.

The scenarios use the real services (a saved result session, the wishlist, the
dismissed list and the price history in the isolated data directory) and check
the outcome the way a user would, through `deals last --json`, `deals detail @1`
and the next `deals find`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import os
import subprocess
import sys

import pytest
from click.testing import CliRunner
from rich.console import Console

from audible_deals import wishlist as wishlist_mod
from audible_deals.cli import cli
from audible_deals.results_cache import load_dismissed_asins
from audible_deals.serialization import deserialize_product
from audible_deals.tui.app import (
    CompareScreen,
    HelpScreen,
    ResultsApp,
    TargetPriceScreen,
    matches_filter,
)
from tests.conftest import make_raw, seed_history

CATALOG = [
    # asin, title, author, narrator, series, price, rating
    ("B0TUI00001", "Alpha Dragon", "Zed Writer", "Nina Voice", "", 9.99, 4.0),
    ("B0TUI00002", "Bravo Mystery", "Amy Quill", "Omar Reader", "", 3.99, 4.8),
    ("B0TUI00003", "Charlie Space", "Zed Writer", "Nina Voice", "", 6.50, 3.9),
    ("B0TUI00004", "Delta Dunes", "Moe Pen", "Pia Speaker", "Dune Saga", 1.99, 4.5),
    ("B0TUI00005", "Echo Garden", "Amy Quill", "Omar Reader", "Dune Saga", 14.00, 4.2),
]


def _raw(asin, title, author, narrator, series, price, rating):
    raw = make_raw(
        asin,
        title=title,
        subtitle="",
        authors=[{"name": author}],
        narrators=[{"name": narrator}],
        price={"lowest_price": {"base": price}, "list_price": {"base": 20.0}},
        rating={
            "overall_distribution": {
                "display_average_rating": str(rating),
                "num_ratings": "100",
            }
        },
        series=[{"title": series, "sequence": asin[-1], "asin": "SER" + asin}]
        if series
        else [],
        runtime_length_min=600,
        language="english",
    )
    return raw


@pytest.fixture
def catalog(api):
    """A catalog served at the HTTP boundary; returns the raw products."""
    raws = [_raw(*row) for row in CATALOG]

    def get(endpoint, **params):
        if endpoint == "1.0/catalog/categories":
            return {"categories": []}
        if endpoint.startswith("1.0/catalog/products/"):
            asin = endpoint.rsplit("/", 1)[1]
            return {"product": next(r for r in raws if r["asin"] == asin)}
        return {"products": raws, "total_results": len(raws)}

    api.get_mock.side_effect = get
    return raws


def run(*args, **kwargs):
    return CliRunner().invoke(cli, list(args), **kwargs)


@pytest.fixture
def products(catalog):
    """The five products as a ``find`` saved them (so ``@N`` and ``last`` work)."""
    result = run(
        "find",
        "--pages",
        "1",
        "--all-languages",
        "--max-price",
        "30",
        "--sort",
        "title",
        "-q",
    )
    assert result.exit_code == 0, result.output
    shown = json.loads(run("last", "--json").stdout)
    assert [p["asin"] for p in shown] == [row[0] for row in CATALOG]  # title order
    return [deserialize_product(entry) for entry in shown]


def plain(widget) -> str:
    """The text a Static shows (its Rich content rendered without styles)."""
    console = Console(
        file=io.StringIO(), width=140, force_terminal=False, color_system=None
    )
    console.print(widget.content)
    return console.file.getvalue()


def last_asins():
    return [p["asin"] for p in json.loads(run("last", "--json").stdout)]


def drive(app, scenario, size=(180, 48)):
    """Run *scenario(pilot)* against *app* headlessly."""

    async def main():
        async with app.run_test(size=size) as pilot:
            await scenario(pilot)

    asyncio.run(main())
    return app


def shown(app):
    return [p.asin for _position, p in app._visible]


async def settle(pilot, app, timeout=10.0):
    """Wait for the ``/`` filter's debounce to run and the rows to be redrawn."""
    waited = 0.0
    while app._filter_timer is not None and waited < timeout:
        await pilot.pause(0.01)
        waited += 0.01
    await pilot.pause()


def messages(app):
    return [n.message for n in app._notifications]


# -- navigation and detail ---------------------------------------------------------


class TestNavigation:
    def test_arrows_and_vim_keys_move_the_cursor(self, products):
        app = ResultsApp(products)
        rows = []

        async def scenario(pilot):
            rows.append(app.table.cursor_row)
            await pilot.press("down")
            rows.append(app.table.cursor_row)
            await pilot.press("j", "j")
            rows.append(app.table.cursor_row)
            await pilot.press("k")
            rows.append(app.table.cursor_row)
            await pilot.press("up")
            rows.append(app.table.cursor_row)
            rows.append(app.table.row_count)

        drive(app, scenario)

        assert rows == [0, 1, 3, 2, 1, 5]

    def test_the_table_has_the_wide_tables_columns_and_original_refs(self, products):
        app = ResultsApp(products, currency="$")
        header = []
        refs = []

        async def scenario(pilot):
            header.extend(str(c.label) for c in app.table.columns.values())
            for row in range(5):
                refs.append(str(app.table.get_row_at(row)[1]))

        drive(app, scenario)

        assert header[1:6] == ["Ref", "Title / Author", "Price", "Hrs", "$/hr"]
        assert "Rating" in header
        assert refs == ["@1", "@2", "@3", "@4", "@5"]

    def test_enter_shows_the_detail_pane_and_it_follows_the_cursor(self, products):
        app = ResultsApp(products)
        panes = []

        async def scenario(pilot):
            side = app.query_one("#side")
            panes.append(side.has_class("active"))
            await pilot.press("enter")
            panes.append(side.has_class("active"))
            panes.append(plain(app.query_one("#side-content")))
            await pilot.press("down")
            panes.append(plain(app.query_one("#side-content")))

        drive(app, scenario)

        assert panes[0] is False and panes[1] is True
        assert products[0].asin in panes[2] and products[0].title in panes[2]
        assert products[1].asin in panes[3]

    def test_escape_closes_the_pane_and_a_second_escape_quits(self, products):
        app = ResultsApp(products)
        states = []

        async def scenario(pilot):
            await pilot.press("enter")
            await pilot.press("escape")
            states.append((app.side_mode, app.is_running))
            await pilot.press("escape")

        drive(app, scenario)

        assert states == [(None, True)]
        assert not app.is_running

    def test_an_empty_result_list_does_not_crash(self, tmp_config):
        app = ResultsApp([])

        async def scenario(pilot):
            await pilot.press("down", "enter", "w", "h", "n", "s")

        drive(app, scenario)

        assert app._visible == []


# -- the filter --------------------------------------------------------------------


class TestFilter:
    def test_matches_title_author_narrator_and_series_only_locally(self, products):
        assert matches_filter(products[0], "dragon")
        assert matches_filter(products[0], "ZED")
        assert matches_filter(products[0], "nina")
        assert matches_filter(products[3], "saga dunes")
        assert not matches_filter(products[0], "dragon amy")
        assert matches_filter(products[0], "   ")

    def test_typing_narrows_the_rows_and_escape_restores_them(self, products):
        app = ResultsApp(products)
        seen = {}

        async def scenario(pilot):
            await pilot.press("slash")
            seen["focus"] = app.focused.id
            await pilot.press("z", "e", "d")
            await settle(pilot, app)
            seen["narrowed"] = shown(app)
            seen["title"] = app.sub_title
            refs = [str(app.table.get_row_at(r)[1]) for r in range(app.table.row_count)]
            seen["refs"] = refs
            await pilot.press("escape")
            seen["cleared"] = shown(app)
            seen["focus_after"] = app.focused.id
            seen["still_running"] = app.is_running

        drive(app, scenario)

        assert seen["focus"] == "filter"
        assert seen["narrowed"] == ["B0TUI00001", "B0TUI00003"]
        assert "2 of 5" in seen["title"] and "filter: zed" in seen["title"]
        assert seen["refs"] == ["@1", "@3"]  # @N stays the position in the saved view
        assert (
            seen["cleared"] == [row[0] for row in CATALOG] or len(seen["cleared"]) == 5
        )
        assert seen["focus_after"] == "results"
        assert seen["still_running"]

    def test_letters_typed_into_the_filter_are_not_commands(self, products):
        app = ResultsApp(products)

        async def scenario(pilot):
            await pilot.press("slash", "q", "s", "w", "n")
            await settle(pilot, app)

        drive(app, scenario)

        assert app.filter_text == "qswn"
        assert shown(app) == []  # nothing matches, and nothing was quit or changed
        assert app.is_running is False  # run_test closed it afterwards
        assert wishlist_mod.load_wishlist() == []

    def test_enter_keeps_the_filter_and_returns_to_the_rows(self, products):
        app = ResultsApp(products)
        seen = {}

        async def scenario(pilot):
            await pilot.press("slash", "a", "m", "y", "enter")
            seen["rows"] = shown(app)
            seen["focus"] = app.focused.id
            await pilot.press("j")
            seen["cursor"] = app.table.cursor_row

        drive(app, scenario)

        assert seen["rows"] == ["B0TUI00002", "B0TUI00005"]
        assert seen["focus"] == "results" and seen["cursor"] == 1

    def test_the_filter_is_a_view_and_is_never_saved(self, products):
        before = last_asins()
        app = ResultsApp(products)

        async def scenario(pilot):
            await pilot.press("slash", "z", "e", "d", "enter")
            await pilot.press("s")  # sorting while filtered

        drive(app, scenario)

        assert sorted(last_asins()) == sorted(before)  # the saved view keeps all five

    def test_the_cursor_stays_on_the_same_title_when_the_filter_changes(self, products):
        app = ResultsApp(products)
        seen = {}

        async def scenario(pilot):
            await pilot.press("j", "j")  # Charlie Space
            await pilot.press("slash", "z")
            await settle(pilot, app)
            seen["narrowed"] = shown(app)
            seen["on"] = app.current().asin

        drive(app, scenario)

        assert seen["narrowed"] == ["B0TUI00001", "B0TUI00003"]
        assert seen["on"] == "B0TUI00003"


class TestFilterDebounce:
    """Typing into ``/`` must not redraw a long table once per keystroke."""

    @staticmethod
    def big(products, count=150):
        """*count* distinct titles; every third one is by "Zed Writer"."""
        template = products[0]
        return [
            dataclasses.replace(
                template,
                asin=f"B0BIG{i:05d}",
                title=f"Title {i:04d}",
                authors=["Zed Writer" if i % 3 == 0 else f"Author {i}"],
            )
            for i in range(count)
        ]

    @staticmethod
    def count_rebuilds(app):
        calls = []
        original = app._rebuild

        def counting(*args, **kwargs):
            calls.append(app.filter_text)
            return original(*args, **kwargs)

        app._rebuild = counting
        return calls

    def test_keystrokes_in_a_burst_redraw_a_1500_row_table_once(self, products):
        app = ResultsApp(self.big(products, 1500))
        app.filter_debounce = 1.0  # the gaps between presses can't outlast it
        seen = {}

        async def scenario(pilot):
            calls = self.count_rebuilds(app)
            await pilot.press("slash", "z", "e", "d", " ", "w")
            seen["during"] = (len(calls), len(shown(app)), app.table.row_count)
            seen["pending"] = app._filter_timer is not None
            await settle(pilot, app)
            seen["after"] = (list(calls), len(shown(app)), app.table.row_count)
            seen["title"] = app.sub_title

        drive(app, scenario)

        assert seen["during"] == (0, 1500, 1500)  # still the full list while typing
        assert seen["pending"] is True
        redraws, rows, table_rows = seen["after"]
        assert redraws == ["zed w"]  # once, with the finished text
        assert rows == table_rows == 500
        assert "500 of 1500" in seen["title"] and "filter: zed w" in seen["title"]

    def test_the_rows_follow_within_a_moment_at_the_default_delay(self, products):
        app = ResultsApp(self.big(products))
        seen = {}
        assert app.filter_debounce == pytest.approx(0.15)

        async def scenario(pilot):
            await pilot.press("slash", "z", "e", "d")
            await settle(pilot, app)
            seen["rows"] = len(shown(app))

        drive(app, scenario)

        assert seen["rows"] == 50

    def test_enter_applies_a_pending_filter_at_once(self, products):
        app = ResultsApp(self.big(products))
        app.filter_debounce = 30.0  # would never fire by itself during the test
        seen = {}

        async def scenario(pilot):
            await pilot.press("slash", "z", "e", "d")
            seen["before"] = len(shown(app))
            await pilot.press("enter")
            seen["after"] = len(shown(app))
            seen["pending"] = app._filter_timer
            seen["focus"] = app.focused.id

        drive(app, scenario)

        assert seen["before"] == 150
        assert seen["after"] == 50
        assert seen["pending"] is None and seen["focus"] == "results"

    def test_escape_drops_a_pending_filter_without_a_stray_redraw(self, products):
        app = ResultsApp(self.big(products))
        app.filter_debounce = 0.3
        seen = {}

        async def scenario(pilot):
            calls = self.count_rebuilds(app)
            await pilot.press("slash", "z", "e", "d", "escape")
            seen["right_after"] = (len(shown(app)), app._filter_timer, list(calls))
            await pilot.pause(0.6)  # long enough for the dropped timer to have fired
            seen["later"] = (len(shown(app)), list(calls))

        drive(app, scenario)

        rows, timer, calls = seen["right_after"]
        assert rows == 150 and timer is None and calls == [""]
        assert seen["later"] == (150, [""])  # no second redraw from the old timer

    def test_each_keystroke_restarts_the_wait(self, products):
        app = ResultsApp(self.big(products))
        app.filter_debounce = 0.4
        seen = {}

        async def scenario(pilot):
            calls = self.count_rebuilds(app)
            await pilot.press("slash", "z")
            await pilot.pause(0.25)
            await pilot.press("e")  # restarts the 0.4 s wait
            await pilot.pause(0.25)  # 0.5 s after "z", only 0.25 s after "e"
            seen["mid"] = list(calls)
            await settle(pilot, app)
            seen["end"] = list(calls)

        drive(app, scenario)

        assert seen["mid"] == []
        assert seen["end"] == ["ze"]


# -- sort ---------------------------------------------------------------------------


class TestSort:
    def test_s_cycles_the_sort_and_the_session_follows(self, products):
        app = ResultsApp(products)
        orders = []

        async def scenario(pilot):
            await pilot.press("s")
            orders.append((app.sort_key, shown(app)))
            await pilot.press("s")
            orders.append((app.sort_key, shown(app)))

        drive(app, scenario)

        (first_key, first), (second_key, second) = orders
        by_price = sorted(CATALOG, key=lambda row: row[5])
        assert first_key == "price" and first == [row[0] for row in by_price]
        assert second_key == "-price" and second == list(reversed(first))
        # What the user saw last is what `deals last` and `@N` mean now.
        assert last_asins() == second

    def test_detail_at_1_is_the_top_row_after_sorting(self, products, catalog):
        app = ResultsApp(products)

        async def scenario(pilot):
            await pilot.press("s")

        drive(app, scenario)

        result = run("detail", "@1")
        assert result.exit_code == 0, result.output
        assert "B0TUI00004" in result.stdout  # the cheapest, 1.99

    def test_the_cycle_visits_every_sort_once_and_wraps(self):
        from audible_deals import browse_actions
        from audible_deals.filtering import _SORT_KEYS

        assert set(browse_actions.SORT_CYCLE) <= set(_SORT_KEYS)
        assert len(set(browse_actions.SORT_CYCLE)) == len(browse_actions.SORT_CYCLE)
        key = None
        visited = []
        for _ in range(len(browse_actions.SORT_CYCLE)):
            key = browse_actions.next_sort_key(key)
            visited.append(key)
        assert visited == list(browse_actions.SORT_CYCLE)
        assert browse_actions.next_sort_key(visited[-1]) == visited[0]

    def test_it_starts_from_the_sort_the_session_already_has(self, products):
        app = ResultsApp(products, sort_key="rating")

        async def scenario(pilot):
            await pilot.press("s")

        drive(app, scenario)

        assert app.sort_key == "price-per-hour"  # the sort after rating


# -- marking and the wishlist -------------------------------------------------------


class TestWishlist:
    def test_space_marks_and_moves_down(self, products):
        app = ResultsApp(products)
        seen = {}

        async def scenario(pilot):
            await pilot.press("space")
            await pilot.press("space")
            seen["marked"] = set(app.marked)
            seen["cursor"] = app.table.cursor_row
            seen["mark_cell"] = str(app.table.get_row_at(0)[0])
            await pilot.press("up", "space")  # unmark the second
            seen["after"] = set(app.marked)

        drive(app, scenario)

        assert seen["marked"] == {"B0TUI00001", "B0TUI00002"}
        assert seen["cursor"] == 2
        assert seen["mark_cell"] == "●"
        assert seen["after"] == {"B0TUI00001"}

    def test_marking_updates_one_cell_instead_of_rebuilding_the_table(self, products):
        app = ResultsApp(TestFilterDebounce.big(products, 200))
        seen = {}

        async def scenario(pilot):
            rebuilds = []
            real = app._rebuild
            app._rebuild = lambda *a, **k: (rebuilds.append(1), real(*a, **k))[1]
            await pilot.press("space", "space", "down", "space")
            seen["rebuilds"] = len(rebuilds)
            seen["cells"] = [str(app.table.get_row_at(r)[0]) for r in range(5)]
            seen["cursor"] = app.table.cursor_row
            seen["title"] = app.sub_title

        drive(app, scenario)

        assert seen["rebuilds"] == 0
        assert seen["cells"] == ["●", "●", " ", "●", " "]
        assert seen["cursor"] == 4
        assert "3 marked" in seen["title"]

    def test_marks_stay_right_under_a_filter_and_after_sorting(self, products):
        app = ResultsApp(products)
        seen = {}

        async def scenario(pilot):
            await pilot.press("space")  # Alpha Dragon
            await pilot.press("slash", "a", "m", "y", "enter")  # Bravo, Echo (Amy)
            await settle(pilot, app)
            await pilot.press("space")  # Bravo Mystery, marked under the filter
            seen["filtered"] = {
                p.asin: str(app.table.get_row_at(r)[0])
                for r, (_pos, p) in enumerate(app._visible)
            }
            await pilot.press("escape")  # the filter goes; both marks must show
            await settle(pilot, app)
            seen["cleared"] = {
                p.asin: str(app.table.get_row_at(r)[0])
                for r, (_pos, p) in enumerate(app._visible)
            }
            await pilot.press("s")  # sort: the dots follow the titles
            seen["sorted"] = {
                p.asin: str(app.table.get_row_at(r)[0])
                for r, (_pos, p) in enumerate(app._visible)
            }
            seen["marked"] = set(app.marked)

        drive(app, scenario)

        assert seen["marked"] == {"B0TUI00001", "B0TUI00002"}
        assert seen["filtered"] == {"B0TUI00002": "●", "B0TUI00005": " "}
        expected = {asin: " " for asin in (row[0] for row in CATALOG)}
        expected.update({"B0TUI00001": "●", "B0TUI00002": "●"})
        assert seen["cleared"] == expected
        assert seen["sorted"] == expected

    def test_a_title_listed_twice_shows_its_mark_on_both_rows(self, products):
        twice = [products[0], products[1], products[0]]
        app = ResultsApp(twice)
        seen = {}

        async def scenario(pilot):
            await pilot.press("space")  # row 0: the same ASIN as row 2
            seen["cells"] = [str(app.table.get_row_at(r)[0]) for r in range(3)]
            await pilot.press("up", "space")  # unmark it again
            seen["after"] = [str(app.table.get_row_at(r)[0]) for r in range(3)]

        drive(app, scenario)

        assert seen["cells"] == ["●", " ", "●"]
        assert seen["after"] == [" ", " ", " "]

    def test_w_asks_for_a_target_defaulting_to_the_current_price(self, products):
        app = ResultsApp(products)
        seen = {}

        async def scenario(pilot):
            await pilot.press("down", "w")  # Bravo Mystery, 3.99
            seen["screen"] = type(app.screen).__name__
            seen["default"] = app.screen.query_one("#target").value
            await pilot.press("enter")  # accept the default

        drive(app, scenario)

        assert seen == {"screen": "TargetPriceScreen", "default": "3.99"}
        (item,) = wishlist_mod.load_wishlist()
        assert item["asin"] == "B0TUI00002" and item["max_price"] == 3.99
        assert any(
            "Added 1 title(s) to the wishlist (target $3.99)" in s for s in app.summary
        )

    def test_a_typed_price_replaces_the_default(self, products):
        app = ResultsApp(products)

        async def scenario(pilot):
            await pilot.press("w")
            await pilot.press("5", ".", "2", "5", "enter")

        drive(app, scenario)

        (item,) = wishlist_mod.load_wishlist()
        assert item["asin"] == "B0TUI00001" and item["max_price"] == 5.25

    def test_marked_titles_go_together_with_no_default_and_empty_means_no_target(
        self, products
    ):
        app = ResultsApp(products)
        seen = {}

        async def scenario(pilot):
            await pilot.press("space", "space")  # marks 1 and 2
            await pilot.press("w")
            seen["default"] = app.screen.query_one("#target").value
            await pilot.press("enter")
            seen["marks_left"] = set(app.marked)

        drive(app, scenario)

        assert seen["default"] == ""
        items = wishlist_mod.load_wishlist()
        assert [i["asin"] for i in items] == ["B0TUI00001", "B0TUI00002"]
        assert all(i["max_price"] is None for i in items)
        assert seen["marks_left"] == set()

    def test_a_bad_price_keeps_the_dialog_open_with_a_message(self, products):
        app = ResultsApp(products)
        seen = {}

        async def scenario(pilot):
            await pilot.press("w")
            await pilot.press("a", "b", "c", "enter")
            seen["still_open"] = isinstance(app.screen, TargetPriceScreen)
            seen["error"] = plain(app.screen.query_one("#error"))
            seen["added"] = wishlist_mod.load_wishlist()
            await pilot.press("backspace", "backspace", "backspace")  # no target
            await pilot.press("enter")

        drive(app, scenario)

        assert seen["still_open"] is True
        assert "greater than 0" in seen["error"] and seen["added"] == []
        assert [i["asin"] for i in wishlist_mod.load_wishlist()] == ["B0TUI00001"]

    def test_escape_cancels_without_adding(self, products):
        app = ResultsApp(products)

        async def scenario(pilot):
            await pilot.press("w")
            await pilot.press("escape")

        drive(app, scenario)

        assert wishlist_mod.load_wishlist() == []
        assert app.summary == []

    def test_a_title_already_on_the_wishlist_is_not_asked_about_again(self, products):
        wishlist_mod.save_wishlist(
            [{"asin": "B0TUI00001", "title": "T", "max_price": 4.0, "added": ""}]
        )
        app = ResultsApp(products)
        seen = {}

        async def scenario(pilot):
            await pilot.press("w")
            seen["screen"] = type(app.screen).__name__

        drive(app, scenario)

        assert seen["screen"] == "Screen" or seen["screen"] != "TargetPriceScreen"
        assert any("Already on the wishlist" in m for m in messages(app))
        assert wishlist_mod.load_wishlist()[0]["max_price"] == 4.0


# -- dismiss ------------------------------------------------------------------------


class TestDismiss:
    def test_n_hides_the_title_everywhere_and_the_next_find_skips_it(
        self, products, catalog
    ):
        app = ResultsApp(products)

        async def scenario(pilot):
            await pilot.press("down", "n")  # Bravo Mystery

        drive(app, scenario)

        assert "B0TUI00002" not in shown(app)
        assert "B0TUI00002" in load_dismissed_asins()
        assert "B0TUI00002" not in last_asins()
        assert any("Dismissed 1 title(s)" in s for s in app.summary)
        # The next scan honors it (the scan cache would answer from before: refresh).
        again = run(
            "find",
            "--pages",
            "1",
            "--all-languages",
            "--max-price",
            "30",
            "--refresh",
            "--json",
        )
        assert again.exit_code == 0, again.output
        assert "B0TUI00002" not in [p["asin"] for p in json.loads(again.stdout)]
        assert len(json.loads(again.stdout)) == 4

    def test_marked_titles_are_dismissed_together(self, products):
        app = ResultsApp(products)

        async def scenario(pilot):
            await pilot.press("space", "space", "n")

        drive(app, scenario)

        assert load_dismissed_asins() == {"B0TUI00001", "B0TUI00002"}
        assert shown(app) == ["B0TUI00003", "B0TUI00004", "B0TUI00005"]
        assert last_asins() == shown(app)

    def test_exclude_seen_is_honored_alongside_the_dismissed_list(
        self, products, catalog
    ):
        app = ResultsApp(products)

        async def scenario(pilot):
            await pilot.press("n")

        drive(app, scenario)

        result = run(
            "find",
            "--pages",
            "1",
            "--all-languages",
            "--max-price",
            "30",
            "--refresh",
            "--exclude-seen",
            "--json",
        )
        assert result.exit_code == 0, result.output
        assert "B0TUI00001" not in [p["asin"] for p in json.loads(result.stdout)]

    def test_a_failing_dismissal_keeps_the_rows(self, products, monkeypatch):
        from audible_deals import browse_actions

        def refuse(asins):
            raise OSError("read-only")

        monkeypatch.setattr(browse_actions, "save_dismissed_asins", refuse)
        app = ResultsApp(products)

        async def scenario(pilot):
            await pilot.press("n")

        drive(app, scenario)

        assert len(shown(app)) == 5
        assert any("Could not dismiss" in m for m in messages(app))


# -- history, compare, open, help --------------------------------------------------


class TestPanesAndScreens:
    def test_h_shows_the_price_history_pane(self, products):
        seed_history(
            "B0TUI00001",
            [("2026-01-01", 12.0), ("2026-02-01", 9.0), ("2026-03-01", 9.99)],
        )
        from tests.conftest import make_product

        unrecorded = make_product(asin="B0NOHIST01", title="Never Seen Before")
        app = ResultsApp([products[0], unrecorded])
        seen = {}

        async def scenario(pilot):
            await pilot.press("h")
            seen["with"] = plain(app.query_one("#side-content"))
            await pilot.press("down")
            seen["without"] = plain(app.query_one("#side-content"))
            await pilot.press("h")
            seen["closed"] = app.side_mode

        drive(app, scenario)

        assert "Price History: B0TUI00001" in seen["with"]
        assert "Low:" in seen["with"] and "High:" in seen["with"]
        assert "No price history for B0NOHIST01" in seen["without"]
        assert seen["closed"] is None

    def test_c_compares_two_to_four_marked_titles(self, products):
        app = ResultsApp(products)
        seen = {}

        async def scenario(pilot):
            await pilot.press("space", "space", "c")
            seen["screen"] = type(app.screen)
            body = plain(app.screen.query_one("#compare-body"))
            seen["body"] = body
            await pilot.press("escape")
            seen["back"] = type(app.screen).__name__

        drive(app, scenario)

        assert seen["screen"] is CompareScreen
        assert "Comparison" in seen["body"]
        assert "B0TUI00001" in seen["body"] and "B0TUI00002" in seen["body"]
        assert seen["back"] != "CompareScreen"

    @pytest.mark.parametrize("marked", [0, 1, 5])
    def test_compare_needs_between_two_and_four(self, products, marked):
        app = ResultsApp(products)
        seen = {}

        async def scenario(pilot):
            await pilot.press(*(["space"] * marked), "c")
            seen["screen"] = type(app.screen)

        drive(app, scenario)

        assert seen["screen"] is not CompareScreen
        assert any("Mark 2 to 4 titles" in m for m in messages(app))

    def test_four_titles_are_the_most_it_compares(self, products):
        app = ResultsApp(products)
        seen = {}

        async def scenario(pilot):
            await pilot.press(*(["space"] * 4), "c")
            seen["screen"] = type(app.screen)

        drive(app, scenario)

        assert seen["screen"] is CompareScreen

    def test_o_opens_the_audible_page(self, products, monkeypatch):
        import click

        opened = []
        monkeypatch.setattr(click, "launch", opened.append)
        app = ResultsApp(products)

        async def scenario(pilot):
            await pilot.press("down", "o")

        drive(app, scenario)

        assert opened == [products[1].url]

    def test_question_mark_lists_the_keys(self, products):
        app = ResultsApp(products)
        seen = {}

        async def scenario(pilot):
            await pilot.press("question_mark")
            seen["screen"] = type(app.screen)
            seen["text"] = plain(app.screen.query_one("#help-body"))
            await pilot.press("escape")
            seen["running"] = app.is_running

        drive(app, scenario)

        assert seen["screen"] is HelpScreen
        for fragment in ("Enter", "filter", "next sort", "wishlist", "compare", "quit"):
            assert fragment in seen["text"]
        assert seen["running"]

    def test_the_footer_names_the_keys(self, products):
        app = ResultsApp(products)
        seen = {}

        async def scenario(pilot):
            await pilot.pause()
            footer = app.query_one("Footer")
            seen["text"] = " ".join(str(w.render()) for w in footer.query("*"))

        drive(app, scenario)

        for label in (
            "Quit",
            "Filter",
            "Sort",
            "Mark",
            "Wishlist",
            "Compare",
            "History",
        ):
            assert label in seen["text"], label


# -- quitting -----------------------------------------------------------------------


class TestQuit:
    def test_q_quits_and_reports_what_was_done(self, products):
        app = ResultsApp(products)

        async def scenario(pilot):
            await pilot.press("n")
            await pilot.press("q")

        drive(app, scenario)

        assert not app.is_running
        assert app.summary == ["Dismissed 1 title(s) for good"]

    def test_the_whole_flow_through_run_results_app(self, products, monkeypatch):
        """`run_results_app` is what the CLI calls; it returns the summary."""
        from audible_deals.tui import app as tui_app

        def fake_run(self, *args, **kwargs):
            self.summary.append("did something")

        monkeypatch.setattr(tui_app.ResultsApp, "run", fake_run)

        assert tui_app.run_results_app(products) == ["did something"]


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pseudo-terminal")
def test_quitting_leaves_the_terminal_the_way_it_was(tmp_path):
    """Run the app in a real pseudo-terminal, press q, and read what it wrote."""
    import pty
    import select
    import time

    script = tmp_path / "run_app.py"
    script.write_text(
        "from audible_deals.tui.app import run_results_app\n"
        "from audible_deals.product import Product\n"
        "p = Product(asin='B0PTY00001', title='Pty Title', authors=['A'], price=3.0,"
        " list_price=10.0, length_minutes=600, rating=4.0, num_ratings=10)\n"
        "run_results_app([p])\n"
        "print('DONE')\n"
    )
    master, slave = pty.openpty()
    import fcntl
    import struct
    import termios

    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
    env = {
        **os.environ,
        "TERM": "xterm-256color",
        "DEALS_HOME": str(tmp_path / "d"),
        "HOME": str(tmp_path),
    }
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        close_fds=True,
    )
    os.close(slave)
    output = b""
    deadline = time.time() + 20
    sent = False
    while time.time() < deadline:
        ready, _, _ = select.select([master], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
        if not sent and b"Pty Title" in output:
            os.write(master, b"q")
            sent = True
        if proc.poll() is not None and not ready:
            break
    code = proc.wait(timeout=10)
    os.close(master)

    assert sent, output[-300:]
    assert code == 0
    assert b"DONE" in output
    # The alternate screen was entered and left; the cursor and mouse are restored.
    assert b"\x1b[?1049h" in output and b"\x1b[?1049l" in output
    assert output.rfind(b"\x1b[?1049l") < output.rfind(b"DONE")
    assert b"\x1b[?25h" in output[output.rfind(b"\x1b[?1049l") - 200 :]
    for mouse in (b"\x1b[?1000l", b"\x1b[?1003l", b"\x1b[?1006l"):
        assert mouse in output
