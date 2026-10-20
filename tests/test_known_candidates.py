"""The product index and the selection of titles worth re-pricing."""

from __future__ import annotations

import datetime
import sqlite3

import pytest

from audible_deals import db, paths
from audible_deals.known_candidates import (
    MAX_KNOWN_CANDIDATES,
    calls_for,
    indexed_count,
    select_known,
)
from audible_deals.price_history import purge_stale_history, record_prices
from audible_deals.result_models import FilterContext
from audible_deals.result_processing import (
    SettingsFilterRequest,
    known_candidate_context,
)
from audible_deals.settings import Settings
from tests.conftest import make_product

TODAY = datetime.date(2026, 6, 1)
ALL = ("",)


def day(offset: int) -> datetime.date:
    return TODAY - datetime.timedelta(days=offset)


def seen(*products, on: datetime.date = TODAY) -> None:
    record_prices(list(products), observation_date=on)


def book(n: int, **fields):
    fields.setdefault("asin", f"B0KNOWN{n:03d}")
    fields.setdefault("title", f"Known {n}")
    fields.setdefault("price", 3.0 + n / 100)
    fields.setdefault("series_name", "")
    fields.setdefault("series_position", "")
    fields.setdefault("category_ids", ["TOP", "SUB"])
    return make_product(**fields)


def pick(*, categories=ALL, exclude=(), context=None, **kwargs):
    return select_known(
        "us",
        category_ids=categories,
        exclude=set(exclude),
        context=context or FilterContext(drop_zero_length=True),
        today=TODAY,
        **kwargs,
    )


class TestProductIndex:
    def test_a_recorded_product_is_kept_with_its_categories(self, tmp_config):
        seen(book(1))

        conn = db.state()
        data = conn.execute("SELECT data FROM product_index").fetchone()[0]
        categories = {
            row[0] for row in conn.execute("SELECT category_id FROM product_categories")
        }
        assert '"asin": "B0KNOWN001"' in data
        assert categories == {"TOP", "SUB"}

    def test_a_later_sighting_replaces_the_data_and_categories(self, tmp_config):
        seen(book(1, price=5.0, category_ids=["OLD"]), on=day(5))
        seen(book(1, price=2.0, category_ids=["NEW"]), on=day(1))

        conn = db.state()
        price, seen_on = conn.execute(
            "SELECT last_price, last_seen FROM products"
        ).fetchone()
        assert (price, seen_on) == (2.0, day(1).isoformat())
        assert [
            r[0] for r in conn.execute("SELECT category_id FROM product_categories")
        ] == ["NEW"]

    def test_an_older_observation_never_overwrites_a_newer_one(self, tmp_config):
        seen(book(1, price=2.0, category_ids=["NEW"]), on=day(1))
        seen(book(1, price=9.0, category_ids=["OLD"], title="Stale"), on=day(7))

        conn = db.state()
        assert conn.execute("SELECT last_price, title FROM products").fetchone() == (
            2.0,
            "Known 1",
        )
        assert [
            r[0] for r in conn.execute("SELECT category_id FROM product_categories")
        ] == ["NEW"]
        assert (
            '"price": 2.0'
            in conn.execute("SELECT data FROM product_index").fetchone()[0]
        )

    def test_the_first_sighting_of_a_day_wins(self, tmp_config):
        seen(book(1, price=2.0), on=day(1))
        seen(book(1, price=8.0), on=day(1))

        assert (
            db.state().execute("SELECT last_price FROM products").fetchone()[0] == 2.0
        )

    def test_a_product_without_categories_is_indexed_without_rows(self, tmp_config):
        seen(book(1, category_ids=[]))

        assert (
            db.state().execute("SELECT COUNT(*) FROM product_categories").fetchone()[0]
            == 0
        )
        assert pick(categories=("TOP",)) == []
        assert pick() == ["B0KNOWN001"]

    def test_purging_history_drops_the_index_rows_too(self, tmp_config):
        seen(book(1), on=day(400))

        purge_stale_history(30)

        conn = db.state()
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM product_categories").fetchone()[0] == 0
        )


