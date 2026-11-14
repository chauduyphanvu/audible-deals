"""Grading a price against the book's own recorded history."""

from __future__ import annotations

import datetime
import random
import statistics

import pytest

from audible_deals.deal_grade import (
    MAX_HOLD_DAYS,
    MIN_CHECKS,
    MIN_SPAN_DAYS,
    grade_latest,
    grade_price,
    grade_products,
)
from audible_deals.price_history import history_key
from tests.conftest import (
    HISTORY_START as START,
    STANDARD_WITH_DIPS,
    daily_history as _daily,
    make_product,
)


def _day(offset: int) -> datetime.date:
    return START + datetime.timedelta(days=offset)


def _entry(offset: int, price) -> dict:
    return {"date": _day(offset).isoformat(), "price": price, "title": "Book"}


DIPS = _daily(*STANDARD_WITH_DIPS)
DIPS_NEXT_DAY = _day(len(DIPS))
FLAT = _daily((200, 15.00))
FLAT_NEXT_DAY = _day(len(FLAT))


class TestNotEnoughHistory:
    @pytest.mark.parametrize(
        "entries",
        [
            [],
            None,
            "not a list",
            {"date": "2026-05-12", "price": 9.99},
            [None, 3, "x", {"price": 5.0}, {"date": "bad", "price": 5.0}],
            [{"date": 20260512, "price": 5.0}, {"date": "2026-05-13"}],
        ],
        ids=["empty", "none", "string", "mapping", "junk-items", "bad-fields"],
    )
    def test_empty_or_malformed_history_is_new(self, entries):
        grade = grade_price(entries, 9.99, DIPS_NEXT_DAY)

        assert grade.grade == "new"
        assert grade.checks == 0
        assert grade.first_date is None
        assert grade.price == 9.99

    @pytest.mark.parametrize(
        ("offsets", "observed", "expected"),
        [
            ([0, 20, 40, 60], 80, "new"),  # long span, too few checks
            (list(range(10)), 10, "new"),  # many checks, span under two weeks
            ([0, 1, 2, 3, 4], MIN_SPAN_DAYS, "typical"),  # both minimums met
            ([0, 1, 2, 3, 4], MIN_SPAN_DAYS - 1, "new"),  # one day short
        ],
    )
    def test_evidence_needs_enough_checks_over_enough_days(
        self, offsets, observed, expected
    ):
        entries = [_entry(offset, 15.00) for offset in offsets]

        grade = grade_price(entries, 15.00, _day(observed))

        assert grade.grade == expected
        assert grade.checks == len(offsets)
        assert grade.first_date == START

    def test_thresholds_match_the_documented_rule(self):
        assert (MIN_CHECKS, MIN_SPAN_DAYS) == (5, 14)

    def test_new_carries_no_price_statistics(self):
        grade = grade_price(DIPS[:4], 1.00, _day(4))

        assert grade.grade == "new"
        assert grade.checks == 4
        assert grade.lowest_price is None
        assert grade.typical_price is None
        assert grade.good_price is None


class TestFlatHistory:
    @pytest.mark.parametrize(
        ("price", "expected"),
        [
            (15.00, "typical"),  # the only price ever seen
            (14.50, "typical"),  # a new low, but only 3% under usual
            (16.00, "typical"),  # 7% above usual
            (13.00, "lowest"),  # a genuine new low
            (17.00, "high"),  # more than 10% above usual
        ],
    )
    def test_flat_history(self, price, expected):
        assert grade_price(FLAT, price, FLAT_NEXT_DAY).grade == expected

    @pytest.mark.parametrize("days", [MIN_SPAN_DAYS + 1, 365])
    def test_usual_price_is_typical_however_long_the_history(self, days):
        entries = _daily((days, 15.00))

        grade = grade_price(entries, 15.00, _day(days))

        assert grade.grade == "typical"
        assert grade.typical_price == 15.00
        assert grade.good_price is None


