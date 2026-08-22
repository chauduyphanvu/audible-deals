"""Catalog filtering, sorting, and metric behavior."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

import audible_deals.config_store as config_store_mod
import audible_deals.constants as constants_mod
from audible_deals.cli import cli
from audible_deals.filtering import (
    dedupe_editions as _typed_dedupe_editions,
)
from audible_deals.filtering import (
    filter_products as _typed_filter_products,
)
from audible_deals.filtering import (
    first_in_series as _typed_first_in_series,
)
from audible_deals.filtering import (
    sort_local as _sort_local,
)
from audible_deals.metrics import (
    price_per_hour as _price_per_hour,
)
from audible_deals.metrics import (
    value_score as _value_score,
)
from audible_deals.result_models import (
    FilterContext,
    FilterOutcome,
)
from audible_deals.serialization import (
    serialize_product as _serialize_product,
)
from tests.conftest import make_product


def _filter_products(products, **values):
    outcome = _typed_filter_products(products, FilterContext(**values))
    return list(outcome.products), dict(outcome.breakdown)


def _dedupe_editions(products):
    outcome = _typed_dedupe_editions(FilterOutcome(products))
    return list(outcome.products), outcome.editions_removed


def _first_in_series(products):
    outcome = _typed_first_in_series(FilterOutcome(products))
    return list(outcome.products), outcome.series_collapsed


class TestFilterProducts:
    def test_max_price(self, products_for_filtering):
        filtered, breakdown = _filter_products(products_for_filtering, max_price=5.0)
        assert all(p.price is not None and p.price <= 5.0 for p in filtered)
        assert breakdown.get("max price", 0) > 0

    def test_min_rating(self, products_for_filtering):
        filtered, _ = _filter_products(products_for_filtering, min_rating=4.0)
        assert all(p.rating >= 4.0 for p in filtered)

    def test_min_hours(self, products_for_filtering):
        filtered, _ = _filter_products(products_for_filtering, min_hours=5.0)
        assert all(p.hours >= 5.0 for p in filtered)

    def test_language(self, products_for_filtering):
        filtered, _ = _filter_products(products_for_filtering, language="french")
        assert all(p.language.lower() == "french" for p in filtered)
        assert len(filtered) == 1

    def test_on_sale(self, products_for_filtering):
        filtered, _ = _filter_products(products_for_filtering, on_sale=True)
        # Only items with a confirmed positive discount should pass
        assert all(p.discount_pct is not None and p.discount_pct > 0 for p in filtered)
        # NO_PRICE (None discount) and EXPENSIVE (0% discount) must be excluded
        assert not any(p.asin in ("NO_PRICE", "EXPENSIVE") for p in filtered)

    def test_skip_asins(self, products_for_filtering):
        filtered, _ = _filter_products(
            products_for_filtering, skip_asins={"CHEAP1", "CHEAP2"}
        )
        assert not any(p.asin in {"CHEAP1", "CHEAP2"} for p in filtered)

    def test_exclude_category_ids(self, products_for_filtering):
        filtered, _ = _filter_products(
            products_for_filtering, exclude_category_ids={"cat_erotica"}
        )
        assert not any(p.asin == "EROTICA" for p in filtered)

    def test_no_filters(self, products_for_filtering):
        filtered, breakdown = _filter_products(products_for_filtering)
        assert len(filtered) == len(products_for_filtering)
        assert breakdown == {}

    def test_combined_filters(self, products_for_filtering):
        filtered, _ = _filter_products(
            products_for_filtering,
            max_price=5.0,
            min_rating=4.0,
            language="english",
        )
        for p in filtered:
            assert p.price is not None and p.price <= 5.0
            assert p.rating >= 4.0
            assert p.language.lower() == "english"


class TestPricePerHour:
    def test_normal(self):
        p = make_product(price=10.0, length_minutes=600)  # 10hrs
        assert _price_per_hour(p) == pytest.approx(1.0)

    def test_no_price(self):
        p = make_product(price=None)
        assert _price_per_hour(p) == float("inf")

    def test_zero_hours(self):
        p = make_product(price=5.0, length_minutes=0)
        assert _price_per_hour(p) == float("inf")


class TestSortLocal:
    @pytest.fixture
    def products(self):
        return [
            make_product(
                asin="A",
                price=5.0,
                rating=3.0,
                length_minutes=300,
                release_date="2024-01-01",
                list_price=10.0,
            ),
            make_product(
                asin="B",
                price=2.0,
                rating=5.0,
                length_minutes=600,
                release_date="2024-06-01",
                list_price=20.0,
            ),
            make_product(
                asin="C",
                price=8.0,
                rating=4.0,
                length_minutes=120,
                release_date="2023-01-01",
                list_price=10.0,
            ),
        ]

    def test_sort_price(self, products):
        result = _sort_local(products, "price")
        prices = [p.price for p in result]
        assert prices == sorted(prices)

    def test_sort_price_reverse(self, products):
        result = _sort_local(products, "-price")
        prices = [p.price for p in result]
        assert prices == sorted(prices, reverse=True)

    def test_sort_rating(self, products):
        result = _sort_local(products, "rating")
        ratings = [p.rating for p in result]
        assert ratings == sorted(ratings, reverse=True)

    def test_sort_length(self, products):
        result = _sort_local(products, "length")
        lengths = [p.length_minutes for p in result]
        assert lengths == sorted(lengths, reverse=True)

    def test_sort_date(self, products):
        result = _sort_local(products, "date")
        dates = [p.release_date for p in result]
        assert dates == sorted(dates, reverse=True)

    def test_sort_discount(self, products):
        result = _sort_local(products, "discount")
        discounts = [p.discount_pct or 0 for p in result]
        assert discounts == sorted(discounts, reverse=True)

    def test_sort_price_per_hour(self, products):
        result = _sort_local(products, "price-per-hour")
        pphs = [_price_per_hour(p) for p in result]
        assert pphs == sorted(pphs)

    def test_sort_unknown_passthrough(self, products):
        result = _sort_local(products, "relevance")
        assert [p.asin for p in result] == ["A", "B", "C"]

    def test_sort_price_with_none(self):
        products = [
            make_product(asin="X", price=None),
            make_product(asin="Y", price=3.0),
        ]
        result = _sort_local(products, "price")
        assert result[0].asin == "Y"
        assert result[1].asin == "X"


class TestDedupeEditions:
    def test_keeps_cheapest(self):
        products = [
            make_product(asin="A", series_name="S", series_position="1", price=10.0),
            make_product(asin="B", series_name="S", series_position="1", price=5.0),
        ]
        result, removed = _dedupe_editions(products)
        assert removed == 1
        assert len(result) == 1
        assert result[0].asin == "B"

    def test_no_series_pass_through(self):
        products = [
            make_product(asin="A", series_name="", series_position=""),
            make_product(asin="B", series_name="", series_position=""),
        ]
        result, removed = _dedupe_editions(products)
        assert removed == 0
        assert len(result) == 2

    def test_different_positions_kept(self):
        products = [
            make_product(asin="A", series_name="S", series_position="1", price=5.0),
            make_product(asin="B", series_name="S", series_position="2", price=5.0),
        ]
        result, removed = _dedupe_editions(products)
        assert removed == 0
        assert len(result) == 2

    def test_case_insensitive(self):
        products = [
            make_product(asin="A", series_name="Epic", series_position="1", price=10.0),
            make_product(asin="B", series_name="epic", series_position="1", price=5.0),
        ]
        result, removed = _dedupe_editions(products)
        assert removed == 1


class TestFirstInSeries:
    def test_keeps_lowest_position(self):
        products = [
            make_product(asin="A", series_name="S", series_position="3"),
            make_product(asin="B", series_name="S", series_position="1"),
            make_product(asin="C", series_name="S", series_position="2"),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 2
        assert len(result) == 1
        assert result[0].asin == "B"

    def test_non_series_pass_through(self):
        products = [
            make_product(asin="A", series_name=""),
            make_product(asin="B", series_name=""),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 0
        assert len(result) == 2

    def test_different_series(self):
        # Both series have their lowest position > 1.0, so both are excluded
        # (Book 1 wasn't in the result set for either series).
        products = [
            make_product(asin="A", series_name="S1", series_position="2"),
            make_product(asin="B", series_name="S2", series_position="3"),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 2
        assert len(result) == 0

    def test_different_series_with_book1(self):
        # Each series has a Book 1, so both are kept.
        products = [
            make_product(asin="A", series_name="S1", series_position="1"),
            make_product(asin="B", series_name="S2", series_position="1"),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 0
        assert len(result) == 2

    def test_non_numeric_position(self):
        # "Book 1" now parses as 1.0 via parse_series_position, same as "1".
        # On a tie, the first-seen product wins (stable behaviour).
        products = [
            make_product(asin="A", series_name="S", series_position="Book 1"),
            make_product(asin="B", series_name="S", series_position="1"),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 1
        assert result[0].asin == "A"


class TestExcludeAuthorFilter:
    def test_exclude_author_single(self):
        """_filter_products excludes products whose authors match the exclude substring."""
        products = [
            make_product(asin="EA1", authors=["Andy Weir"]),
            make_product(asin="EA2", authors=["Brandon Sanderson"]),
        ]
        filtered, breakdown = _filter_products(products, exclude_authors=("Andy Weir",))
        asins = [p.asin for p in filtered]
        assert "EA1" not in asins
        assert "EA2" in asins
        assert breakdown == {"excluded authors": 1}

    def test_exclude_author_multiple(self):
        """Multiple --exclude-author values are all applied."""
        products = [
            make_product(asin="EAM1", authors=["Andy Weir"]),
            make_product(asin="EAM2", authors=["Brandon Sanderson"]),
            make_product(asin="EAM3", authors=["Terry Pratchett"]),
        ]
        filtered, breakdown = _filter_products(
            products, exclude_authors=("andy", "sanderson")
        )
        asins = [p.asin for p in filtered]
        assert "EAM1" not in asins
        assert "EAM2" not in asins
        assert "EAM3" in asins
        assert breakdown == {"excluded authors": 2}

    def test_exclude_author_case_insensitive(self):
        products = [make_product(asin="EAC1", authors=["Andy Weir"])]
        filtered, _ = _filter_products(products, exclude_authors=("ANDY WEIR",))
        assert len(filtered) == 0

    def test_exclude_author_empty_tuple_no_filter(self):
        products = [make_product(asin="EAE1", authors=["Anyone"])]
        filtered, breakdown = _filter_products(products, exclude_authors=())
        assert len(filtered) == 1
        assert breakdown == {}

    def test_find_exclude_author_flag(self, mock_client, tmp_config):
        """deals find --exclude-author filters out matching authors."""
        products = [
            make_product(
                asin="FEA1",
                price=3.0,
                authors=["Andy Weir"],
                series_name="",
                series_position="",
            ),
            make_product(
                asin="FEA2",
                price=4.0,
                authors=["Brandon Sanderson"],
                series_name="",
                series_position="",
            ),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "find_excl_author.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--exclude-author",
                "andy weir",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "FEA1" not in asins
        assert "FEA2" in asins

    def test_last_exclude_author_flag(self, tmp_config):
        """deals last --exclude-author filters from cache."""

        products = [
            make_product(
                asin="LEA1",
                price=3.0,
                authors=["Andy Weir"],
                series_name="",
                series_position="",
            ),
            make_product(
                asin="LEA2",
                price=4.0,
                authors=["Brandon Sanderson"],
                series_name="",
                series_position="",
            ),
        ]
        constants_mod.LAST_RESULTS_FILE.write_text(
            json.dumps([_serialize_product(p) for p in products])
        )
        out_file = tmp_config / "last_excl_author.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "last",
                "--exclude-author",
                "weir",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LEA1" not in asins
        assert "LEA2" in asins

    def test_exclude_author_in_profile(self, tmp_config):
        """profile save --exclude-author persists the exclusion."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "profile",
                "save",
                "no-weir",
                "--exclude-author",
                "Andy Weir",
                "--exclude-author",
                "Brandon Sanderson",
            ],
        )
        assert result.exit_code == 0, result.output

        profiles = config_store_mod.load_profiles()
        assert "no-weir" in profiles
        excluded = profiles["no-weir"]["exclude_authors"]
        assert "Andy Weir" in excluded
        assert "Brandon Sanderson" in excluded

    def test_find_profile_exclude_author_applied(self, mock_client, tmp_config):
        """find --profile with exclude_authors actually filters out the author."""

        config_store_mod.save_profiles({"no-weir": {"exclude_authors": ["Andy Weir"]}})
        products = [
            make_product(
                asin="EA1",
                price=3.0,
                authors=["Andy Weir"],
                series_name="",
                series_position="",
            ),
            make_product(
                asin="EA2",
                price=3.0,
                authors=["Pierce Brown"],
                series_name="",
                series_position="",
            ),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "profile_excl.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--profile",
                "no-weir",
                "--pages",
                "1",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "EA1" not in asins  # Andy Weir excluded
        assert "EA2" in asins  # Pierce Brown kept


class TestAuthorFilter:
    def test_author_substring_match(self):
        """_filter_products filters by author substring (case-insensitive)."""
        products = [
            make_product(asin="A1", authors=["Andy Weir"]),
            make_product(asin="A2", authors=["Brandon Sanderson"]),
            make_product(asin="A3", authors=["andy waters"]),  # Different "andy"
        ]
        filtered, breakdown = _filter_products(products, author="andy")
        asins = [p.asin for p in filtered]
        assert "A1" in asins
        assert "A3" in asins
        assert "A2" not in asins
        assert breakdown == {"author": 1}

    def test_author_case_insensitive(self):
        products = [make_product(asin="CI1", authors=["Andy Weir"])]
        filtered, _ = _filter_products(products, author="ANDY WEIR")
        assert len(filtered) == 1

    def test_author_no_match(self):
        products = [make_product(asin="NM1", authors=["Brandon Sanderson"])]
        filtered, _ = _filter_products(products, author="tolkien")
        assert len(filtered) == 0

    def test_author_empty_string_no_filter(self):
        products = [make_product(asin="EF1", authors=["Anyone"])]
        filtered, breakdown = _filter_products(products, author="")
        assert len(filtered) == 1
        assert breakdown == {}

    def test_find_author_flag(self, mock_client, tmp_config):
        """deals find --author filters by author name."""
        products = [
            make_product(
                asin="FA1",
                price=3.0,
                authors=["Andy Weir"],
                series_name="",
                series_position="",
            ),
            make_product(
                asin="FA2",
                price=4.0,
                authors=["Brandon Sanderson"],
                series_name="",
                series_position="",
            ),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "find_author.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--author",
                "weir",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "FA1" in asins
        assert "FA2" not in asins

    def test_last_author_filter(self, tmp_config):
        """deals last --author filters by author."""

        products = [
            make_product(
                asin="LA1",
                price=3.0,
                authors=["Andy Weir"],
                series_name="",
                series_position="",
            ),
            make_product(
                asin="LA2",
                price=4.0,
                authors=["Brandon Sanderson"],
                series_name="",
                series_position="",
            ),
        ]
        constants_mod.LAST_RESULTS_FILE.write_text(
            json.dumps([_serialize_product(p) for p in products])
        )
        out_file = tmp_config / "last_author.json"
        runner = CliRunner()
        result = runner.invoke(
            cli, ["last", "--author", "weir", "--output", str(out_file)]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LA1" in asins
        assert "LA2" not in asins

    def test_author_in_profile(self, tmp_config):
        """profile save --author persists the author filter."""
        runner = CliRunner()
        result = runner.invoke(
            cli, ["profile", "save", "weir-profile", "--author", "Andy Weir"]
        )
        assert result.exit_code == 0, result.output

        profiles = config_store_mod.load_profiles()
        assert profiles["weir-profile"]["author"] == "Andy Weir"

    def test_author_in_config(self, tmp_config):
        """config set author saves and retrieves the author filter."""
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "author", "Andy Weir"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(cli, ["config", "get", "author"])
        assert result.exit_code == 0, result.output
        assert "Andy Weir" in result.output


class TestExcludeNarratorFilter:
    def test_exclude_narrator_single(self):
        products = [
            make_product(asin="EN1", narrators=["R.C. Bray"]),
            make_product(asin="EN2", narrators=["Scott Brick"]),
        ]
        filtered, breakdown = _filter_products(
            products, exclude_narrators=("R.C. Bray",)
        )
        asins = [p.asin for p in filtered]
        assert "EN1" not in asins
        assert "EN2" in asins
        assert breakdown == {"excluded narrators": 1}

    def test_exclude_narrator_substring(self):
        products = [
            make_product(asin="ENS1", narrators=["R.C. Bray"]),
            make_product(asin="ENS2", narrators=["Scott Brick"]),
        ]
        filtered, _ = _filter_products(products, exclude_narrators=("bray",))
        asins = [p.asin for p in filtered]
        assert "ENS1" not in asins
        assert "ENS2" in asins

    def test_exclude_narrator_case_insensitive(self):
        products = [make_product(asin="ENC1", narrators=["R.C. Bray"])]
        filtered, _ = _filter_products(products, exclude_narrators=("BRAY",))
        assert len(filtered) == 0

    def test_exclude_narrator_multiple(self):
        products = [
            make_product(asin="ENM1", narrators=["R.C. Bray"]),
            make_product(asin="ENM2", narrators=["Scott Brick"]),
            make_product(asin="ENM3", narrators=["Kate Reading"]),
        ]
        filtered, breakdown = _filter_products(
            products, exclude_narrators=("bray", "brick")
        )
        asins = [p.asin for p in filtered]
        assert "ENM1" not in asins
        assert "ENM2" not in asins
        assert "ENM3" in asins
        assert breakdown == {"excluded narrators": 2}

    def test_exclude_narrator_empty_no_filter(self):
        products = [make_product(asin="ENE1", narrators=["Anyone"])]
        filtered, breakdown = _filter_products(products, exclude_narrators=())
        assert len(filtered) == 1
        assert breakdown == {}

    def test_find_exclude_narrator_flag(self, mock_client, tmp_config):
        products = [
            make_product(
                asin="FEN1",
                price=3.0,
                narrators=["R.C. Bray"],
                series_name="",
                series_position="",
                num_ratings=10,
            ),
            make_product(
                asin="FEN2",
                price=3.0,
                narrators=["Scott Brick"],
                series_name="",
                series_position="",
                num_ratings=10,
            ),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "find_excl_narrator.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--exclude-narrator",
                "bray",
                "--all-languages",
                "-q",
                "-n",
                "0",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "FEN1" not in asins
        assert "FEN2" in asins

    def test_last_exclude_narrator_flag(self, tmp_config):

        products = [
            make_product(
                asin="LEN1",
                price=3.0,
                narrators=["R.C. Bray"],
                series_name="",
                series_position="",
            ),
            make_product(
                asin="LEN2",
                price=3.0,
                narrators=["Scott Brick"],
                series_name="",
                series_position="",
            ),
        ]
        constants_mod.LAST_RESULTS_FILE.write_text(
            json.dumps([_serialize_product(p) for p in products])
        )
        out_file = tmp_config / "last_excl_narrator.json"
        runner = CliRunner()
        result = runner.invoke(
            cli, ["last", "--exclude-narrator", "bray", "--output", str(out_file)]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "LEN1" not in asins
        assert "LEN2" in asins

    def test_exclude_narrator_in_profile(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "profile",
                "save",
                "no-bray",
                "--exclude-narrator",
                "R.C. Bray",
            ],
        )
        assert result.exit_code == 0, result.output

        profiles = config_store_mod.load_profiles()
        assert "no-bray" in profiles
        excluded = profiles["no-bray"]["exclude_narrators"]
        assert "R.C. Bray" in excluded


class TestFirstInSeriesStrict:
    def test_book3_only_gets_filtered_out(self):
        """A series with only Book 3 should be excluded (no Book 1)."""
        products = [
            make_product(asin="FIS1", series_name="Epic Series", series_position="3"),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 1
        assert len(result) == 0

    def test_prequel_at_half_passes(self):
        """Position 0.5 (prequel) is <= 1.0 so it passes through."""
        products = [
            make_product(asin="FIS2", series_name="Epic Series", series_position="0.5"),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 0
        assert len(result) == 1
        assert result[0].asin == "FIS2"

    def test_position_one_point_zero_passes(self):
        """Position '1.0' is exactly <= 1.0 so it passes."""
        products = [
            make_product(asin="FIS3", series_name="Epic Series", series_position="1.0"),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 0
        assert len(result) == 1
        assert result[0].asin == "FIS3"

    def test_book1_in_series_passes(self):
        """Position '1' passes through."""
        products = [
            make_product(asin="FIS4", series_name="A Series", series_position="1"),
            make_product(asin="FIS5", series_name="A Series", series_position="2"),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 1
        assert result[0].asin == "FIS4"

    def test_non_series_pass_through_unchanged(self):
        """Non-series items are never affected by the strict check."""
        products = [
            make_product(asin="FIS6", series_name=""),
            make_product(asin="FIS7", series_name=""),
        ]
        result, collapsed = _first_in_series(products)
        assert collapsed == 0
        assert len(result) == 2

    def test_mixed_book1_and_no_book1(self):
        """Series with Book 1 keeps it; series without Book 1 is excluded."""
        products = [
            make_product(asin="FIS8", series_name="HasBook1", series_position="1"),
            make_product(asin="FIS9", series_name="NoBook1", series_position="3"),
        ]
        result, collapsed = _first_in_series(products)
        asins = [p.asin for p in result]
        assert "FIS8" in asins
        assert "FIS9" not in asins
        assert collapsed == 1


class TestMaxPricePerHour:
    def test_filters_high_pph(self):
        products = [
            make_product(asin="CHEAP", price=2.0, length_minutes=600),  # $0.20/hr
            make_product(asin="EXPENSIVE", price=10.0, length_minutes=60),  # $10/hr
        ]
        filtered, breakdown = _filter_products(products, max_pph=1.0)
        assert len(filtered) == 1
        assert filtered[0].asin == "CHEAP"
        assert "max $/hr" in breakdown

    def test_no_filter_when_none(self):
        products = [make_product(price=10.0, length_minutes=60)]
        filtered, breakdown = _filter_products(products, max_pph=None)
        assert len(filtered) == 1

    def test_excludes_items_with_no_price(self):
        products = [
            make_product(asin="NOPRICE", price=None, length_minutes=600),
            make_product(asin="PRICED", price=2.0, length_minutes=600),
        ]
        filtered, breakdown = _filter_products(products, max_pph=1.0)
        assert len(filtered) == 1
        assert filtered[0].asin == "PRICED"

    def test_excludes_items_with_zero_hours(self):
        products = [
            make_product(asin="ZEROHRS", price=1.0, length_minutes=0),
            make_product(asin="PRICED", price=2.0, length_minutes=600),
        ]
        filtered, breakdown = _filter_products(products, max_pph=1.0)
        assert len(filtered) == 1
        assert filtered[0].asin == "PRICED"


class TestValueSort:
    def test_value_sort(self):
        high_value = make_product(asin="HV", price=2.0, length_minutes=1200, rating=4.8)
        # score = (4.8 * 20) / 2 = 48
        low_value = make_product(asin="LV", price=10.0, length_minutes=60, rating=3.0)
        # score = (3.0 * 1) / 10 = 0.3
        result = _sort_local([low_value, high_value], "value")
        assert result[0].asin == "HV"
        assert result[1].asin == "LV"

    def test_value_score_zero_price(self):
        """Free items (price=0.0) with valid rating and hours return inf."""
        p = make_product(price=0.0, length_minutes=600, rating=4.5)
        assert _value_score(p) == float("inf")

    def test_value_score_none_price(self):
        p = make_product(price=None, length_minutes=600, rating=4.5)
        assert _value_score(p) == 0.0

    def test_value_score_zero_hours(self):
        p = make_product(price=5.0, length_minutes=0, rating=4.5)
        assert _value_score(p) == 0.0

    def test_value_score_zero_rating(self):
        p = make_product(price=5.0, length_minutes=600, rating=0.0)
        assert _value_score(p) == 0.0

    def test_value_score_positive(self):
        p = make_product(price=2.0, length_minutes=600, rating=4.0)
        # (4.0 * 10) / 2 = 20
        assert _value_score(p) == pytest.approx(20.0)


class TestMinDiscount:
    def test_filters_low_discount(self):
        products = [
            make_product(asin="HIGH", price=3.0, list_price=20.0),  # 85% off
            make_product(asin="LOW", price=8.0, list_price=10.0),  # 20% off
            make_product(asin="NONE", price=5.0, list_price=5.0),  # 0% off
        ]
        filtered, breakdown = _filter_products(products, min_discount=50)
        assert len(filtered) == 1
        assert filtered[0].asin == "HIGH"
        assert "min discount" in breakdown

    def test_no_filter_when_zero(self):
        products = [make_product(price=5.0, list_price=5.0)]
        filtered, breakdown = _filter_products(products, min_discount=0)
        assert len(filtered) == 1


class TestValueSortTiebreaker:
    def test_tiebreaker_by_rating(self):
        """Items with same value score should sort by rating."""
        # score 0.0 because rating == 0
        unrated = make_product(
            asin="UNRATED", price=5.0, length_minutes=600, rating=0.0
        )
        # score 0.0 because hours == 0
        zero_hrs = make_product(asin="ZEROHRS", price=5.0, length_minutes=0, rating=4.5)
        result = _sort_local([unrated, zero_hrs], "value")
        # zero_hrs has rating 4.5 > 0.0, so it should come first
        assert result[0].asin == "ZEROHRS"
        assert result[1].asin == "UNRATED"


class TestFilterSeries:
    def test_filter_series_match(self):
        """Products with series_name containing the search string are kept."""
        products = [
            make_product(asin="S1", series_name="The Stormlight Archive"),
            make_product(asin="S2", series_name="Mistborn"),
            make_product(asin="S3", series_name="Stormlight Chronicles"),
        ]
        filtered, breakdown = _filter_products(products, series="stormlight")
        assert len(filtered) == 2
        assert all(p.asin in ("S1", "S3") for p in filtered)

    def test_filter_series_no_match(self):
        """Products without matching series are excluded."""
        products = [
            make_product(asin="S1", series_name="Mistborn"),
            make_product(asin="S2", series_name="The Way of Kings"),
        ]
        filtered, breakdown = _filter_products(products, series="wheel of time")
        assert len(filtered) == 0
        assert breakdown.get("series") == 2

    def test_filter_series_case_insensitive(self):
        """Series filter is case-insensitive."""
        products = [
            make_product(asin="S1", series_name="The Dresden Files"),
        ]
        filtered, _ = _filter_products(products, series="DRESDEN")
        assert len(filtered) == 1

    def test_filter_series_empty_no_filter(self):
        """Empty series string does not filter anything."""
        products = [
            make_product(asin="S1", series_name="Some Series"),
            make_product(asin="S2", series_name=""),
        ]
        filtered, breakdown = _filter_products(products, series="")
        assert len(filtered) == 2
        assert "series" not in breakdown


class TestPlusCatalogFilter:
    def test_skip_plus_excludes_plus_titles(self):
        products = [
            make_product(asin="P1", in_plus_catalog=True),
            make_product(asin="P2", in_plus_catalog=False),
        ]
        filtered, breakdown = _filter_products(products, skip_plus=True)
        assert len(filtered) == 1
        assert filtered[0].asin == "P2"
        assert breakdown.get("plus catalog") == 1

    def test_only_plus_keeps_only_plus(self):
        products = [
            make_product(asin="P1", in_plus_catalog=True),
            make_product(asin="P2", in_plus_catalog=False),
        ]
        filtered, breakdown = _filter_products(products, only_plus=True)
        assert len(filtered) == 1
        assert filtered[0].asin == "P1"
        assert breakdown.get("not plus") == 1

    def test_neither_flag_passes_all(self):
        products = [
            make_product(asin="P1", in_plus_catalog=True),
            make_product(asin="P2", in_plus_catalog=False),
        ]
        filtered, breakdown = _filter_products(products)
        assert len(filtered) == 2
        assert "plus catalog" not in breakdown
        assert "not plus" not in breakdown

    def test_find_skip_plus(self, mock_client, tmp_config):
        products = [
            make_product(
                asin="SP1",
                price=3.0,
                in_plus_catalog=True,
                series_name="",
                series_position="",
            ),
            make_product(
                asin="SP2",
                price=3.0,
                in_plus_catalog=False,
                series_name="",
                series_position="",
            ),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "skip_plus.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--all-languages",
                "--skip-plus",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "SP1" not in asins
        assert "SP2" in asins

    def test_find_only_plus(self, mock_client, tmp_config):
        products = [
            make_product(
                asin="OP1",
                price=3.0,
                in_plus_catalog=True,
                series_name="",
                series_position="",
            ),
            make_product(
                asin="OP2",
                price=3.0,
                in_plus_catalog=False,
                series_name="",
                series_position="",
            ),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "only_plus.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--all-languages",
                "--only-plus",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "OP1" in asins
        assert "OP2" not in asins

    def test_find_skip_plus_and_only_plus_mutually_exclusive(self):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--skip-plus",
                "--only-plus",
            ],
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output


class TestExcludeKeyword:
    def test_exclude_keyword_by_title(self):
        products = [
            make_product(asin="EK1", title="Foo Box Set"),
            make_product(asin="EK2", title="Foo Complete Edition"),
        ]
        filtered, breakdown = _filter_products(products, exclude_keywords=("box set",))
        asins = [p.asin for p in filtered]
        assert "EK1" not in asins
        assert "EK2" in asins
        assert breakdown.get("excluded keywords") == 1

    def test_exclude_keyword_case_insensitive(self):
        products = [make_product(asin="EK3", title="ABRIDGED VERSION")]
        filtered, _ = _filter_products(products, exclude_keywords=("abridged",))
        assert len(filtered) == 0

    def test_exclude_keyword_subtitle_match(self):
        products = [
            make_product(asin="EK4", title="Good Book", subtitle="Abridged Edition")
        ]
        filtered, _ = _filter_products(products, exclude_keywords=("abridged",))
        assert len(filtered) == 0

    def test_exclude_multiple_keywords(self):
        products = [
            make_product(asin="EK5", title="Box Set Collection"),
            make_product(asin="EK6", title="Abridged Cut"),
            make_product(asin="EK7", title="Full Novel"),
        ]
        filtered, breakdown = _filter_products(
            products, exclude_keywords=("abridged", "box set")
        )
        asins = [p.asin for p in filtered]
        assert "EK5" not in asins
        assert "EK6" not in asins
        assert "EK7" in asins
        assert breakdown.get("excluded keywords") == 2

    def test_find_exclude_keyword_flag(self, mock_client, tmp_config):
        products = [
            make_product(
                asin="FEK1",
                price=3.0,
                title="Abridged Story",
                series_name="",
                series_position="",
            ),
            make_product(
                asin="FEK2",
                price=3.0,
                title="Full Story",
                series_name="",
                series_position="",
            ),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "excl_kw.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--all-languages",
                "--exclude-keyword",
                "abridged",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "FEK1" not in asins
        assert "FEK2" in asins


class TestReleasedDateNormalization:
    def test_compact_released_after_is_normalized(self, mock_client, tmp_config):
        """Compact ISO form '20240101' parses and normalizes to '2024-01-01'."""
        from audible_deals.cli.catalog import _validate_history_filter_options

        after, before = _validate_history_filter_options(
            False, None, 0.0, "20240101", ""
        )
        assert after == "2024-01-01"
        assert before == ""

    def test_compact_released_before_is_normalized(self, mock_client, tmp_config):
        """Compact ISO form '20241231' normalizes to '2024-12-31'."""
        from audible_deals.cli.catalog import _validate_history_filter_options

        after, before = _validate_history_filter_options(
            False, None, 0.0, "", "20241231"
        )
        assert after == ""
        assert before == "2024-12-31"

    def test_dashed_dates_pass_through_unchanged(self, mock_client, tmp_config):
        """Standard dashed dates are returned as-is."""
        from audible_deals.cli.catalog import _validate_history_filter_options

        after, before = _validate_history_filter_options(
            False, None, 0.0, "2024-06-01", "2024-12-31"
        )
        assert after == "2024-06-01"
        assert before == "2024-12-31"
