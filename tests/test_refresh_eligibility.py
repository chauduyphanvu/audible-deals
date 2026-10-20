from __future__ import annotations

import datetime
import json

import pytest

from audible_deals import paths
from audible_deals.refresh_eligibility import (
    load_refresh_eligibility,
    mark_refresh_eligible,
)
from tests.conftest import make_product


def _write_history(asin: str, marketplaces: dict) -> None:
    """A legacy per-ASIN history file (read by the one-time import)."""
    history_dir = paths.legacy_history_dir()
    history_dir.mkdir(parents=True, exist_ok=True)
    (history_dir / f"{asin}.json").write_text(
        json.dumps({"marketplaces": marketplaces})
    )


def test_import_derives_eligibility_from_seen_numeric_histories(tmp_config):
    (paths.data_dir() / "seen_asins.json").write_text(
        json.dumps(["B00MIGR001", "B00MIGR002"])
    )
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
    # The result is stored in state.db; the sources are kept as *.migrated.
    assert (paths.data_dir() / "seen_asins.json.migrated").exists()
    assert (paths.data_dir() / "history.migrated" / "B00MIGR001.json").exists()


@pytest.mark.parametrize(
    "content",
    ["not-json", json.dumps({"version": 99, "marketplaces": {}})],
)
def test_corrupt_or_unsupported_legacy_store_imports_nothing(tmp_config, content):
    legacy = paths.data_dir() / "refresh_eligibility.json"
    legacy.write_text(content)
    (paths.data_dir() / "seen_asins.json").write_text(json.dumps(["B00CORRUP1"]))
    _write_history("B00CORRUP1", {"us": [{"date": "2026-01-01", "price": 5}]})

    # A store that could not be read is not rebuilt from history either.
    assert load_refresh_eligibility() == {}
    mark_refresh_eligible([make_product(asin="B00CORRUP1", price=4)])

    assert load_refresh_eligibility() == {
        "us": {"B00CORRUP1": datetime.date.today().isoformat()}
    }
    assert (
        paths.data_dir() / "refresh_eligibility.json.migrated"
    ).read_text() == content


def test_valid_entries_survive_an_invalid_neighbour(tmp_config):
    (paths.data_dir() / "refresh_eligibility.json").write_text(
        json.dumps(
            {
                "version": 1,
                "marketplaces": {
                    "us": {"B00GOOD001": "2026-01-02", "B00BADDATE": "yesterday"},
                    "xx": {"B00BADLOC1": "2026-01-02"},
                },
            }
        )
    )

    assert load_refresh_eligibility() == {"us": {"B00GOOD001": "2026-01-02"}}


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
