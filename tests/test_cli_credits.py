"""`deals credits` CLI behavior."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

import audible_deals.config_store as config_store_mod
import audible_deals.wishlist as wishlist_mod
from audible_deals.cli import cli
from tests.conftest import make_product


def _entry(asin: str, **extra) -> dict:
    return {"asin": asin, "title": f"Entry {asin}", "max_price": None, **extra}


def _book(asin: str, price: float | None, **overrides):
    return make_product(
        asin=asin,
        title=f"Book {asin}",
        authors=["Ann Author"],
        price=price,
        **overrides,
    )


@pytest.fixture
def client(mock_client):
    mock_client.get_library_asins.return_value = set()
    mock_client.get_wishlist.return_value = []
    mock_client.get_products_batch.return_value = []
    return mock_client


def _run(*args):
    return CliRunner().invoke(cli, ["credits", *args], catch_exceptions=False)


def _flat(result) -> str:
    """Output with wrapped lines rejoined, for assertions on long sentences."""
    return " ".join(result.output.split())


def _plan(result) -> dict:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


class TestDefaultSource:
    def test_plans_the_local_wishlist(self, tmp_config, client):
        config_store_mod.save_config({"credit_price": 11.25})
        wishlist_mod.save_wishlist([_entry("BIG1"), _entry("BIG2"), _entry("CHEAP")])
        client.get_products_batch.return_value = [
            _book("BIG1", 38.49),
            _book("BIG2", 28.99),
            _book("CHEAP", 4.99),
        ]

        result = _run("1")

        assert result.exit_code == 0, result.output
        assert "Credit plan · 1 credit · credit price $11.25" in result.output
        assert "CREDIT" in result.output and "Book BIG1 · Ann Author" in result.output
        assert "$38.49" in result.output
        assert "WAIT" in result.output and "Book BIG2" in result.output
        assert "CASH" in result.output and "cheaper than a credit" in result.output
        assert "1 credit buys $38.49 of books" in _flat(result)
        assert "$27.24 more than it costs you" in _flat(result)
        assert "Cash today: $4.99 for 1 book." in result.output
        client.get_wishlist.assert_not_called()
        assert sorted(client.get_products_batch.call_args[0][0]) == [
            "BIG1",
            "BIG2",
            "CHEAP",
        ]

    def test_credits_default_to_one(self, tmp_config, client):
        wishlist_mod.save_wishlist([_entry("AAA"), _entry("BBB")])
        client.get_products_batch.return_value = [
            _book("AAA", 30.0),
            _book("BBB", 20.0),
        ]

        plan = _plan(_run("--json"))

        assert plan["credits"] == 1
        assert [p["asin"] for p in plan["credit"]] == ["AAA"]
        assert [p["asin"] for p in plan["wait"]] == ["BBB"]

    def test_without_credit_price_hints_and_has_no_cash_section(
        self, tmp_config, client
    ):
        wishlist_mod.save_wishlist([_entry("AAA"), _entry("CHEAP")])
        client.get_products_batch.return_value = [
            _book("AAA", 30.0),
            _book("CHEAP", 2.0),
        ]

        result = _run("2")

        assert result.exit_code == 0, result.output
        assert "credit price" not in result.output
        assert "CASH" not in result.output
        assert "more than they cost you" not in result.output
        assert (
            "Set what you pay per credit with: deals config set credit-price 11.25"
            in _flat(result)
        )

    def test_leftover_credits_are_explained(self, tmp_config, client):
        config_store_mod.save_config({"credit_price": 11.25})
        wishlist_mod.save_wishlist([_entry("AAA"), _entry("CHEAP")])
        client.get_products_batch.return_value = [
            _book("AAA", 30.0),
            _book("CHEAP", 2.0),
        ]

        with_price = _flat(_run("3"))
        config_store_mod.save_config({})
        without_price = _flat(_run("3"))

        assert (
            "2 credits left over: nothing else on your wishlist costs more than a credit."
            in with_price
        )
        assert (
            "1 credit left over: no other priced books on your wishlist."
            in without_price
        )

    def test_plus_and_unpriced_books(self, tmp_config, client):
        wishlist_mod.save_wishlist([_entry("AAA"), _entry("PLUS"), _entry("NOPRICE")])
        client.get_products_batch.return_value = [
            _book("AAA", 30.0),
            _book("PLUS", 15.0, in_plus_catalog=True),
            _book("NOPRICE", None),
        ]

        result = _run("1")

        assert result.exit_code == 0, result.output
        assert "PLUS" in result.output and "included with Plus" in result.output
        assert "1 book skipped: no price listed." in result.output

    def test_long_wait_list_is_summarized(self, tmp_config, client):
        asins = [f"W{i:02d}" for i in range(12)]
        wishlist_mod.save_wishlist([_entry(a) for a in asins])
        client.get_products_batch.return_value = [
            _book(a, 30.0 - i) for i, a in enumerate(asins)
        ]

        result = _run("1")

        assert "11 more, from $29.00 down to $19.00" in result.output
        assert "Book W05" not in result.output

    def test_ten_waiting_books_are_listed(self, tmp_config, client):
        asins = [f"W{i:02d}" for i in range(11)]
        wishlist_mod.save_wishlist([_entry(a) for a in asins])
        client.get_products_batch.return_value = [
            _book(a, 30.0 - i) for i, a in enumerate(asins)
        ]

        result = _run("1")

        assert "more, from" not in result.output
        assert "Book W10" in result.output

    def test_uses_the_marketplace_currency(self, tmp_config, client):
        wishlist_mod.save_wishlist([_entry("AAA")])
        client.get_products_batch.return_value = [_book("AAA", 30.0, locale="uk")]

        result = CliRunner().invoke(cli, ["--locale", "uk", "credits"])

        assert result.exit_code == 0, result.output
        assert "£30.00" in result.output
        assert "$" not in result.output

    def test_author_watches_are_not_books(self, tmp_config, client):
        wishlist_mod.save_wishlist(
            [
                _entry("AAA"),
                {"type": "author", "author": "Ann Author", "max_price": 5.0},
            ]
        )
        client.get_products_batch.return_value = [_book("AAA", 30.0)]

        result = _run()

        assert result.exit_code == 0, result.output
        client.get_products_batch.assert_called_once_with(["AAA"])


class TestOwnedBooks:
    def test_owned_books_are_removed(self, tmp_config, client):
        wishlist_mod.save_wishlist([_entry("OWNED"), _entry("NEW")])
        client.get_products_batch.return_value = [
            _book("OWNED", 40.0),
            _book("NEW", 20.0),
        ]
        client.get_library_asins.return_value = {"OWNED"}

        plan = _plan(_run("--json"))

        assert [p["asin"] for p in plan["credit"]] == ["NEW"]
        assert plan["wait"] == []

    def test_everything_owned_is_not_an_empty_wishlist(self, tmp_config, client):
        wishlist_mod.save_wishlist([_entry("OWNED")])
        client.get_products_batch.return_value = [_book("OWNED", 40.0)]
        client.get_library_asins.return_value = {"OWNED"}

        result = _run("2")

        assert result.exit_code == 0, result.output
        assert "2 credits left over" in result.output


class TestAudibleWishlist:
    def test_merges_with_local_entries_and_dedupes_by_asin(self, tmp_config, client):
        wishlist_mod.save_wishlist([_entry("LOCAL"), _entry("BOTH")])
        client.get_wishlist.return_value = [
            _book("BOTH", 25.0),
            _book("ACCOUNT", 35.0),
        ]
        client.get_products_batch.return_value = [_book("LOCAL", 30.0)]

        plan = _plan(_run("5", "--audible-wishlist", "--json"))

        assert sorted(p["asin"] for p in plan["credit"]) == ["ACCOUNT", "BOTH", "LOCAL"]
        client.get_products_batch.assert_called_once_with(["LOCAL"])

    def test_works_with_an_empty_local_wishlist(self, tmp_config, client):
        client.get_wishlist.return_value = [_book("ACCOUNT", 35.0)]

        plan = _plan(_run("--audible-wishlist", "--json"))

        assert [p["asin"] for p in plan["credit"]] == ["ACCOUNT"]
        client.get_products_batch.assert_not_called()

    def test_owned_account_books_are_removed(self, tmp_config, client):
        client.get_wishlist.return_value = [_book("OWNED", 35.0), _book("NEW", 20.0)]
        client.get_library_asins.return_value = {"OWNED"}

        plan = _plan(_run("--audible-wishlist", "--json"))

        assert [p["asin"] for p in plan["credit"]] == ["NEW"]


class TestOtherMarketplaces:
    def test_entries_for_other_marketplaces_are_skipped_with_a_notice(
        self, tmp_config, client
    ):
        wishlist_mod.save_wishlist(
            [
                _entry("US1", locale="us"),
                _entry("NOLOCALE"),
                _entry("UK1", locale="uk"),
                _entry("DE1", locale="de"),
            ]
        )
        client.get_products_batch.return_value = [
            _book("US1", 30.0),
            _book("NOLOCALE", 20.0),
        ]

        result = _run("2")

        assert result.exit_code == 0, result.output
        assert "Skipped 2 wishlist entries from other marketplaces." in result.output
        assert sorted(client.get_products_batch.call_args[0][0]) == [
            "NOLOCALE",
            "US1",
        ]

    def test_notice_uses_the_singular(self, tmp_config, client):
        wishlist_mod.save_wishlist([_entry("US1"), _entry("UK1", locale="uk")])
        client.get_products_batch.return_value = [_book("US1", 30.0)]

        result = _run()

        assert "Skipped 1 wishlist entry from other marketplaces." in result.output

    def test_no_notice_when_nothing_is_skipped(self, tmp_config, client):
        wishlist_mod.save_wishlist([_entry("US1")])
        client.get_products_batch.return_value = [_book("US1", 30.0)]

        assert "Skipped" not in _run().output

    def test_active_marketplace_selects_the_entries(self, tmp_config, client):
        wishlist_mod.save_wishlist(
            [_entry("US1", locale="us"), _entry("UK1", locale="uk")]
        )
        client.get_products_batch.return_value = [_book("UK1", 30.0, locale="uk")]

        result = CliRunner().invoke(cli, ["--locale", "uk", "credits"])

        assert result.exit_code == 0, result.output
        client.get_products_batch.assert_called_once_with(["UK1"])
        assert "Skipped 1 wishlist entry from other marketplaces." in result.output

    def test_only_other_marketplaces_is_an_empty_wishlist(self, tmp_config, client):
        wishlist_mod.save_wishlist([_entry("UK1", locale="uk")])

        result = _run()

        assert result.exit_code != 0
        assert "Skipped 1 wishlist entry from other marketplaces." in result.output
        assert "Your wishlist is empty" in result.output
        client.get_products_batch.assert_not_called()


class TestJson:
    def test_shape(self, tmp_config, client):
        config_store_mod.save_config({"credit_price": 11.25})
        wishlist_mod.save_wishlist(
            [
                _entry("BIG"),
                _entry("WAITER"),
                _entry("CHEAP"),
                _entry("PLUS"),
                _entry("NOPRICE"),
            ]
        )
        client.get_products_batch.return_value = [
            _book("BIG", 38.49),
            _book("WAITER", 20.0),
            _book("CHEAP", 4.99),
            _book("PLUS", 9.0, in_plus_catalog=True),
            _book("NOPRICE", None),
        ]

        plan = _plan(_run("1", "--json"))

        assert set(plan) == {
            "credits",
            "credit_price",
            "credit",
            "cash",
            "plus",
            "wait",
            "unavailable",
            "totals",
        }
        assert plan["credits"] == 1
        assert plan["credit_price"] == 11.25
        assert [p["asin"] for p in plan["credit"]] == ["BIG"]
        assert [p["asin"] for p in plan["cash"]] == ["CHEAP"]
        assert [p["asin"] for p in plan["plus"]] == ["PLUS"]
        assert [p["asin"] for p in plan["wait"]] == ["WAITER"]
        assert plan["unavailable"] == 1
        assert plan["credit"][0]["full_title"] == "Book BIG"
        assert plan["credit"][0]["price"] == 38.49
        assert plan["totals"] == {
            "cash_total": 4.99,
            "credit_value": 38.49,
            "value_per_credit": 38.49,
            "credits_unused": 0,
            "saved": 27.24,
        }

    def test_credit_price_is_null_when_unset(self, tmp_config, client):
        wishlist_mod.save_wishlist([_entry("AAA")])
        client.get_products_batch.return_value = [_book("AAA", 30.0)]

        plan = _plan(_run("2", "--json"))

        assert plan["credit_price"] is None
        assert plan["cash"] == []
        assert plan["totals"]["saved"] is None
        assert plan["totals"]["credits_unused"] == 1

    def test_stdout_is_only_json(self, tmp_config, client):
        wishlist_mod.save_wishlist([_entry("AAA"), _entry("UK1", locale="uk")])
        client.get_products_batch.return_value = [_book("AAA", 30.0)]

        result = _run("--json")

        assert result.exit_code == 0, result.output
        json.loads(result.stdout)
        assert "Skipped 1 wishlist entry" in result.stderr
        assert "Credit plan" not in result.stdout


class TestErrors:
    def test_empty_wishlist_errors_without_calling_audible(self, tmp_config, client):
        result = _run()

        assert result.exit_code != 0
        assert "Your wishlist is empty" in result.output
        assert "deals wishlist add" in result.output
        assert "--audible-wishlist" in result.output
        client.get_products_batch.assert_not_called()
        client.get_library_asins.assert_not_called()

    def test_empty_account_wishlist_errors(self, tmp_config, client):
        result = _run("--audible-wishlist")

        assert result.exit_code != 0
        assert "Your wishlist is empty" in result.output
        assert "deals wishlist add" in result.output
        assert "or pass --audible-wishlist" not in result.output

    @pytest.mark.parametrize("value", ["0", "-1", "two"])
    def test_credits_must_be_a_positive_integer(self, tmp_config, client, value):
        wishlist_mod.save_wishlist([_entry("AAA")])

        result = CliRunner().invoke(cli, ["credits", "--", value])

        assert result.exit_code == 2
        assert "Invalid value" in result.output
        client.get_products_batch.assert_not_called()

    def test_corrupt_credit_price_is_reported(self, tmp_config, client):
        wishlist_mod.save_wishlist([_entry("AAA")])
        (tmp_config / "config.json").write_text('{"credit_price": "cheap"}')

        result = _run()

        assert result.exit_code != 0
        assert "credit_price" in result.output
        client.get_products_batch.assert_not_called()


def test_help_lists_the_command_and_options(tmp_config):
    result = CliRunner().invoke(cli, ["credits", "--help"])

    assert result.exit_code == 0, result.output
    assert "[N]" in result.output
    assert "--audible-wishlist" in result.output
    assert "--json" in result.output
    top = CliRunner().invoke(cli, ["--help"])
    assert "credits" in top.output
