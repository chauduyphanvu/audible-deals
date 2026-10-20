"""``find`` re-prices titles it has shown before but the scan did not return."""

from __future__ import annotations

import datetime
import json

import pytest
from click.testing import CliRunner

from audible_deals import db
from audible_deals.cli import cli
from audible_deals.price_history import load_price_history
from tests.conftest import make_raw

PAGES = 2
GEM = "B0GEM00001"


def raw(asin: str, price: float, *, category: str = "CAT1", **fields):
    return make_raw(
        asin,
        price={"lowest_price": {"base": price}, "list_price": {"base": 20.0}},
        runtime_length_min=600,
        language="english",
        series=[],
        category_ladders=[{"ladder": [{"id": category, "name": "Science Fiction"}]}],
        **fields,
    )


class FakeCatalog:
    """Scans return filler titles; the GEM is only reachable by search or by ASIN."""

    def __init__(self, api):
        self.mock = api.get_mock
        self.mock.side_effect = self.get
        self.gem_price = 2.5
        self.scan_price = 4.0
        self.fail_batches = False

    def get(self, endpoint, **params):
        if endpoint == "1.0/catalog/categories":
            return {"categories": [{"id": "CAT1", "name": "Science Fiction"}]}
        if endpoint.startswith("1.0/catalog/categories/"):
            return {"category": {"name": "Science Fiction", "children": []}}
        if endpoint == "1.0/library":
            return {"items": []}
        assert endpoint == "1.0/catalog/products", endpoint
        if params.get("asins"):
            if self.fail_batches:
                raise RuntimeError_()
            wanted = params["asins"].split(",")
            return {
                "products": [
                    raw(asin, self.gem_price if asin == GEM else self.scan_price)
                    for asin in wanted
                ]
            }
        if params.get("keywords") == "gem" or params.get("title") == "gem":
            return {"products": [raw(GEM, 2.0)], "total_results": 1}
        page = params.get("page", 0)
        return {
            "products": [raw(f"B0P{page}{i:06d}", self.scan_price) for i in range(3)],
            "total_results": 400,
        }

    def batch_calls(self) -> list[str]:
        return [
            call.kwargs["asins"]
            for call in self.mock.call_args_list
            if call.kwargs.get("asins")
        ]

    def scan_calls(self) -> int:
        return sum(
            1
            for call in self.mock.call_args_list
            if call.args[0] == "1.0/catalog/products" and not call.kwargs.get("asins")
        )


class RuntimeError_(Exception):  # not retried by the transport (4xx-style)
    status_code = 400


@pytest.fixture
def catalog(api):
    return FakeCatalog(api)


def run(*args: str):
    return CliRunner().invoke(cli, list(args))


def flat(text: str) -> str:
    return " ".join(text.split())


FIND = ("find", "--max-price", "5", "--pages", str(PAGES), "--refresh")


def see_the_gem():
    """Show the gem in a search, as a user would, so it enters the index."""
    result = run("search", "gem", "--max-price", "5", "--pages", "1")
    assert result.exit_code == 0, result.output
    assert GEM in result.output


class TestRepricing:
    def test_a_title_outside_the_scan_comes_back_when_it_is_cheap_again(self, catalog):
        see_the_gem()

        result = run(*FIND)

        assert result.exit_code == 0, result.output
        assert catalog.batch_calls() == [GEM]
        assert GEM in result.output
        assert "Re-pricing 1 title(s) from earlier scans" in flat(result.output)
        assert catalog.scan_calls() >= PAGES

    def test_the_repriced_title_is_filtered_by_its_live_price(self, catalog):
        see_the_gem()
        catalog.gem_price = 9.0  # no longer cheap

        result = run(*FIND)

        assert catalog.batch_calls() == [GEM]
        assert GEM not in result.output

    def test_it_is_saved_in_the_session_and_recorded_in_the_history(self, catalog):
        see_the_gem()
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        with db.transaction(db.state()) as conn:
            conn.execute("UPDATE price_history SET date = ?", (yesterday,))
            conn.execute("UPDATE products SET last_seen = ?", (yesterday,))
        catalog.gem_price = 1.5

        run(*FIND)

        last = json.loads(run("last", "--json").stdout)
        assert GEM in {item["asin"] for item in last}
        assert [e["price"] for e in load_price_history(GEM)] == [2.0, 1.5]

    def test_a_title_the_scan_returned_is_not_asked_for_again(self, catalog):
        see_the_gem()
        original = catalog.get

        def scan_returns_the_gem(endpoint, **params):
            response = original(endpoint, **params)
            if endpoint == "1.0/catalog/products" and not params.get("asins"):
                response["products"].append(raw(GEM, 2.0))
            return response

        catalog.mock.side_effect = scan_returns_the_gem

        run(*FIND)

        assert catalog.batch_calls() == []

    def test_json_output_has_it_and_the_notice_goes_to_stderr(self, catalog):
        see_the_gem()

        result = CliRunner().invoke(cli, [*FIND, "--json"])

        assert result.exit_code == 0
        assert GEM in {item["asin"] for item in json.loads(result.stdout)}
        assert "Re-pricing" in result.stderr and "Re-pricing" not in result.stdout

    def test_quiet_mode_is_silent_about_it(self, catalog):
        see_the_gem()

        result = run(*FIND, "-q")

        assert "Re-pricing" not in result.output
        assert catalog.batch_calls() == [GEM]

    def test_the_session_records_how_many_were_added(self, catalog):
        see_the_gem()
        run(*FIND)

        from audible_deals.results_cache import load_result_session

        assert load_result_session().source["known_candidates"] == 1


