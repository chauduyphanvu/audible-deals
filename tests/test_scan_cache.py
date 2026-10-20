"""The catalog cache: keys, freshness, accounting, and how the client uses it."""

from __future__ import annotations

import click
import pytest

from audible_deals import cache_store, constants, db, scan_cache
from audible_deals.catalog_workflow import (
    bind_catalog_categories,
    build_find_scan_plan,
    estimate_cached_calls,
)
from audible_deals.client import CatalogSearchRequest, DealsClient
from audible_deals.scan_cache import ScanCache
from tests.conftest import make_product, make_raw


def page(*asins: str, total: int | None = None) -> dict:
    return {
        "products": [make_raw(asin) for asin in asins],
        "total_results": len(asins) if total is None else total,
    }


@pytest.fixture
def client(api):
    return DealsClient(auth_file=api.tmp_path / "auth.json", locale="us")


class TestTtl:
    @pytest.mark.parametrize(
        ("config", "minutes"),
        [
            ({}, 120),
            ({"cache_ttl": 30}, 30),
            ({"cache_ttl": 0}, 0),
            ({"cache_ttl": -5}, 0),
            ({"cache_ttl": 10**9}, constants.CATALOG_CACHE_MAX_TTL_MINUTES),
            ({"cache_ttl": "soon"}, 120),
            ({"cache_ttl": True}, 120),
            ({"cache_ttl": 1.5}, 120),
        ],
    )
    def test_configured_minutes(self, config, minutes):
        assert scan_cache.ttl_minutes(config) == minutes

    def test_zero_turns_the_cache_off(self):
        assert ScanCache.from_config({"cache_ttl": 0}) is None

    def test_default_cache_is_two_hours(self):
        cache = ScanCache.from_config({})
        assert cache is not None
        assert cache.ttl_s == 120 * 60
        assert cache.refresh is False

    def test_refresh_is_carried(self):
        cache = ScanCache.from_config({}, refresh=True)
        assert cache is not None and cache.refresh


class TestKeys:
    def test_parameter_order_does_not_matter(self):
        a = scan_cache.entry_key("e", {"a": 1, "b": 2})
        b = scan_cache.entry_key("e", {"b": 2, "a": 1})
        assert a == b

    def test_every_parameter_is_part_of_the_key(self):
        base = scan_cache.catalog_products_params(keywords="x", page=0)
        keys = {
            scan_cache.entry_key(scan_cache.CATALOG_ENDPOINT, base),
            scan_cache.entry_key(
                scan_cache.CATALOG_ENDPOINT,
                scan_cache.catalog_products_params(keywords="x", page=1),
            ),
            scan_cache.entry_key(
                scan_cache.CATALOG_ENDPOINT,
                scan_cache.catalog_products_params(keywords="y", page=0),
            ),
            scan_cache.entry_key(
                scan_cache.CATALOG_ENDPOINT,
                scan_cache.catalog_products_params(
                    keywords="x", page=0, sort_by="BestSellers"
                ),
            ),
            scan_cache.entry_key(
                scan_cache.CATALOG_ENDPOINT,
                scan_cache.catalog_products_params(
                    keywords="x", page=0, category_id="123"
                ),
            ),
            scan_cache.entry_key("other", base),
            # Response groups are part of the request, so part of the key.
            scan_cache.entry_key(
                scan_cache.CATALOG_ENDPOINT,
                {**base, "response_groups": "product_attrs"},
            ),
        }
        assert len(keys) == 7

    def test_title_and_keyword_probes_differ(self):
        assert scan_cache.catalog_products_params(title="x") != (
            scan_cache.catalog_products_params(keywords="x")
        )

    def test_page_size_is_capped(self):
        params = scan_cache.catalog_products_params(num_results=500)
        assert params["num_results"] == constants.MAX_PAGE_SIZE


class TestProductsRoundTrip:
    def test_pack_and_unpack_preserve_every_field(self):
        product = make_product(asin="B0ROUND001", price=3.5, authors=["Ada", "Bram"])
        [restored] = scan_cache.unpack_products(scan_cache.pack_products([product]))
        assert restored == product

    def test_unknown_fields_are_ignored_and_missing_ones_default(self):
        [restored] = scan_cache.unpack_products(
            [{"asin": "B0OLD00001", "title": "Old", "from_the_future": 1}]
        )
        assert restored.asin == "B0OLD00001"
        assert restored.price is None

    @pytest.mark.parametrize("bad", [None, {}, "x", [1], [{"asin": "A"}]])
    def test_malformed_payloads_are_rejected(self, bad):
        with pytest.raises(ValueError):
            scan_cache.unpack_products(bad)


