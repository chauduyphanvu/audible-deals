"""The catalog cache as users meet it: ``find``/``search`` re-runs, ``--refresh``, dry runs."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from audible_deals import cache_store, db
from audible_deals.cli import cli
from audible_deals.price_history import load_price_history
from tests.conftest import make_raw

PAGES = 4  # catalog pages the fake API has for any query


class FakeApi:
    """A tiny Audible: categories, paged catalog products and a library."""

    def __init__(self, api, price: float = 3.0):
        self.mock = api.get_mock
        self.price = price
        self.mock.side_effect = self.get

    def get(self, endpoint, **params):
        if endpoint == "1.0/catalog/categories":
            return {"categories": [{"id": "CAT1", "name": "Science Fiction"}]}
        if endpoint.startswith("1.0/catalog/categories/"):
            return {"category": {"name": "Science Fiction", "children": []}}
        if endpoint == "1.0/library":
            return {"items": [make_raw("B0OWNED001")]}
        if endpoint == "1.0/catalog/products":
            page = params.get("page", 0)
            products = [
                make_raw(
                    f"B0P{page}{index:06d}",
                    price={
                        "lowest_price": {"base": self.price},
                        "list_price": {"base": 20.0},
                    },
                    runtime_length_min=600,
                    language="english",
                )
                for index in range(3)
            ]
            return {"products": products, "total_results": PAGES * 50}
        raise AssertionError(f"unexpected endpoint {endpoint}")

    def calls(self, endpoint: str = "1.0/catalog/products") -> int:
        return sum(1 for call in self.mock.call_args_list if call.args[0] == endpoint)


@pytest.fixture
def fake_api(api):
    return FakeApi(api)


def run(*args: str):
    return CliRunner().invoke(cli, list(args))


def flat(text: str) -> str:
    """The text with the console's line wrapping undone."""
    return " ".join(text.split())


FIND = ("find", "--max-price", "10", "--pages", str(PAGES), "-n", "5")


class TestRerun:
    def test_an_immediate_rerun_makes_no_catalog_calls_and_says_so(self, fake_api):
        first = run(*FIND)
        assert first.exit_code == 0, first.output
        assert fake_api.calls() == PAGES
        assert "cached" not in first.output.lower()

        second = run(*FIND)

        assert second.exit_code == 0, second.output
        assert fake_api.calls() == PAGES  # nothing new
        assert "Using cached results" in second.output
        assert f"{PAGES} of {PAGES} pages" in flat(second.output)
        assert "--refresh" in flat(second.output)
        assert "B0P0000000" in second.output  # the results are still shown

    def test_refresh_fetches_everything_again(self, fake_api):
        run(*FIND)

        refreshed = run(*FIND, "--refresh")

        assert refreshed.exit_code == 0, refreshed.output
        assert fake_api.calls() == 2 * PAGES
        assert "Using cached results" not in refreshed.output
        # ...and the fresh pages are what the next run reuses.
        run(*FIND)
        assert fake_api.calls() == 2 * PAGES

    def test_a_different_query_is_not_served_from_the_cache(self, fake_api):
        run(*FIND)

        run(*FIND, "--keywords", "dragon")

        # Four new pages plus the exact-title probe for the keyword.
        assert fake_api.calls() == 2 * PAGES + 1

    def test_json_output_stays_clean_and_the_note_goes_to_stderr(self, fake_api):
        run(*FIND)

        result = CliRunner().invoke(cli, [*FIND, "--json"])

        assert result.exit_code == 0
        assert json.loads(result.stdout)[0]["asin"].startswith("B0P")
        assert "Using cached results" in result.stderr
        assert "Using cached results" not in result.stdout

    def test_quiet_mode_suppresses_the_note(self, fake_api):
        run(*FIND)

        assert "Using cached results" not in run(*FIND, "-q").output

    def test_search_uses_the_cache_too(self, fake_api):
        run("search", "dragon", "--pages", "2", "--max-price", "10")
        before = fake_api.calls()

        second = run("search", "dragon", "--pages", "2", "--max-price", "10")

        assert fake_api.calls() == before
        assert "Using cached results" in second.output

    def test_cache_ttl_zero_turns_it_off(self, fake_api):
        assert run("config", "set", "cache-ttl", "0").exit_code == 0

        run(*FIND)
        second = run(*FIND)

        assert fake_api.calls() == 2 * PAGES
        assert "Using cached results" not in second.output
        assert cache_store.summary() == {}

    def test_cache_ttl_is_validated(self, tmp_config):
        assert run("config", "set", "cache-ttl", "-1").exit_code != 0
        assert run("config", "set", "cache-ttl", "soon").exit_code != 0
        ok = run("config", "set", "cache-ttl", "30")
        assert ok.exit_code == 0 and "cache_ttl = 30" in ok.output

    def test_an_expired_entry_is_fetched_again(self, fake_api):
        assert run("config", "set", "cache-ttl", "10").exit_code == 0
        run(*FIND)
        with db.transaction(db.cache()) as conn:
            conn.execute("UPDATE cache_entries SET stored_at = stored_at - 601")

        run(*FIND)

        assert fake_api.calls() == 2 * PAGES