class TestSwitchingItOff:
    def test_no_known(self, catalog):
        see_the_gem()

        result = run(*FIND, "--no-known")

        assert catalog.batch_calls() == []
        assert GEM not in result.output
        assert "--no-known" in load_command()

    def test_the_config_key(self, catalog):
        see_the_gem()
        assert run("config", "set", "known-candidates", "false").exit_code == 0

        run(*FIND)
        assert catalog.batch_calls() == []

        run(*FIND, "--known")  # the flag beats the config
        assert catalog.batch_calls() == [GEM]

    def test_a_keyword_search_is_left_alone(self, catalog):
        see_the_gem()

        run(*FIND, "--keywords", "dragon")

        assert catalog.batch_calls() == []

    def test_nothing_indexed_means_no_extra_calls(self, catalog):
        result = run(*FIND)

        assert result.exit_code == 0
        assert catalog.batch_calls() == []

    def test_titles_from_another_category_are_not_asked_for(self, catalog):
        see_the_gem()

        run(
            "find",
            "--max-price",
            "5",
            "--pages",
            "1",
            "--refresh",
            "--category",
            "CAT2",
        )

        assert catalog.batch_calls() == []

    def test_titles_the_user_filters_out_are_not_asked_for(self, catalog):
        see_the_gem()

        run(*FIND, "--min-rating", "4.9")  # the gem is rated 4.5

        assert catalog.batch_calls() == []

    def test_other_marketplaces_have_their_own_index(self, catalog):
        see_the_gem()

        run("--locale", "uk", *FIND)

        assert catalog.batch_calls() == []


def load_command() -> str:
    from audible_deals.results_cache import load_result_session

    return load_result_session().source["command"]


class TestFailureIsHarmless:
    def test_a_failed_reprice_keeps_the_scan_results(self, catalog):
        see_the_gem()
        catalog.fail_batches = True

        result = run(*FIND)

        assert result.exit_code == 0, result.output
        assert "Could not re-price titles from earlier scans" in flat(result.output)
        assert "B0P0000000" in result.output

    def test_an_interrupted_scan_skips_it(self, catalog):
        see_the_gem()
        original = catalog.get

        def interrupt(endpoint, **params):
            if endpoint == "1.0/catalog/products" and params.get("page") == 1:
                raise KeyboardInterrupt
            return original(endpoint, **params)

        catalog.mock.side_effect = interrupt

        result = run(*FIND)

        assert result.exit_code == 130
        assert catalog.batch_calls() == []


class TestPlanning:
    def _dry(self, *extra):
        result = run(*FIND, "--dry-run", "--json", *extra)
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    def test_the_extra_calls_show_up_in_the_dry_run(self, catalog):
        see_the_gem()

        plan = self._dry()

        assert plan["known_candidate_calls"] == 1
        assert plan["api_calls"] == PAGES + 1 - plan["cached_calls"]

    def test_the_text_dry_run_explains_them(self, catalog):
        see_the_gem()

        text = flat(run(*FIND, "--dry-run").output)

        assert "Known titles: up to 1 of those calls re-price titles" in text

    def test_nothing_indexed_no_extra_calls(self, catalog):
        assert self._dry()["known_candidate_calls"] == 0

    def test_no_known_means_no_extra_calls(self, catalog):
        see_the_gem()
        assert self._dry("--no-known")["known_candidate_calls"] == 0

    def test_two_batches_at_most(self, catalog):
        # 130 indexed titles in the scanned category: capped at 100 = two calls.
        from audible_deals.price_history import record_prices
        from tests.conftest import make_product

        record_prices(
            [
                make_product(
                    asin=f"B0IDX{n:05d}",
                    price=3.0,
                    series_name="",
                    category_ids=["CAT1"],
                )
                for n in range(130)
            ]
        )

        assert self._dry()["known_candidate_calls"] == 2

    def test_they_count_toward_the_call_cap(self, catalog):
        see_the_gem()

        # 59 scan pages leave room for one known-title call; 60 leave none.
        assert self._dry("--pages", "59")["known_candidate_calls"] == 1
        sixty = self._dry("--pages", "60")
        assert sixty["known_candidate_calls"] == 0
        assert sixty["api_calls"] == 60
        # An explicit override lifts the cap for the extra calls too.
        assert (
            self._dry("--pages", "60", "--allow-large-scan")["known_candidate_calls"]
            == 1
        )

    def test_a_full_scan_budget_skips_the_reprice(self, catalog):
        see_the_gem()

        result = run("find", "--max-price", "5", "--pages", "60", "--refresh")

        assert result.exit_code == 0, result.output
        assert catalog.batch_calls() == []

    def test_cached_scan_pages_leave_room_for_the_extra_calls(self, catalog):
        see_the_gem()
        run("find", "--max-price", "5", "--pages", "60", "--allow-large-scan")

        plan = json.loads(
            run(
                "find", "--max-price", "5", "--pages", "60", "--dry-run", "--json"
            ).stdout
        )

        assert plan["cached_calls"] > 0
        assert plan["known_candidate_calls"] == 1


def test_the_index_is_filled_by_what_was_shown(catalog):
    see_the_gem()

    rows = db.state().execute("SELECT asin, last_price FROM products").fetchall()

    assert rows == [(GEM, 2.0)]