class TestScanCache:
    def test_a_stored_entry_is_found_with_its_age(self, tmp_config):
        cache = ScanCache(600)
        cache.store("us", "e", {"p": 1}, {"v": 1})

        value, age = cache.lookup("us", "e", {"p": 1})

        assert value == {"v": 1}
        assert 0 <= age < 5

    def test_entries_are_per_marketplace(self, tmp_config):
        cache = ScanCache(600)
        cache.store("us", "e", {}, {"v": "us"})

        assert cache.lookup("uk", "e", {}) is None

    def test_stale_entries_are_ignored(self, tmp_config):
        ScanCache(600).store("us", "e", {}, {"v": 1})
        with db.transaction(db.cache()) as conn:
            conn.execute("UPDATE cache_entries SET stored_at = stored_at - 601")

        assert ScanCache(600).lookup("us", "e", {}) is None
        assert ScanCache(700).lookup("us", "e", {}) is not None

    def test_refresh_skips_reads_but_still_writes(self, tmp_config):
        ScanCache(600).store("us", "e", {}, {"v": "old"})
        refreshing = ScanCache(600, refresh=True)

        assert refreshing.lookup("us", "e", {}) is None
        refreshing.store("us", "e", {}, {"v": "new"})

        assert ScanCache(600).lookup("us", "e", {})[0] == {"v": "new"}

    def test_accounting_separates_cached_and_live_pages(self, tmp_config):
        cache = ScanCache(600)
        cache.note_page([make_product(asin="B0CACHED01")], 30.0)
        cache.note_page([make_product(asin="B0CACHED02")], 90.0)
        cache.note_page([make_product(asin="B0LIVE0001")], None)

        use = cache.use()

        assert (use.pages_cached, use.pages_live) == (2, 1)
        assert use.oldest_age_s == 90.0
        assert cache.cached_asins() == {"B0CACHED01", "B0CACHED02"}

    def test_a_product_seen_live_is_still_reported_as_cached_if_any_copy_was(
        self, tmp_config
    ):
        # Which copy a scan keeps is not tracked, so the safe answer is "do
        # not trust this price": it stays out of the price history.
        cache = ScanCache(600)
        cache.note_page([make_product(asin="B0BOTH0001")], 30.0)
        cache.note_page([make_product(asin="B0BOTH0001")], None)

        assert "B0BOTH0001" in cache.cached_asins()

    def test_nothing_used_reports_no_cache_use(self, tmp_config):
        use = ScanCache(600).use()
        assert not use.any

    def test_old_entries_are_pruned_on_the_first_write(self, tmp_config):
        cache_store.put("catalog:us", "ancient", {"v": 1})
        cache_store.put("categories", "us", [{"id": "1", "name": "A"}])
        with db.transaction(db.cache()) as conn:
            conn.execute("UPDATE cache_entries SET stored_at = stored_at - 10 * 86400")

        ScanCache(600).store("us", "e", {}, {"v": 1})

        assert cache_store.get("catalog:us", "ancient") is None
        # Other namespaces are not this cache's to prune.
        assert cache_store.get("categories", "us") is not None


class TestLibraryCache:
    def test_round_trip(self, tmp_config):
        cache = ScanCache(600)
        cache.library_store("us", ["B0B", "B0A"])

        assert ScanCache(600).library_lookup("us") == {"B0A", "B0B"}
        assert ScanCache(600).library_lookup("uk") is None

    def test_refresh_ignores_the_stored_list(self, tmp_config):
        ScanCache(600).library_store("us", ["B0A"])

        assert ScanCache(600, refresh=True).library_lookup("us") is None

    def test_older_than_a_day_is_stale(self, tmp_config):
        ScanCache(600).library_store("us", ["B0A"])
        with db.transaction(db.cache()) as conn:
            conn.execute("UPDATE cache_entries SET stored_at = stored_at - 86401")

        assert ScanCache(600).library_lookup("us") is None

    def test_using_it_records_its_age(self, tmp_config):
        ScanCache(600).library_store("us", ["B0A"])
        cache = ScanCache(600)
        cache.library_lookup("us")

        assert cache.use().library_age_s is not None
        assert cache.use().any

    @pytest.mark.parametrize("bad", ["B0A", [1, 2], {"a": 1}])
    def test_an_unreadable_value_is_a_miss(self, tmp_config, bad):
        cache_store.put("library", "us", bad)

        assert ScanCache(600).library_lookup("us") is None