class TestMigration:
    def test_a_v1_database_gains_the_index_without_losing_rows(self, tmp_config):
        path = paths.state_db_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(path, isolation_level=None)
        for statement in db._STATE_MIGRATIONS[0]:
            raw.execute(statement)
        raw.execute("PRAGMA user_version = 1")
        raw.execute(
            "INSERT INTO products (asin, locale, title, last_price, last_seen) "
            "VALUES ('B0OLD00001', 'us', 'Old', 3.0, '2026-05-30')"
        )
        raw.execute(
            "INSERT INTO price_history (asin, locale, date, price, title) "
            "VALUES ('B0OLD00001', 'us', '2026-05-30', 3.0, 'Old')"
        )
        raw.close()

        conn = db.state()

        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.schema_version(
            "state"
        )
        assert conn.execute("SELECT asin, title FROM products").fetchall() == [
            ("B0OLD00001", "Old")
        ]
        assert conn.execute("SELECT COUNT(*) FROM product_index").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 1
        # Titles from before the index have no data, so they are never candidates.
        assert pick() == []
        # ...until they are seen again.
        seen(book(1, asin="B0OLD00001"))
        assert pick() == ["B0OLD00001"]


class TestSelection:
    def test_a_title_seen_recently_and_not_in_the_scan_is_selected(self, tmp_config):
        seen(book(1), book(2))

        assert pick(exclude={"B0KNOWN002"}) == ["B0KNOWN001"]

    def test_only_this_marketplace(self, tmp_config):
        seen(book(1, locale="uk"), book(2))

        assert pick() == ["B0KNOWN002"]

    @pytest.mark.parametrize(
        ("age", "selected"),
        [(0, True), (89, True), (90, True), (91, False), (400, False)],
    )
    def test_seen_within_ninety_days(self, tmp_config, age, selected):
        seen(book(1), on=day(age))

        assert (pick() == ["B0KNOWN001"]) is selected

    @pytest.mark.parametrize(
        ("scanned", "selected"),
        [
            (("TOP",), True),
            (("SUB",), True),
            (("OTHER",), False),
            (("OTHER", "SUB"), True),
            (("",), True),  # an unrestricted scan covers every category
            (("OTHER", ""), True),
        ],
    )
    def test_the_scanned_categories_must_intersect_the_titles(
        self, tmp_config, scanned, selected
    ):
        seen(book(1))

        assert (pick(categories=scanned) == ["B0KNOWN001"]) is selected

    def test_a_title_without_data_or_price_is_never_selected(self, tmp_config):
        seen(book(1), book(2))
        with db.transaction(db.state()) as conn:
            conn.execute("DELETE FROM product_index WHERE asin = 'B0KNOWN001'")
            conn.execute(
                "UPDATE products SET last_price = NULL WHERE asin = 'B0KNOWN002'"
            )

        assert pick() == []

    def test_an_unreadable_row_is_skipped(self, tmp_config):
        seen(book(1), book(2))
        with db.transaction(db.state()) as conn:
            conn.execute(
                "UPDATE product_index SET data = '{broken' WHERE asin = 'B0KNOWN001'"
            )

        assert pick() == ["B0KNOWN002"]

    def test_the_ranking_is_last_known_price_then_price_per_hour_then_asin(
        self, tmp_config
    ):
        seen(
            book(1, price=5.0, length_minutes=600),
            book(2, price=2.0, length_minutes=60),
            book(3, price=2.0, length_minutes=600),
            book(4, price=2.0, length_minutes=600),
        )

        assert pick() == ["B0KNOWN003", "B0KNOWN004", "B0KNOWN002", "B0KNOWN001"]

    def test_the_candidate_cap(self, tmp_config):
        seen(*[book(n, price=2.0 + n / 1000) for n in range(1, 131)])

        picked = pick()

        assert len(picked) == MAX_KNOWN_CANDIDATES == 100
        assert picked[0] == "B0KNOWN001" and picked[-1] == "B0KNOWN100"
        assert len(pick(limit=50)) == 50

    def test_the_cap_applies_after_the_filters(self, tmp_config):
        # Cheap titles that fail a filter must not use up the slots.
        seen(*[book(n, price=1.0, language="german") for n in range(1, 121)])
        seen(*[book(n, price=4.0) for n in range(121, 131)])

        picked = pick(context=FilterContext(language="english", drop_zero_length=True))

        assert len(picked) == 10 and picked[0] == "B0KNOWN121"


