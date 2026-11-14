"""Tests for the finish-cost footers and JSON in `deals series --gaps`."""

from __future__ import annotations

import json

from click.testing import CliRunner

import audible_deals.config_store as config_store_mod
from audible_deals.cli import cli
from tests.conftest import make_product, make_series_products_batch

CREDIT = 11.25


def _text(result) -> str:
    """Rendered output with line wrapping collapsed, so assertions ignore width."""
    return " ".join(result.output.split())


def _setup(mock_client, series: dict[str, list]) -> None:
    """Give the user two owned books in each series, plus the given gap products.

    Each gap is a price, None for an unavailable book, or a dict of make_product
    overrides that must include "price".
    """
    library = []
    catalog = {}
    for index, (name, gaps) in enumerate(series.items()):
        series_asin = f"SER_{index}"
        for owned in (1, 2):
            library.append(
                make_product(
                    asin=f"OWN_{index}_{owned}",
                    title=f"{name} Owned {owned}",
                    series_name=name,
                    series_asin=series_asin,
                    series_position=str(owned),
                )
            )
        catalog[series_asin] = [
            make_product(
                asin=f"GAP_{index}_{position}",
                title=f"{name} Gap {position}",
                series_name=name,
                series_position=str(position),
                **(gap if isinstance(gap, dict) else {"price": gap}),
            )
            for position, gap in enumerate(gaps, start=3)
        ]
    mock_client.get_library.return_value = library
    mock_client.get_series_products_many.return_value = make_series_products_batch(
        catalog
    )


def _gaps(*extra_args, credit_price=CREDIT):
    if credit_price is not None:
        config_store_mod.save_config({"credit_price": credit_price})
    return CliRunner().invoke(cli, [*extra_args, "series", "--gaps"])


class TestFooterVariants:
    def test_mixed_cash_and_credit(self, tmp_config, mock_client):
        _setup(mock_client, {"Alpha Series": [4.99, 20.0, 24.99, 34.98]})

        result = _gaps()

        assert result.exit_code == 0, result.output
        assert "Finish: 4 books · $84.96 cash\n" in result.output
        assert (
            "Cheapest: 3 credits + $4.99 cash ≈ $38.74 at $11.25 per credit "
            "(saves $46.22)" in _text(result)
        )

    def test_all_cash(self, tmp_config, mock_client):
        _setup(mock_client, {"Alpha Series": [5.0, 6.0, 7.0]})

        result = _gaps()

        assert "Finish: 3 books · $18.00 cash" in _text(result)
        assert (
            "Cheapest: all cash ($18.00); every book is under your $11.25 credit"
            in _text(result)
        )

    def test_all_credit(self, tmp_config, mock_client):
        _setup(mock_client, {"Alpha Series": [15.0, 20.0, 25.0, 30.0]})

        result = _gaps()

        assert "Finish: 4 books · $90.00 cash" in _text(result)
        assert (
            "Cheapest: 4 credits ≈ $45.00 at $11.25 per credit (saves $45.00)"
            in _text(result)
        )

    def test_book_at_exactly_the_credit_price_is_on_credit(
        self, tmp_config, mock_client
    ):
        _setup(mock_client, {"Alpha Series": [11.25, 5.0]})

        result = _gaps()

        text = _text(result)
        assert "Cheapest: 1 credit + $5.00 cash ≈ $16.25 at $11.25 per credit" in text
        assert "saves" not in text

    def test_no_credit_line_without_credit_price(self, tmp_config, mock_client):
        _setup(mock_client, {"Alpha Series": [4.99, 20.0]})

        result = _gaps(credit_price=None)

        assert "Finish: 2 books · $24.99 cash" in _text(result)
        assert "Cheapest" not in result.output

    def test_footer_follows_the_rows_of_its_series(self, tmp_config, mock_client):
        _setup(mock_client, {"Alpha Series": [4.99, 20.0]})

        lines = _gaps().output.splitlines()

        finish = next(i for i, line in enumerate(lines) if "Finish:" in line)
        assert "Alpha Series Gap 4" in lines[finish - 1]
        assert lines[finish].startswith("  Finish:")
        assert lines[finish + 1].startswith("  Cheapest:")