class TestClientSearchCache:
    def test_second_identical_page_comes_from_the_cache(self, api, client):
        api.get_mock.return_value = page("B0A", "B0B", total=2)
        cache = ScanCache(600)

        first = client.search_catalog(keywords="dragon", cache=cache)
        second = client.search_catalog(keywords="dragon", cache=cache)

        assert api.get_mock.call_count == 1
        assert first == second
        assert [p.asin for p in second[0]] == ["B0A", "B0B"]
        assert cache.use().pages_cached == 1
        assert cache.use().pages_live == 1

    def test_without_a_cache_every_call_is_live(self, api, client):
        api.get_mock.return_value = page("B0A")

        client.search_catalog(keywords="dragon")
        client.search_catalog(keywords="dragon")

        assert api.get_mock.call_count == 2

    def test_different_queries_do_not_share_entries(self, api, client):
        api.get_mock.return_value = page("B0A")
        cache = ScanCache(600)

        client.search_catalog(keywords="dragon", cache=cache)
        client.search_catalog(keywords="dragons", cache=cache)
        client.search_catalog(keywords="dragon", sort_by="BestSellers", cache=cache)
        client.search_catalog(keywords="dragon", page=1, cache=cache)

        assert api.get_mock.call_count == 4

    def test_the_cache_survives_a_new_client_and_process_state(self, api, tmp_config):
        api.get_mock.return_value = page("B0A")
        DealsClient(auth_file=tmp_config / "auth.json").search_catalog(
            keywords="x", cache=ScanCache(600)
        )
        db.close_all()

        DealsClient(auth_file=tmp_config / "auth.json").search_catalog(
            keywords="x", cache=ScanCache(600)
        )

        assert api.get_mock.call_count == 1

    def test_other_marketplaces_are_separate(self, api, tmp_config):
        api.get_mock.return_value = page("B0A")
        cache = ScanCache(600)
        DealsClient(auth_file=tmp_config / "auth.json", locale="us").search_catalog(
            keywords="x", cache=cache
        )

        DealsClient(auth_file=tmp_config / "auth.json", locale="uk").search_catalog(
            keywords="x", cache=cache
        )

        assert api.get_mock.call_count == 2

    def test_refresh_fetches_live_and_updates_the_entry(self, api, client):
        api.get_mock.return_value = page("B0OLD")
        client.search_catalog(keywords="x", cache=ScanCache(600))
        api.get_mock.return_value = page("B0NEW")

        refreshed, _ = client.search_catalog(
            keywords="x", cache=ScanCache(600, refresh=True)
        )
        later, _ = client.search_catalog(keywords="x", cache=ScanCache(600))

        assert [p.asin for p in refreshed] == ["B0NEW"]
        assert [p.asin for p in later] == ["B0NEW"]
        assert api.get_mock.call_count == 2

    def test_errors_are_never_cached(self, api, client):
        api.get_mock.side_effect = click.ClickException("boom")
        cache = ScanCache(600)
        with pytest.raises(click.ClickException):
            client.search_catalog(keywords="x", cache=cache)

        api.get_mock.side_effect = None
        api.get_mock.return_value = page("B0A")
        products, _ = client.search_catalog(keywords="x", cache=cache)

        assert [p.asin for p in products] == ["B0A"]
        assert api.get_mock.call_count == 2
        assert cache.use().pages_cached == 0

    def test_a_non_integer_total_is_returned_but_not_stored(self, api, client):
        api.get_mock.return_value = {
            "products": [make_raw("B0A")],
            "total_results": "many",
        }
        cache = ScanCache(600)

        client.search_catalog(keywords="x", cache=cache)
        client.search_catalog(keywords="x", cache=cache)

        assert api.get_mock.call_count == 2

    def test_a_stale_entry_is_refetched(self, api, client):
        api.get_mock.return_value = page("B0A")
        client.search_catalog(keywords="x", cache=ScanCache(600))
        with db.transaction(db.cache()) as conn:
            conn.execute("UPDATE cache_entries SET stored_at = stored_at - 601")

        client.search_catalog(keywords="x", cache=ScanCache(600))

        assert api.get_mock.call_count == 2

    @pytest.mark.parametrize(
        "damaged",
        [
            "not json at all",
            '{"total": 1}',
            '{"total": "1", "products": []}',
            '{"total": 1, "products": "x"}',
            '{"total": 1, "products": [1]}',
            "[]",
        ],
    )
    def test_a_damaged_entry_is_refetched_not_fatal(self, api, client, damaged):
        api.get_mock.return_value = page("B0A")
        client.search_catalog(keywords="x", cache=ScanCache(600))
        with db.transaction(db.cache()) as conn:
            conn.execute("UPDATE cache_entries SET value = ?", (damaged,))

        products, _ = client.search_catalog(keywords="x", cache=ScanCache(600))

        assert [p.asin for p in products] == ["B0A"]
        assert api.get_mock.call_count == 2

    def test_an_unwritable_cache_never_fails_the_search(self, api, client, monkeypatch):
        api.get_mock.return_value = page("B0A")

        def broken(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(db, "cache", broken)

        products, _ = client.search_catalog(keywords="x", cache=ScanCache(600))

        assert [p.asin for p in products] == ["B0A"]

    def test_search_segments_use_the_request_cache(self, api, client):
        api.get_mock.return_value = page("B0A", total=1)
        cache = ScanCache(600)
        request = CatalogSearchRequest(keywords="x", max_pages=3, cache=cache)

        client.search_segments([request])
        results = client.search_segments([request])

        assert api.get_mock.call_count == 1
        assert [p.asin for p in results[0].pages[0][0]] == ["B0A"]

    def test_search_pages_never_uses_a_cache(self, api, client):
        # notify's author watches call search_pages; they need live prices.
        api.get_mock.return_value = page("B0A", total=1)

        list(client.search_pages(keywords="x", max_pages=1))
        list(client.search_pages(keywords="x", max_pages=1))

        assert api.get_mock.call_count == 2


class TestClientOtherCaches:
    def test_library_asins_use_and_refresh_the_stored_list(self, api, client):
        api.get_mock.return_value = {"items": [{"asin": "B0OWNED001"}]}
        client.get_library_asins(cache=ScanCache(600))

        fresh_client = DealsClient(auth_file=api.tmp_path / "auth.json")
        assert fresh_client.get_library_asins(cache=ScanCache(600)) == {"B0OWNED001"}
        assert api.get_mock.call_count == 1

        api.get_mock.return_value = {"items": [{"asin": "B0OWNED002"}]}
        refreshed = DealsClient(auth_file=api.tmp_path / "auth.json")
        assert refreshed.get_library_asins(cache=ScanCache(600, refresh=True)) == {
            "B0OWNED002"
        }
        assert ScanCache(600).library_lookup("us") == {"B0OWNED002"}

    def test_an_empty_library_is_a_valid_cache_hit(self, api, client):
        """A user with no books is a real answer, not a miss: no refetch, and the
        note about what came from the cache matches what happened."""
        api.get_mock.return_value = {"items": []}
        assert client.get_library_asins(cache=ScanCache(600)) == set()
        assert api.get_mock.call_count == 1

        fresh_client = DealsClient(auth_file=api.tmp_path / "auth.json")
        cache = ScanCache(600)
        assert fresh_client.get_library_asins(cache=cache) == set()

        assert api.get_mock.call_count == 1  # served from the cache
        assert cache.use().library_age_s is not None
        assert cache.use().any

    def test_library_asins_without_a_cache_stay_live_and_unrecorded(self, api, client):
        api.get_mock.return_value = {"items": [{"asin": "B0OWNED001"}]}
        client.get_library_asins()
        DealsClient(auth_file=api.tmp_path / "auth.json").get_library_asins()

        assert api.get_mock.call_count == 2
        assert ScanCache(600).library_lookup("us") is None

    def test_series_relationships_and_batches_are_cached(self, api, client):
        def respond(endpoint, **params):
            if params.get("response_groups") == "relationships":
                return {
                    "product": {
                        "relationships": [
                            {"asin": "B0CHILD001", "relationship_to_product": "child"}
                        ]
                    }
                }
            return page("B0CHILD001")

        api.get_mock.side_effect = respond
        cache = ScanCache(600)
        first = client.get_series_products_many(["B0SERIES01"], cache=cache)
        second = client.get_series_products_many(["B0SERIES01"], cache=cache)

        assert api.get_mock.call_count == 2  # one relationship call, one batch
        assert [p.asin for p in second.products["B0SERIES01"]] == ["B0CHILD001"]
        assert first.products == second.products

    def test_series_without_a_cache_are_live(self, api, client):
        api.get_mock.side_effect = lambda endpoint, **params: (
            {"product": {"relationships": []}}
            if params.get("response_groups") == "relationships"
            else page()
        )

        client.get_series_products_many(["B0SERIES01"])
        client.get_series_products_many(["B0SERIES01"])

        assert api.get_mock.call_count == 2

    def test_category_names_are_remembered_across_clients(self, api, client):
        api.get_mock.return_value = {"category": {"name": "Science Fiction"}}

        assert client.get_category_name("18580606011") == "Science Fiction"
        again = DealsClient(auth_file=api.tmp_path / "auth.json")
        assert again.get_category_name("18580606011") == "Science Fiction"

        assert api.get_mock.call_count == 1

    def test_a_failed_name_lookup_is_not_remembered(self, api, client):
        api.get_mock.side_effect = click.ClickException("down")
        assert client.get_category_name("18580606011") == "18580606011"

        api.get_mock.side_effect = None
        api.get_mock.return_value = {"category": {"name": "Science Fiction"}}
        assert client.get_category_name("18580606011") == "Science Fiction"

    def test_a_missing_name_is_not_remembered(self, api, client):
        api.get_mock.return_value = {"category": {}}
        assert client.get_category_name("18580606011") == "18580606011"
        api.get_mock.return_value = {"category": {"name": "Science Fiction"}}

        assert client.get_category_name("18580606011") == "Science Fiction"


class TestEstimate:
    def _plan(self, pages=3, **kwargs):
        plan = build_find_scan_plan("", pages=pages, **kwargs)
        return bind_catalog_categories(plan, [""])

    def test_nothing_cached_means_nothing_estimated(self, tmp_config):
        assert estimate_cached_calls(self._plan(), "us", ScanCache(600)) == 0

    def test_without_a_cache_or_with_refresh_it_is_zero(self, tmp_config):
        assert estimate_cached_calls(self._plan(), "us", None) == 0
        assert (
            estimate_cached_calls(self._plan(), "us", ScanCache(600, refresh=True)) == 0
        )

    def _store_pages(self, plan, cache, *, pages_per_request=None):
        """Store a page for each page of each request in *plan*; returns the keys."""
        from audible_deals.catalog_workflow import _plan_requests

        keys = []
        for request, _meta in _plan_requests(plan, None):
            count = pages_per_request or request.max_pages
            for number in range(count):
                params = scan_cache.catalog_products_params(
                    keywords=request.keywords,
                    title=request.title,
                    category_id=request.category_id,
                    sort_by=request.sort_by,
                    page=number,
                )
                cache.store(
                    "us",
                    scan_cache.CATALOG_ENDPOINT,
                    params,
                    {
                        "total": 100_000,
                        "products": scan_cache.pack_products([make_product()]),
                    },
                )
                keys.append(
                    (number, scan_cache.entry_key(scan_cache.CATALOG_ENDPOINT, params))
                )
        return keys

    def test_every_cached_page_is_counted_but_only_the_first_is_decoded(
        self, tmp_config, monkeypatch
    ):
        from audible_deals.catalog_workflow import _plan_requests

        plan = self._plan(pages=6)
        cache = ScanCache(600)
        self._store_pages(plan, cache)
        requests = _plan_requests(plan, None)
        decoded = []
        real = cache_store.get_entry

        def counting(namespace, key, **kwargs):
            decoded.append(key)
            return real(namespace, key, **kwargs)

        monkeypatch.setattr(cache_store, "get_entry", counting)

        counted = estimate_cached_calls(plan, "us", cache)

        assert counted == sum(request.max_pages for request, _ in requests)
        assert counted > len(requests)  # several pages per request
        # one decode per request (its first page); the other pages are probed
        assert len(decoded) == len(requests)

    def test_stale_and_missing_later_pages_are_not_counted(self, tmp_config):
        from audible_deals.catalog_workflow import _plan_requests

        plan = self._plan(pages=4)
        cache = ScanCache(600)
        keys = self._store_pages(plan, cache)
        conn = db.cache()
        # page 2 of every request is older than the lifetime; page 3 is gone
        with db.transaction(conn):
            for number, key in keys:
                if number == 2:
                    conn.execute(
                        "UPDATE cache_entries SET stored_at = stored_at - 100000 "
                        "WHERE key = ?",
                        (key,),
                    )
                elif number == 3:
                    conn.execute("DELETE FROM cache_entries WHERE key = ?", (key,))
        full = [r for r, _ in _plan_requests(plan, None) if r.max_pages == 4]

        counted = estimate_cached_calls(plan, "us", cache)

        # pages 0 and 1 of each four-page request count (a single-page probe
        # request, if the plan has one, counts its only page)
        singles = len(_plan_requests(plan, None)) - len(full)
        assert counted == 2 * len(full) + singles

    def test_unresolved_categories_cannot_be_estimated(self, tmp_config):
        plan = build_find_scan_plan("", pages=3, category_ids=None)
        assert estimate_cached_calls(plan, "us", ScanCache(600)) == 0

    def test_counts_the_pages_a_previous_scan_stored(self, api, client):
        api.get_mock.return_value = page("B0A", total=10_000)
        cache = ScanCache(600)
        plan = self._plan(pages=3)

        from audible_deals.catalog_workflow import execute_catalog_scan

        execute_catalog_scan(client, plan, cache=cache)
        calls_after_scan = api.get_mock.call_count

        assert estimate_cached_calls(plan, "us", ScanCache(600)) == calls_after_scan
        assert estimate_cached_calls(self._plan(pages=2), "us", ScanCache(600)) == 2
        # A page that was never fetched is not counted.
        assert estimate_cached_calls(self._plan(pages=5), "us", ScanCache(600)) == 3

    def test_a_deep_plan_reuses_the_pages_of_a_single_sort_plan(self, api, client):
        api.get_mock.return_value = page("B0A", total=10_000)
        from audible_deals.catalog_workflow import execute_catalog_scan

        execute_catalog_scan(client, self._plan(pages=2), cache=ScanCache(600))

        # Only the BestSellers sort was fetched; the deep plan adds two more.
        deep = self._plan(pages=2, deep=True)
        assert estimate_cached_calls(deep, "us", ScanCache(600)) == 2
        assert deep.total_calls == 6


class TestThreadSafety:
    """Worker threads fetch pages concurrently and all go through one connection."""

    def test_concurrent_lookups_and_stores_never_lose_an_entry(self, tmp_config):
        import threading

        cache = ScanCache(600)
        for index in range(8):
            cache.store("us", "e", {"i": index}, {"v": index})
        misses: list[int] = []
        barrier = threading.Barrier(8)

        def work(worker: int) -> None:
            barrier.wait()
            for round_ in range(150):
                index = (worker + round_) % 8
                if cache.lookup("us", "e", {"i": index}) is None:
                    misses.append(index)
                cache.store("us", "e", {"i": index}, {"v": index})

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert not any(thread.is_alive() for thread in threads)
        assert misses == []


class TestLiveOnlyCommands:
    """track run, notify, watch, wishlist checks and monitors must see live prices."""

    ALLOWED = {
        "cli/catalog.py",  # find, search
        "cli/foryou.py",  # for-me
        "cli/series.py",  # series
        "cli/library.py",  # library (refreshes the stored owned list)
        "cli/helpers.py",  # the shared builder
        "scan_cache.py",
    }

    def test_only_the_scan_commands_build_a_cache(self):
        from pathlib import Path

        import audible_deals

        root = Path(audible_deals.__file__).parent
        users = {
            str(path.relative_to(root))
            for path in root.rglob("*.py")
            if "ScanCache.from_config(" in path.read_text(encoding="utf-8")
            or "_scan_cache(" in path.read_text(encoding="utf-8")
        }

        assert users <= self.ALLOWED, f"unexpected cache users: {users - self.ALLOWED}"

    def test_monitor_scans_pass_no_cache(self, tmp_config, monkeypatch):
        from contextlib import contextmanager

        from audible_deals.monitor_service import (
            MonitorDefinition,
            MonitorRuntime,
            scan_monitor,
        )
        from tests.test_monitor_service import definition

        seen_kwargs = []

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        @contextmanager
        def progress(*args, **kwargs):
            yield None

        def fake_scan(*args, **kwargs):
            seen_kwargs.append(kwargs)
            return []

        monkeypatch.setattr(
            "audible_deals.monitor_service.execute_catalog_scan", fake_scan
        )
        runtime = MonitorRuntime(
            get_client=lambda locale: Client(),
            resolve_categories=lambda *args: ("", "", set()),
            resolve_skip_asins=lambda *args: None,
            progress=progress,
        )

        scan_monitor(MonitorDefinition.from_dict(definition("live", 1)), runtime)

        assert seen_kwargs == [{}]

    def test_the_wishlist_and_notify_paths_call_the_client_without_a_cache(self):
        import inspect

        from audible_deals import notification_service

        source = inspect.getsource(notification_service.collect_target_hits)
        assert "cache" not in source
