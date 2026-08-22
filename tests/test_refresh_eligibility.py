from __future__ import annotations

import datetime
import json

import pytest

from audible_deals import constants
from audible_deals.refresh_eligibility import (
    load_refresh_eligibility,
    mark_refresh_eligible,
)
from tests.conftest import make_product


def _write_history(asin: str, marketplaces: dict) -> None:
    constants.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    (constants.HISTORY_DIR / f"{asin}.json").write_text(
        json.dumps({"marketplaces": marketplaces})
    )


def test_first_use_migrates_seen_numeric_histories_across_marketplaces(tmp_config):
    constants.SEEN_ASINS_FILE.write_text(json.dumps(["B00MIGR001", "B00MIGR002"]))
    _write_history(
        "B00MIGR001",
        {
            "us": [
                {"date": "2026-01-01", "price": 5},
                {"date": "2026-01-03", "price": 4},
                {"date": "bad", "price": 3},
            ],
            "uk": [{"date": "2026-01-02", "price": 6}],
        },
    )
    _write_history(
        "B00MIGR002",
        {"us": [{"date": "2026-01-04", "price": None}]},
    )
    _write_history(
        "B00UNSEEN1",
        {"us": [{"date": "2026-01-05", "price": 2}]},
    )

    assert load_refresh_eligibility() == {
        "us": {"B00MIGR001": "2026-01-03"},
        "uk": {"B00MIGR001": "2026-01-02"},
    }
    persisted = json.loads(constants.REFRESH_ELIGIBILITY_FILE.read_text())
    assert persisted["version"] == 1


@pytest.mark.parametrize(
    "content",
    ["not-json", json.dumps({"version": 99, "marketplaces": {}})],
)
def test_corrupt_or_unsupported_store_reads_empty_without_rewrite(tmp_config, content):
    constants.REFRESH_ELIGIBILITY_FILE.write_text(content)
    before = constants.REFRESH_ELIGIBILITY_FILE.read_bytes()

    assert load_refresh_eligibility() == {}
    mark_refresh_eligible([make_product(asin="B00CORRUP1", price=4)])

    assert constants.REFRESH_ELIGIBILITY_FILE.read_bytes() == before


def test_mark_keeps_later_date_and_only_numeric_products(tmp_config):
    asin = "B00SURF001"
    mark_refresh_eligible(
        [
            make_product(asin=asin, locale="us", price=4),
            make_product(asin=asin, locale="uk", price=5),
            make_product(asin="B00NOPRICE", price=None),
            make_product(asin="B00NOTFIN1", price=float("nan")),
        ],
        datetime.date(2026, 1, 10),
    )
    mark_refresh_eligible(
        [make_product(asin=asin, locale="us", price=3)],
        datetime.date(2026, 1, 9),
    )

    assert load_refresh_eligibility() == {
        "us": {asin: "2026-01-10"},
        "uk": {asin: "2026-01-10"},
    }