class TestSingularForms:
    def test_one_book_one_credit(self, tmp_config, mock_client):
        _setup(mock_client, {"Alpha Series": [20.0]})

        result = _gaps()

        assert "Finish: 1 book · $20.00 cash" in _text(result)
        assert (
            "Cheapest: 1 credit ≈ $11.25 at $11.25 per credit (saves $8.75)"
            in _text(result)
        )
        assert "1 books" not in result.output
        assert "1 credits" not in result.output

    def test_one_book_all_cash(self, tmp_config, mock_client):
        _setup(mock_client, {"Alpha Series": [5.0]})

        result = _gaps()

        assert "Finish: 1 book · $5.00 cash" in _text(result)
        assert (
            "Cheapest: all cash ($5.00); the book is under your $11.25 credit"
            in _text(result)
        )

    def test_plural_credits(self, tmp_config, mock_client):
        _setup(mock_client, {"Alpha Series": [20.0, 30.0]})

        assert "Cheapest: 2 credits ≈ $22.50" in _text(_gaps())


class TestSuffixes:
    def test_unavailable_and_plus_notes(self, tmp_config, mock_client):
        _setup(
            mock_client,
            {
                "Alpha Series": [
                    {"price": 20.0, "in_plus_catalog": True},
                    5.0,
                    None,
                    None,
                ]
            },
        )

        result = _gaps(credit_price=None)

        assert (
            "Finish: 2 books · $25.00 cash · 2 unavailable not included "
            "· 1 included with Plus" in _text(result)
        )

    def test_only_unavailable_note(self, tmp_config, mock_client):
        _setup(mock_client, {"Alpha Series": [5.0, None]})

        assert "Finish: 1 book · $5.00 cash · 1 unavailable not included" in _text(
            _gaps(credit_price=None)
        )
        assert "Plus" not in _gaps(credit_price=None).output

    def test_plus_books_still_count_toward_the_cost(self, tmp_config, mock_client):
        _setup(
            mock_client,
            {"Alpha Series": [{"price": 20.0, "in_plus_catalog": True}, 5.0]},
        )

        result = _gaps()

        assert "Finish: 2 books · $25.00 cash · 1 included with Plus" in _text(result)
        assert "Cheapest: 1 credit + $5.00 cash ≈ $16.25" in _text(result)

    def test_all_unavailable_series_has_no_footer(self, tmp_config, mock_client):
        _setup(mock_client, {"Alpha Series": [None, None]})

        result = _gaps()

        assert result.exit_code == 0, result.output
        assert "Alpha Series Gap 3" in result.output
        assert "Finish:" not in result.output
        assert "Cheapest" not in result.output
        assert "All " not in result.output


class TestGrandTotal:
    def test_total_after_the_last_series_with_credit_price(
        self, tmp_config, mock_client
    ):
        _setup(
            mock_client,
            {"Alpha Series": [4.99, 20.0], "Beta Series": [3.0, 15.0, 25.0]},
        )

        result = _gaps()

        last = result.output.rstrip().splitlines()[-1]
        assert last == (
            "All 2 series: 5 books · $67.99 cash · cheapest 3 credits + $7.99 cash "
            "≈ $41.74"
        )

    def test_total_without_credit_price(self, tmp_config, mock_client):
        _setup(
            mock_client,
            {"Alpha Series": [4.99, 20.0], "Beta Series": [3.0, 15.0, 25.0]},
        )

        result = _gaps(credit_price=None)

        assert result.output.rstrip().splitlines()[-1] == (
            "All 2 series: 5 books · $67.99 cash"
        )

    def test_total_counts_only_series_with_a_footer(self, tmp_config, mock_client):
        _setup(
            mock_client,
            {
                "Alpha Series": [5.0],
                "Beta Series": [None],
                "Gamma Series": [20.0],
            },
        )

        result = _gaps(credit_price=None)

        assert "All 2 series: 2 books · $25.00 cash" in result.output

    def test_no_total_for_a_single_footer(self, tmp_config, mock_client):
        _setup(mock_client, {"Alpha Series": [5.0, 20.0], "Beta Series": [None]})

        result = _gaps()

        assert result.output.count("Finish:") == 1
        assert "All " not in result.output

    def test_total_with_all_cash_series(self, tmp_config, mock_client):
        _setup(mock_client, {"Alpha Series": [5.0], "Beta Series": [6.0]})

        assert "All 2 series: 2 books · $11.00 cash · cheapest all cash" in _text(
            _gaps()
        )

    def test_total_with_all_credit_series(self, tmp_config, mock_client):
        _setup(mock_client, {"Alpha Series": [15.0], "Beta Series": [20.0]})

        assert "All 2 series: 2 books · $35.00 cash · cheapest 2 credits ≈ $22.50" in (
            _text(_gaps())
        )