class TestPriceHistory:
    def test_live_prices_are_recorded_and_cached_ones_are_not(self, fake_api):
        run(*FIND)
        asin = "B0P0000000"
        assert [e["price"] for e in load_price_history(asin)] == [3.0]
        with db.transaction(db.state()) as conn:
            conn.execute("DELETE FROM price_history")

        run(*FIND)  # answered from the cache

        assert load_price_history(asin) == []

    def test_refresh_records_the_new_live_price(self, fake_api):
        run(*FIND)
        with db.transaction(db.state()) as conn:
            conn.execute("DELETE FROM price_history")
        fake_api.price = 2.0

        run(*FIND, "--refresh")

        assert [e["price"] for e in load_price_history("B0P0000000")] == [2.0]

    def test_cached_results_are_still_saved_for_selectors(self, fake_api):
        run(*FIND)
        run(*FIND)

        last = run("last", "--json")

        assert last.exit_code == 0, last.output
        assert json.loads(last.stdout)


class TestDryRun:
    def _dry(self, *extra):
        result = run(*FIND, "--dry-run", "--json", *extra)
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    def test_cold_cache_plans_every_call(self, fake_api):
        plan = self._dry()
        assert (plan["api_calls"], plan["cached_calls"]) == (PAGES, 0)

    def test_pages_in_the_cache_count_as_cached_not_as_calls(self, fake_api):
        run(*FIND)

        plan = self._dry()

        assert (plan["api_calls"], plan["cached_calls"]) == (0, PAGES)

    def test_refresh_plans_every_call_again(self, fake_api):
        run(*FIND)

        plan = self._dry("--refresh")

        assert (plan["api_calls"], plan["cached_calls"]) == (PAGES, 0)

    def test_text_dry_run_shows_the_cached_count(self, fake_api):
        run(*FIND)

        result = run(*FIND, "--dry-run")

        assert f"API calls: 0 ({PAGES} cached)" in result.output

    def test_a_genre_is_resolved_from_the_stored_category_list(self, fake_api):
        genre = (*FIND, "--genre", "science fiction")
        assert run(*genre).exit_code == 0

        plan = json.loads(run(*genre, "--dry-run", "--json").stdout)

        assert (plan["api_calls"], plan["cached_calls"]) == (0, PAGES)


class TestCallLimit:
    def test_a_fully_cached_large_scan_needs_no_override(self, fake_api):
        big = ("find", "--max-price", "10", "--deep", "--pages", "21")
        refused = run(*big)
        assert refused.exit_code != 0
        assert "--allow-large-scan" in refused.output

        # The fake API only has PAGES pages per query, so warming the cache is
        # cheap, but the plan is still 63 calls.
        assert run(*big, "--allow-large-scan").exit_code == 0

        again = run(*big)
        assert again.exit_code == 0, again.output


class TestOwnedTitles:
    SKIP = (*FIND, "--skip-owned")

    def test_the_owned_list_is_reused_for_a_day(self, fake_api):
        run(*self.SKIP)
        assert fake_api.calls("1.0/library") == 1

        second = run(*self.SKIP)

        assert fake_api.calls("1.0/library") == 1
        assert "owned titles" in flat(second.output)

    def test_refresh_fetches_the_library_again(self, fake_api):
        run(*self.SKIP)

        run(*self.SKIP, "--refresh")

        assert fake_api.calls("1.0/library") == 2

    def test_the_library_command_refreshes_the_stored_list(self, fake_api):
        library = run("library", "--json")
        assert library.exit_code == 0, library.output
        assert cache_store.get("library", "us") == ["B0OWNED001"]

        run(*self.SKIP)

        assert fake_api.calls("1.0/library") == 1  # only the `library` call


class TestCommandsThatMustBeLive:
    def test_cache_is_not_part_of_other_commands(self, fake_api):
        run(*FIND)
        before = fake_api.calls()

        # Wishlist checks and notifications read prices straight from the API.
        from audible_deals.client import DealsClient

        client = DealsClient(locale="us")
        client.get_products_batch(["B0A"])
        client.get_products_batch(["B0A"])
        list(client.search_pages(keywords="x", max_pages=1))

        assert fake_api.calls() == before + 3