class TestFiltersOnLastKnownData:
    def _settings(self, **fields):
        return Settings(**fields)

    def _context(self, settings, **kwargs):
        return known_candidate_context(
            SettingsFilterRequest(products=(), settings=settings, **kwargs)
        )

    @pytest.mark.parametrize(
        ("settings", "failing"),
        [
            (Settings(language="english"), {"language": "german"}),
            (Settings(min_rating=4.0), {"rating": 3.0}),
            (Settings(min_ratings=500), {"num_ratings": 10}),
            (Settings(min_hours=5.0), {"length_minutes": 60}),
            (Settings(exclude_authors=("Bad Author",)), {"authors": ["Bad Author"]}),
            (Settings(exclude_narrators=("Bad Voice",)), {"narrators": ["Bad Voice"]}),
            (Settings(narrator="Right Voice"), {"narrators": ["Other"]}),
            (Settings(skip_plus=True), {"in_plus_catalog": True}),
            (Settings(only_plus=True), {"in_plus_catalog": False}),
            (Settings(released_after="2024-01-01"), {"release_date": "2020-01-01"}),
            (Settings(publisher="Podium"), {"publisher": "Tantor"}),
            (Settings(series="Saga"), {"series_name": "Other"}),
        ],
    )
    def test_a_title_failing_a_filter_is_not_selected(
        self, tmp_config, settings, failing
    ):
        good = {
            "language": "english",
            "rating": 4.5,
            "num_ratings": 1000,
            "length_minutes": 600,
            "authors": ["Fine"],
            "narrators": ["Right Voice"],
            "in_plus_catalog": settings.only_plus,
            "release_date": "2025-01-01",
            "publisher": "Podium",
            "series_name": "Saga",
        }
        seen(book(1, **good), book(2, **{**good, **failing}))

        assert pick(context=self._context(settings)) == ["B0KNOWN001"]

    def test_skipped_and_excluded_titles_are_not_selected(self, tmp_config):
        seen(book(1), book(2), book(3, category_ids=["BANNED"]))

        context = self._context(
            Settings(),
            skip_asins={"B0KNOWN001"},
            exclude_category_ids={"BANNED"},
        )

        assert pick(context=context) == ["B0KNOWN002"]

    def test_price_and_discount_filters_wait_for_the_live_price(self, tmp_config):
        # Last seen at $40 against a $15 list price: every price filter would
        # reject it, but today's price is what decides, so it is still a candidate.
        seen(book(1, price=40.0, list_price=15.0, length_minutes=60))

        context = self._context(
            Settings(
                max_price=5.0,
                max_effective_price=5.0,
                on_sale=True,
                min_discount=50,
                max_pph=0.5,
                hist_below=10,
                min_price_drop=10.0,
                require_history=True,
            ),
            credit_price=10.0,
        )

        assert pick(context=context) == ["B0KNOWN001"]


class TestBudget:
    @pytest.mark.parametrize(
        ("count", "calls"), [(0, 0), (1, 1), (50, 1), (51, 2), (100, 2)]
    )
    def test_batches_of_fifty(self, count, calls):
        assert calls_for(count) == calls

    def test_indexed_count_is_a_ceiling_for_the_plan(self, tmp_config):
        assert indexed_count("us", today=TODAY) == 0
        seen(*[book(n) for n in range(1, 61)])
        seen(book(200, locale="uk"), book(201, category_ids=["ELSEWHERE"]))

        assert indexed_count("us", today=TODAY) == 61
        assert indexed_count("us", ("ELSEWHERE",), today=TODAY) == 1
        assert indexed_count("uk", today=TODAY) == 1

    def test_indexed_count_stops_at_the_cap(self, tmp_config):
        seen(*[book(n) for n in range(1, 131)])

        assert indexed_count("us", today=TODAY) == MAX_KNOWN_CANDIDATES