class TestCurrencyAndFilters:
    def test_uses_the_marketplace_currency_symbol(self, tmp_config, mock_client):
        _setup(mock_client, {"Alpha Series": [4.99, 20.0], "Beta Series": [20.0]})

        result = _gaps("--locale", "uk")

        assert "£" in result.output
        assert "$" not in result.output
        assert "Finish: 2 books · £24.99 cash" in _text(result)
        assert (
            "Cheapest: 1 credit + £4.99 cash ≈ £16.24 at £11.25 per credit "
            "(saves £8.75)" in _text(result)
        )
        assert "All 2 series: 3 books · £44.99 cash" in _text(result)

    def test_max_price_footer_matches_the_displayed_rows(self, tmp_config, mock_client):
        _setup(mock_client, {"Alpha Series": [None, 4.0, 6.0]})
        config_store_mod.save_config({"credit_price": CREDIT})

        result = CliRunner().invoke(cli, ["series", "--gaps", "--max-price", "5"])

        assert result.exit_code == 0, result.output
        assert "Alpha Series Gap 4" in result.output
        assert "Alpha Series Gap 5" not in result.output
        assert "Finish: 1 book · $4.00 cash · 1 unavailable not included" in _text(
            result
        )
        assert (
            "Cheapest: all cash ($4.00); the book is under your $11.25 credit"
            in _text(result)
        )

    def test_max_price_that_hides_every_priced_gap_leaves_no_footer(
        self, tmp_config, mock_client
    ):
        _setup(mock_client, {"Alpha Series": [None, 20.0]})

        result = CliRunner().invoke(cli, ["series", "--gaps", "--max-price", "5"])

        assert result.exit_code == 0, result.output
        assert "Alpha Series Gap 3" in result.output
        assert "Finish:" not in result.output


class TestJsonFinish:
    def test_finish_object_with_credit_price(self, tmp_config, mock_client):
        _setup(
            mock_client,
            {
                "Alpha Series": [
                    4.99,
                    20.0,
                    24.99,
                    {"price": 34.98, "in_plus_catalog": True},
                    None,
                ]
            },
        )
        config_store_mod.save_config({"credit_price": CREDIT})

        result = CliRunner().invoke(cli, ["series", "--gaps", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert data[0]["finish"] == {
            "books": 4,
            "cash_total": 84.96,
            "unavailable": 1,
            "plus": 1,
            "credits": 3,
            "cash_part": 4.99,
            "best_total": 38.74,
            "saved": 46.22,
            "credit_price": 11.25,
        }

    def test_finish_object_without_credit_price(self, tmp_config, mock_client):
        _setup(mock_client, {"Alpha Series": [4.99, 20.0, None]})

        result = CliRunner().invoke(cli, ["series", "--gaps", "--json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)[0]["finish"] == {
            "books": 2,
            "cash_total": 24.99,
            "unavailable": 1,
            "plus": 0,
        }

    def test_existing_keys_are_kept_and_there_is_no_top_level_total(
        self, tmp_config, mock_client
    ):
        _setup(mock_client, {"Alpha Series": [5.0], "Beta Series": [20.0]})

        result = CliRunner().invoke(cli, ["series", "--gaps", "--json"])

        data = json.loads(result.stdout)
        assert [entry["series"] for entry in data] == ["Alpha Series", "Beta Series"]
        assert all(
            set(entry) == {"series", "owned", "total_known", "missing", "finish"}
            for entry in data
        )
        assert data[0]["missing"] == [
            {
                "asin": "GAP_0_3",
                "title": "Alpha Series Gap 3",
                "position": "3",
                "price": 5.0,
            }
        ]
        assert "All " not in result.stdout

    def test_all_unavailable_series_reports_zero_books(self, tmp_config, mock_client):
        _setup(mock_client, {"Alpha Series": [None, None]})
        config_store_mod.save_config({"credit_price": CREDIT})

        result = CliRunner().invoke(cli, ["series", "--gaps", "--json"])

        assert json.loads(result.stdout)[0]["finish"] == {
            "books": 0,
            "cash_total": 0.0,
            "unavailable": 2,
            "plus": 0,
            "credits": 0,
            "cash_part": 0.0,
            "best_total": 0.0,
            "saved": 0.0,
            "credit_price": 11.25,
        }

    def test_max_price_applies_to_the_json_finish_object(self, tmp_config, mock_client):
        _setup(mock_client, {"Alpha Series": [None, 4.0, 6.0]})

        result = CliRunner().invoke(
            cli, ["series", "--gaps", "--json", "--max-price", "5"]
        )

        assert json.loads(result.stdout)[0]["finish"] == {
            "books": 1,
            "cash_total": 4.0,
            "unavailable": 1,
            "plus": 0,
        }
