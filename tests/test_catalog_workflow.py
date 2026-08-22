"""Catalog serialization and scan workflow behavior."""

from __future__ import annotations

import json

import pytest

from audible_deals.catalog_workflow import (
    build_search_scan_plan,
    execute_catalog_scan,
    rank_catalog_relevance,
)
from audible_deals.presentation.terminal import catalog_scan_progress
from audible_deals.result_models import (
    CatalogScanPlan,
    CatalogScanProgress,
)
from audible_deals.serialization import (
    deserialize_product as _deserialize_product,
)
from audible_deals.serialization import (
    export_products as _export_products,
)
from audible_deals.serialization import (
    serialize_product as _serialize_product,
)
from tests.conftest import make_product


class TestSerializeProduct:
    def test_includes_computed_fields(self):
        p = make_product(price=10.0, list_price=20.0, length_minutes=600)
        d = _serialize_product(p)
        assert d["full_title"] == p.full_title
        assert d["hours"] == p.hours
        assert d["discount_pct"] == p.discount_pct
        assert d["url"] == p.url
        assert "price_per_hour" in d

    def test_rounds_prices(self):
        p = make_product(price=1.9299999, list_price=10.1800001)
        d = _serialize_product(p)
        assert d["price"] == 1.93
        assert d["list_price"] == 10.18

    def test_none_price(self):
        p = make_product(price=None, list_price=None)
        d = _serialize_product(p)
        assert d["price"] is None
        assert d["list_price"] is None
        assert d["price_per_hour"] is None


class TestExportProducts:
    def test_json_export(self, tmp_path):
        products = [make_product(asin="E1"), make_product(asin="E2")]
        path = tmp_path / "out.json"
        _export_products(products, path)
        data = json.loads(path.read_text())
        assert len(data) == 2
        assert data[0]["asin"] == "E1"

    def test_csv_export(self, tmp_path):
        products = [make_product(asin="E1")]
        path = tmp_path / "out.csv"
        _export_products(products, path)
        content = path.read_text()
        assert "asin" in content
        assert "E1" in content

    def test_empty_csv(self, tmp_path):
        path = tmp_path / "empty.csv"
        _export_products([], path)
        assert path.read_text() == ""

    @pytest.mark.parametrize("suffix", ["json", "csv"])
    def test_export_uses_utf8_for_non_ascii_text(self, tmp_path, suffix):
        path = tmp_path / f"out.{suffix}"
        _export_products([make_product(title="Café 東京")], path)
        assert "Café 東京" in path.read_text(encoding="utf-8")

    def test_unsupported_format(self, tmp_path):
        import click

        path = tmp_path / "out.xml"
        with pytest.raises(click.BadParameter, match="Unsupported"):
            _export_products([make_product()], path)


class TestDeserializeProduct:
    def test_round_trip(self):
        p = make_product(asin="RT1", price=4.99, list_price=12.99)
        d = _serialize_product(p)
        p2 = _deserialize_product(d)
        assert p2.asin == p.asin
        assert p2.price == p.price
        assert p2.title == p.title
        assert p2.authors == p.authors

    def test_extra_keys_ignored(self):
        """Extra keys from serialization (computed fields) are silently ignored."""
        p = make_product(asin="EK1")
        d = _serialize_product(p)
        # d has extra keys like full_title, hours, discount_pct, price_per_hour, url
        p2 = _deserialize_product(d)
        assert p2.asin == "EK1"

    def test_missing_optional_fields(self):
        """Minimal dict with only required fields works."""
        d = {
            "asin": "MIN1",
            "title": "Minimal",
            "subtitle": "",
            "authors": ["A"],
            "narrators": [],
            "publisher": "",
            "price": None,
            "list_price": None,
            "length_minutes": 0,
            "rating": 0.0,
            "num_ratings": 0,
            "categories": [],
            "category_ids": [],
            "series_name": "",
            "series_position": "",
            "language": "english",
            "release_date": "",
            "in_plus_catalog": False,
        }
        p = _deserialize_product(d)
        assert p.asin == "MIN1"

    def test_corrupt_dict_returns_none(self):
        """Dicts missing required fields return None instead of crashing."""
        assert _deserialize_product({}) is None
        assert _deserialize_product({"price": 5.0}) is None