class TestStandardPriceWithDips:
    @pytest.mark.parametrize(
        ("price", "expected", "rank", "times"),
        [
            (6.99, "lowest", 1, 0),  # new all-time low
            (7.49, "lowest", 1, 1),  # ties the previous low
            (9.99, "good", 2, 2),  # at a dip
            (12.99, "good", 3, 3),  # at the shallowest dip
            (16.99, "typical", 4, 3),  # under usual, nearer usual than the low
            (24.50, "typical", 4, 1),  # the standard price
            (25.99, "typical", 5, 1),  # above standard, under the 10% margin
            (27.99, "high", 5, 1),  # meaningfully above standard
        ],
    )
    def test_grades_across_the_range(self, price, expected, rank, times):
        grade = grade_price(DIPS, price, DIPS_NEXT_DAY)

        assert grade.grade == expected
        assert grade.rank == rank
        assert grade.times_at_or_below == times

    def test_carries_display_evidence(self):
        grade = grade_price(DIPS, 27.99, DIPS_NEXT_DAY)

        assert grade.checks == len(DIPS)
        assert grade.first_date == START
        assert grade.lowest_price == 7.49
        assert grade.lowest_date == _day(81)
        assert grade.typical_price == 24.50
        assert grade.good_price == 12.99
        assert grade.good_times == 3
        assert grade.observed_on == DIPS_NEXT_DAY

    def test_lowest_date_is_the_most_recent_visit_to_the_low(self):
        entries = [*DIPS, _entry(len(DIPS), 7.49), _entry(len(DIPS) + 1, 24.50)]

        grade = grade_price(entries, 24.50, _day(len(DIPS) + 2))

        assert grade.lowest_date == _day(len(DIPS))


class TestUnevenSpacing:
    def test_dense_checks_during_one_sale_do_not_move_the_usual_price(self):
        weekly = [_entry(offset, 24.50) for offset in range(0, 56, 7)]
        sale_week = [_entry(offset, 9.99) for offset in range(50, 60)]
        entries = [*weekly, *sale_week, _entry(60, 24.50)]
        observed = _day(67)
        # Counted per check, the sale would look like the usual price.
        assert statistics.median(entry["price"] for entry in entries) == 9.99

        at_standard = grade_price(entries, 24.50, observed)
        at_sale = grade_price(entries, 9.99, observed)

        assert at_standard.typical_price == 24.50
        assert at_standard.grade == "typical"
        assert at_sale.grade == "lowest"
        assert at_sale.times_at_or_below == 1

    def test_one_check_before_a_long_gap_counts_for_at_most_the_hold_cap(self):
        entries = [_entry(offset, 24.50) for offset in (0, 7, 14, 21)]
        entries.append(_entry(28, 9.99))
        observed = _day(28 + 10 * MAX_HOLD_DAYS)

        grade = grade_price(entries, 24.50, observed)

        assert grade.typical_price == 24.50
        assert grade.grade == "typical"


class TestTodayAlreadyRecorded:
    def test_entry_on_the_observation_date_is_ignored(self):
        recorded = [*DIPS, _entry(len(DIPS), 9.99)]

        before = grade_price(DIPS, 9.99, DIPS_NEXT_DAY)
        after = grade_price(recorded, 9.99, DIPS_NEXT_DAY)

        assert after == before
        assert after.checks == len(DIPS)

    def test_a_different_price_recorded_earlier_today_is_ignored(self):
        recorded = [*DIPS, _entry(len(DIPS), 1.00)]

        assert grade_price(recorded, 9.99, DIPS_NEXT_DAY) == grade_price(
            DIPS, 9.99, DIPS_NEXT_DAY
        )

    def test_entries_after_the_observation_date_are_ignored(self):
        later = [*DIPS, _entry(len(DIPS) + 3, 0.99)]

        assert grade_price(later, 9.99, DIPS_NEXT_DAY) == grade_price(
            DIPS, 9.99, DIPS_NEXT_DAY
        )

    def test_grade_latest_grades_the_newest_entry_against_earlier_ones(self):
        recorded = [*DIPS, _entry(len(DIPS), 9.99)]

        assert grade_latest(recorded) == grade_price(DIPS, 9.99, DIPS_NEXT_DAY)

    @pytest.mark.parametrize("entries", [[], [{"price": 5.0}], [_entry(0, None)]])
    def test_grade_latest_without_dated_prices_is_none(self, entries):
        assert grade_latest(entries) is None

    def test_datetime_observation_is_treated_as_its_date(self):
        moment = datetime.datetime.combine(DIPS_NEXT_DAY, datetime.time(23, 59))

        assert grade_price(DIPS, 9.99, moment) == grade_price(DIPS, 9.99, DIPS_NEXT_DAY)