class TestFetchWithProgress:
    def test_scan_plan_is_frozen_and_owns_request_counts(self):
        plan = CatalogScanPlan.create(
            queries=["one", "two"],
            category_ids=["a", "b"],
            sort_orders=["BestSellers", "-ReleaseDate", "AvgRating"],
            exact_probe_queries=["one", "two"],
            pages=4,
        )

        assert plan.queries == ("one", "two")
        assert plan.category_multiplier == 2
        assert plan.broad_calls == 48
        assert plan.probe_calls == 4
        assert plan.total_calls == 52
        assert plan.max_items == 2600
        with pytest.raises(AttributeError):
            plan.pages = 2

        unresolved = CatalogScanPlan.create(
            queries=[""],
            category_ids=None,
            sort_orders=["BestSellers"],
            pages=1,
        )
        assert unresolved.category_multiplier is None
        assert unresolved.total_calls is None
        assert unresolved.max_items is None

    def test_rich_adapter_preserves_caller_description(self, monkeypatch):
        class ProgressRecorder:
            def __init__(self):
                self.description = None
                self.updates = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def add_task(self, description, **kwargs):
                self.description = description
                return 1

            def update(self, task, **kwargs):
                self.updates.append(kwargs)

        recorder = ProgressRecorder()
        monkeypatch.setattr(
            "audible_deals.presentation.terminal.create_scan_progress", lambda: recorder
        )
        plan = CatalogScanPlan.create(
            queries=["query"],
            category_ids=["category"],
            sort_orders=["Relevance"],
            pages=1,
        )

        with catalog_scan_progress(plan, "Scanning selected category") as update:
            update(CatalogScanProgress("Searching 'query'", 1, 1, 3))

        assert recorder.description == "Scanning selected category"
        assert recorder.updates == [{"total": 1, "completed": 1, "items": 3}]

    def test_single_sort_no_dedup(self, mock_client, tmp_config):
        """Single sort order returns all products."""
        products = [
            make_product(asin="FP1"),
            make_product(asin="FP2"),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])

        plan = CatalogScanPlan.create(
            queries=[""],
            category_ids=[""],
            sort_orders=["BestSellers"],
            pages=1,
        )
        result = execute_catalog_scan(mock_client, plan)
        assert {p.asin for p in result} == {"FP1", "FP2"}

    def test_multi_sort_deduplicates(self, mock_client, tmp_config):
        """Multiple sort orders deduplicate overlapping ASINs."""
        pass1 = [make_product(asin="MD1"), make_product(asin="MD2")]
        pass2 = [make_product(asin="MD2"), make_product(asin="MD3")]  # MD2 overlaps

        call_count = 0

        def fake_search_pages(**kwargs):
            nonlocal call_count
            data = [pass1, pass2][call_count]
            call_count += 1
            yield data, 1, len(data)

        mock_client.search_pages.side_effect = fake_search_pages

        plan = CatalogScanPlan.create(
            queries=[""],
            category_ids=[""],
            sort_orders=["BestSellers", "AvgRating"],
            pages=1,
        )
        result = execute_catalog_scan(mock_client, plan)
        asins = [p.asin for p in result]
        assert sorted(asins) == ["MD1", "MD2", "MD3"]