class TestNonFinitePrices:
    @pytest.mark.parametrize(
        "bad",
        [float("nan"), float("inf"), float("-inf"), None, "n/a", True],
        ids=["nan", "inf", "-inf", "none", "text", "bool"],
    )
    def test_bad_entry_prices_are_skipped(self, bad):
        noisy = [*DIPS[:40], {"date": "2026-01-01", "price": bad}, *DIPS[40:]]
        noisy.append({"date": _day(5).isoformat(), "title": "no price"})

        assert grade_price(noisy, 9.99, DIPS_NEXT_DAY) == grade_price(
            DIPS, 9.99, DIPS_NEXT_DAY
        )

    @pytest.mark.parametrize(
        "bad",
        [float("nan"), float("inf"), None, "9.99", True],
        ids=["nan", "inf", "none", "text", "bool"],
    )
    def test_bad_current_price_is_new(self, bad):
        grade = grade_price(DIPS, bad, DIPS_NEXT_DAY)

        assert grade.grade == "new"
        assert grade.price is None
        assert grade.checks == len(DIPS)


class TestOrderingAndCap:
    def test_entry_order_does_not_matter(self):
        shuffled = list(DIPS)
        random.Random(7).shuffle(shuffled)

        assert grade_price(shuffled, 12.99, DIPS_NEXT_DAY) == grade_price(
            DIPS, 12.99, DIPS_NEXT_DAY
        )

    def test_duplicate_date_keeps_the_last_entry(self):
        duplicated = [*DIPS, _entry(0, 1.00), _entry(0, 24.50)]

        grade = grade_price(duplicated, 9.99, DIPS_NEXT_DAY)

        assert grade == grade_price(DIPS, 9.99, DIPS_NEXT_DAY)

    def test_grading_is_deterministic(self):
        assert grade_price(DIPS, 9.99, DIPS_NEXT_DAY) == grade_price(
            DIPS, 9.99, DIPS_NEXT_DAY
        )

    def test_low_that_fell_off_the_365_entry_cap_no_longer_counts(self):
        full = _daily((30, 4.99), (370, 24.50))
        capped = full[-365:]
        observed = _day(len(full))

        uncapped_grade = grade_price(full, 9.99, observed)
        capped_grade = grade_price(capped, 9.99, observed)

        assert uncapped_grade.grade == "good"
        assert uncapped_grade.lowest_price == 4.99
        assert capped_grade.grade == "lowest"
        assert capped_grade.checks == 365
        assert capped_grade.first_date == _day(len(full) - 365)


class TestGradeProducts:
    def test_grades_priced_products_from_their_marketplace_history(self):
        us = make_product(asin="B00GRADE01", price=9.99, locale="us")
        uk = make_product(asin="B00GRADE02", price=9.99, locale="uk")
        unpriced = make_product(asin="B00GRADE03", price=None)
        histories = {
            history_key(us.asin, "us"): DIPS,
            history_key(uk.asin, "us"): DIPS,
        }

        grades = grade_products([us, uk, unpriced], histories, DIPS_NEXT_DAY)

        assert set(grades) == {us.asin, uk.asin}
        assert grades[us.asin].grade == "good"
        assert grades[uk.asin].grade == "new"
        assert grades[uk.asin].checks == 0

    def test_accepts_frozen_history_entries(self):
        from audible_deals.result_models import DiscoveryResult

        product = make_product(asin="B00GRADE04", price=9.99)
        frozen = DiscoveryResult(
            (product,), histories={history_key(product.asin, "us"): DIPS}
        ).histories

        grades = grade_products([product], frozen, DIPS_NEXT_DAY)

        assert grades[product.asin] == grade_price(DIPS, 9.99, DIPS_NEXT_DAY)