class TestExactTitleSearch:
    def test_broad_then_title_merge_dedup_and_relevance_tiers(
        self, mock_client, tmp_config
    ):
        broad = [
            make_product(asin="OTHER", title="Unrelated", authors=["Someone"]),
            make_product(
                asin="AUTHORPHRASE", title="Elsewhere", authors=["The Dune Writer"]
            ),
            make_product(asin="TITLEPHRASE", title="Dune Messiah"),
            make_product(asin="EXACTAUTHOR", title="Biography", authors=["DUNE"]),
        ]
        exact = [
            make_product(asin="EXACTTITLE", title="  dune "),
            make_product(asin="TITLEPHRASE", title="Dune Messiah"),
        ]
        calls = []

        def fake_search_pages(**kwargs):
            calls.append(kwargs)
            if kwargs.get("title"):
                yield exact, 1, len(exact)
            else:
                yield broad, 1, len(broad)

        mock_client.search_pages.side_effect = fake_search_pages
        plan = CatalogScanPlan.create(
            queries=["Dune"],
            category_ids=["fiction"],
            sort_orders=["Relevance"],
            exact_probe_queries=["Dune"],
            pages=2,
        )
        products = execute_catalog_scan(mock_client, plan)

        assert [call.get("title") for call in calls] == [None, "Dune"]
        assert calls[1]["category_id"] == "fiction"
        assert calls[1]["sort_by"] == "Relevance"
        assert calls[1]["max_pages"] == 1
        assert [p.asin for p in products] == [
            "EXACTTITLE",
            "EXACTAUTHOR",
            "TITLEPHRASE",
            "AUTHORPHRASE",
            "OTHER",
        ]

    def test_probe_failure_retains_broad_results(self, mock_client, tmp_config, caplog):
        broad = [make_product(asin="BROAD", title="Broad")]

        def fake_search_pages(**kwargs):
            if kwargs.get("title"):
                raise RuntimeError("title unavailable")
            yield broad, 1, 1

        mock_client.search_pages.side_effect = fake_search_pages
        with caplog.at_level("INFO", logger="audible_deals"):
            plan = build_search_scan_plan("query", pages=1)
            products = execute_catalog_scan(
                mock_client,
                plan,
            )
        assert products == broad
        assert "Exact-title probe failed" in caplog.text

    def test_probe_failure_discards_products_yielded_before_error(
        self, mock_client, tmp_config, caplog
    ):
        broad = [make_product(asin="BROAD", title="Broad")]
        partial = make_product(asin="PARTIAL", title="query")
        updates = []

        def fake_search_pages(**kwargs):
            if kwargs.get("title"):
                yield [partial], 1, 1
                raise RuntimeError("title stream failed")
            yield broad, 1, 1

        mock_client.search_pages.side_effect = fake_search_pages
        with caplog.at_level("INFO", logger="audible_deals"):
            plan = build_search_scan_plan("query", pages=1)
            products = execute_catalog_scan(mock_client, plan, updates.append)

        assert products == broad
        assert (updates[-1].total, updates[-1].completed, updates[-1].items) == (
            2,
            2,
            2,
        )
        assert "Exact-title probe failed" in caplog.text

    def test_or_queries_each_receive_one_title_probe(self, mock_client, tmp_config):
        calls = []

        def fake_search_pages(**kwargs):
            calls.append(kwargs)
            query = kwargs.get("title") or kwargs.get("keywords")
            prefix = "T" if kwargs.get("title") else "B"
            yield [make_product(asin=f"{prefix}{query}", title=query)], 1, 1

        mock_client.search_pages.side_effect = fake_search_pages
        plan = build_search_scan_plan("one | two", category_ids=["cat"], pages=1)
        products = execute_catalog_scan(mock_client, plan)
        assert [call.get("title") for call in calls] == [None, "one", None, "two"]
        assert {product.asin for product in products} == {
            "Tone",
            "Bone",
            "Ttwo",
            "Btwo",
        }

    def test_relevance_rank_is_stable_within_tiers(self):
        products = [
            make_product(asin="A", title="Dune", authors=["Other"]),
            make_product(asin="B", title="Other", authors=["dune"]),
            make_product(asin="C", title="Dune Returns"),
        ]
        assert [p.asin for p in rank_catalog_relevance(products, " DUNE ")] == [
            "A",
            "B",
            "C",
        ]

    def test_progress_includes_broad_and_exact_calls_and_unique_items(
        self, mock_client, tmp_config, monkeypatch
    ):
        updates = []
        broad = [make_product(asin="A", title="Query")]
        exact = [
            make_product(asin="A", title="Query"),
            make_product(asin="B", title="Query Two"),
        ]

        def fake_search_pages(**kwargs):
            products = exact if kwargs.get("title") else broad
            yield products, 1, len(products)

        mock_client.search_pages.side_effect = fake_search_pages

        plan = build_search_scan_plan("Query", category_ids=["cat"], pages=2)
        products = execute_catalog_scan(
            mock_client,
            plan,
            updates.append,
        )

        assert {product.asin for product in products} == {"A", "B"}
        assert (updates[-1].total, updates[-1].completed, updates[-1].items) == (
            2,
            2,
            2,
        )

    def test_multi_query_progress_reports_global_deduplicated_count(
        self, mock_client, tmp_config, monkeypatch
    ):
        updates = []

        def fake_search_pages(**kwargs):
            query = kwargs.get("title") or kwargs.get("keywords")
            asin = "SHARED" if not kwargs.get("title") else f"EXACT{query}"
            yield [make_product(asin=asin, title=query)], 1, 1

        mock_client.search_pages.side_effect = fake_search_pages

        plan = build_search_scan_plan("one | two", category_ids=["cat"], pages=1)
        products = execute_catalog_scan(mock_client, plan, updates.append)

        assert {product.asin for product in products} == {
            "SHARED",
            "EXACTone",
            "EXACTtwo",
        }
        assert (updates[-1].total, updates[-1].completed, updates[-1].items) == (
            4,
            4,
            3,
        )

    def test_failed_exact_probe_still_completes_progress_task(
        self, mock_client, tmp_config, monkeypatch
    ):
        updates = []

        def fake_search_pages(**kwargs):
            if kwargs.get("title"):
                raise RuntimeError("probe failed")
            yield [make_product(asin="BROAD")], 1, 1

        mock_client.search_pages.side_effect = fake_search_pages

        plan = build_search_scan_plan("query", category_ids=["cat"], pages=1)
        products = execute_catalog_scan(mock_client, plan, updates.append)

        assert [product.asin for product in products] == ["BROAD"]
        assert (updates[-1].total, updates[-1].completed, updates[-1].items) == (
            2,
            2,
            1,
        )
