"""Whispersync for Voice: Kindle book + Audible narration add-on pricing."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

import audible_deals.config_store as config_store_mod
from audible_deals.cli import cli
from audible_deals.filtering import filter_products
from audible_deals.metrics import whispersync_margin
from audible_deals.presentation.markdown import render_markdown
from audible_deals.presentation.products import (
    display_comparison,
    display_product_detail,
    display_products,
)
from audible_deals.product import parse_product
from audible_deals.result_models import FilterContext
from audible_deals.results_cache import load_result_session
from audible_deals.serialization import deserialize_product, serialize_product
from tests.conftest import make_product
from tests.test_display import _capture


def _raw(**overrides):
    raw = {
        "asin": "B0F1GDQ6TP",
        "title": "Inventing the Renaissance",
        "price": {
            "lowest_price": {"base": 20.99},
            "list_price": {"base": 30.12},
            "ws4v_upsell_price": {"base": 5.599999904632568},
        },
        "is_ws4v_enabled": True,
        "ws4v_companion_asin": "B0DNNGRN3C",
        "is_ws4v_companion_asin_owned": False,
    }
    raw.update(overrides)
    return raw


def _ws(**overrides):
    values = dict(
        asin="WS1",
        price=20.99,
        whispersync_price=5.60,
        kindle_asin="B0DNNGRN3C",
        series_name="",
        series_position="",
    )
    values.update(overrides)
    return make_product(**values)


class TestParse:
    def test_reads_addon_price_and_kindle_book(self):
        p = parse_product(_raw())
        assert p.whispersync_price == pytest.approx(5.60)
        assert p.kindle_asin == "B0DNNGRN3C"
        assert p.owns_kindle is False
        assert p.kindle_url == "https://www.amazon.com/dp/B0DNNGRN3C"

    def test_reads_owned_kindle_book(self):
        assert parse_product(_raw(is_ws4v_companion_asin_owned=True)).owns_kindle

    def test_kindle_owner_price_is_the_narration_price(self):
        # Seen live: for Kindle owners the API sends the add-on price as the
        # regular price and drops ws4v_upsell_price.
        raw = _raw(is_ws4v_companion_asin_owned=True)
        del raw["price"]["ws4v_upsell_price"]
        raw["price"]["lowest_price"] = {"base": 4.82}
        p = parse_product(raw)
        assert p.owns_kindle is True
        assert p.whispersync_price == pytest.approx(4.82) == p.price

    @pytest.mark.parametrize(
        "overrides",
        [
            # The API prices titles that have no Kindle companion; ignore those.
            {"is_ws4v_enabled": False, "ws4v_companion_asin": None},
            {"is_ws4v_enabled": None},
            {"ws4v_companion_asin": ""},
            {"is_ws4v_enabled": "true"},
            {"ws4v_companion_asin": "B0DN)](https://evil.example"},
        ],
    )
    def test_ignores_price_without_whispersync_companion(self, overrides):
        p = parse_product(_raw(**overrides))
        assert p.whispersync_price is None
        assert p.kindle_asin == ""
        assert p.owns_kindle is False

    def test_bad_addon_price_keeps_the_kindle_link(self):
        raw = _raw()
        raw["price"]["ws4v_upsell_price"] = {"base": "n/a"}
        p = parse_product(raw)
        assert p.whispersync_price is None
        assert p.kindle_asin == "B0DNNGRN3C"

    def test_kindle_url_follows_marketplace(self):
        p = parse_product(_raw(), locale="uk")
        assert p.kindle_url == "https://www.amazon.co.uk/dp/B0DNNGRN3C"


class TestMargin:
    def test_against_cash_price(self):
        assert whispersync_margin(_ws(), None) == pytest.approx(15.39)

    def test_against_cheaper_credit(self):
        assert whispersync_margin(_ws(), 9.57) == pytest.approx(3.97)

    def test_none_without_addon_or_price(self):
        assert whispersync_margin(_ws(whispersync_price=None), None) is None
        assert whispersync_margin(_ws(price=None), None) is None


class TestFilter:
    def test_keeps_only_books_where_whispersync_can_win(self):
        products = [
            _ws(asin="WIN"),
            _ws(asin="LOSE", price=14.99, whispersync_price=16.85),
            _ws(asin="NONE", whispersync_price=None, kindle_asin=""),
        ]
        outcome = filter_products(products, FilterContext(whispersync=True))
        assert [p.asin for p in outcome.products] == ["WIN"]
        assert outcome.breakdown["whispersync"] == 2

    def test_compares_against_a_cheaper_credit(self):
        products = [_ws(asin="CREDIT", price=20.99, whispersync_price=10.0)]
        outcome = filter_products(
            products, FilterContext(whispersync=True, credit_price=9.57)
        )
        assert outcome.products == ()

    def test_keeps_kindle_owner_price_unless_a_credit_is_cheaper(self):
        owner = _ws(asin="OWNER", price=4.82, whispersync_price=4.82, owns_kindle=True)
        assert filter_products([owner], FilterContext(whispersync=True)).products
        pricey = _ws(price=24.0, whispersync_price=24.0, owns_kindle=True)
        outcome = filter_products(
            [pricey], FilterContext(whispersync=True, credit_price=9.57)
        )
        assert outcome.products == ()

    def test_off_by_default(self):
        products = [_ws(whispersync_price=None, kindle_asin="")]
        assert len(filter_products(products, FilterContext()).products) == 1


class TestSerialization:
    def test_round_trip(self):
        product = _ws(owns_kindle=True)
        data = serialize_product(product)
        assert data["whispersync_price"] == 5.6
        assert data["kindle_url"] == "https://www.amazon.com/dp/B0DNNGRN3C"
        assert deserialize_product(json.loads(json.dumps(data))) == product

    def test_older_cache_entries_default_to_no_whispersync(self):
        data = serialize_product(make_product())
        for key in ("whispersync_price", "kindle_asin", "owns_kindle", "kindle_url"):
            data.pop(key)
        product = deserialize_product(data)
        assert product.whispersync_price is None
        assert product.kindle_url == ""


class TestDisplay:
    def test_detail_shows_kindle_break_even_and_link(self):
        out = _capture(display_product_detail, _ws())
        assert "Whispersync:" in out
        assert "$5.60 to add narration to the Kindle book" in out
        assert "Cheaper if the Kindle book costs under $15.39" in out
        assert "Kindle: https://www.amazon.com/dp/B0DNNGRN3C" in out

    def test_detail_with_credit_price_compares_against_the_credit(self):
        out = _capture(display_product_detail, _ws(), credit_price=9.57)
        assert "Beats a credit if the Kindle book costs under $3.97" in out

    def test_detail_shows_savings_when_kindle_book_owned(self):
        out = _capture(display_product_detail, _ws(owns_kindle=True))
        assert "$5.60 to add narration to your Kindle book" in out
        assert "Saves $15.39 over the Audible price" in out

    def test_detail_says_when_addon_is_not_cheaper(self):
        out = _capture(
            display_product_detail, _ws(price=14.99, whispersync_price=16.85)
        )
        assert "$16.85 to add narration to the Kindle book" in out
        assert "No cheaper than the Audible price" in out

    def test_detail_with_kindle_price_gives_the_total(self):
        out = _capture(display_product_detail, _ws(), kindle_price=9.99)
        assert "Kindle $9.99 + narration = $15.59, saves $5.40 over" in out

    def test_detail_with_kindle_price_says_when_audible_wins(self):
        out = _capture(display_product_detail, _ws(), kindle_price=16.99)
        assert "Kindle $16.99 + narration = $22.59, $1.60 more than" in out

    def test_detail_with_kindle_price_compares_against_credit(self):
        out = _capture(
            display_product_detail, _ws(), credit_price=9.57, kindle_price=2.99
        )
        assert "= $8.59, saves $0.98 over a credit" in out

    def test_owned_kindle_book_ignores_kindle_price(self):
        out = _capture(display_product_detail, _ws(owns_kindle=True), kindle_price=9.99)
        assert "Saves $15.39 over the Audible price" in out

    def test_detail_kindle_owner_price(self):
        owner = _ws(price=4.82, whispersync_price=4.82, owns_kindle=True)
        out = _capture(display_product_detail, owner)
        assert "$4.82 to add narration to your Kindle book" in out
        assert "This is the price shown above" in out

    def test_detail_kindle_owner_price_against_cheaper_credit(self):
        owner = _ws(price=24.0, whispersync_price=24.0, owns_kindle=True)
        out = _capture(display_product_detail, owner, credit_price=9.57)
        assert "No cheaper than a credit" in out

    def test_detail_omits_whispersync_for_other_titles(self):
        out = _capture(display_product_detail, make_product())
        assert "Whispersync" not in out
        assert "Kindle:" not in out

    @pytest.mark.parametrize("width", [160, 90, 50])
    def test_results_flag_only_winning_books(self, width):
        out = _capture(
            display_products,
            [
                _ws(asin="WIN", title="Winner"),
                _ws(asin="LOSE", title="Loser", price=14.99, whispersync_price=16.85),
            ],
            width=width,
        )
        assert out.count("Whispersync") == 1
        assert "$5.60 + Kindle under $15.39" in out

    def test_results_label_kindle_owner_price(self):
        owner = _ws(price=4.82, whispersync_price=4.82, owns_kindle=True)
        out = _capture(display_products, [owner], width=160)
        assert "price for owners of the Kindle book" in out
        table = render_markdown([owner])
        assert "Kindle owner price" in table

    def test_results_show_owned_kindle_book(self):
        out = _capture(display_products, [_ws(owns_kindle=True)], width=160)
        assert "$5.60 with your Kindle book, saves $15.39" in out

    def test_comparison_row(self):
        out = _capture(
            display_comparison,
            [_ws(asin="WIN"), make_product(asin="PLAIN")],
            width=160,
        )
        assert "Whispersync" in out
        assert "$5.60 + Kindle under $15.39" in out

    def test_markdown_links_the_kindle_book(self):
        table = render_markdown([_ws(), make_product(asin="PLAIN")])
        assert (
            "[Kindle](https://www.amazon.com/dp/B0DNNGRN3C) + $5.60 narration" in table
        )
        assert table.count("Kindle") == 1


class TestCli:
    def test_find_whispersync_keeps_winning_books(self, mock_client, tmp_config):
        mock_client.search_pages.return_value = iter(
            [
                (
                    [
                        _ws(asin="WIN"),
                        _ws(asin="LOSE", price=14.99, whispersync_price=16.85),
                        make_product(asin="PLAIN", series_name=""),
                    ],
                    1,
                    3,
                )
            ]
        )
        result = CliRunner().invoke(
            cli,
            ["find", "--pages", "1", "--all-languages", "--max-price", "30"]
            + ["--whispersync", "--json"],
        )
        assert result.exit_code == 0, result.output
        rows = json.loads(result.stdout)
        assert [row["asin"] for row in rows] == ["WIN"]
        assert rows[0]["kindle_url"] == "https://www.amazon.com/dp/B0DNNGRN3C"
        assert load_result_session().current_recipe.whispersync is True

    def test_last_whispersync_refines_cached_results(self, mock_client, tmp_config):
        mock_client.search_pages.return_value = iter(
            [([_ws(asin="WIN"), make_product(asin="PLAIN", series_name="")], 1, 2)]
        )
        runner = CliRunner()
        seeded = runner.invoke(
            cli, ["find", "--pages", "1", "--all-languages", "--max-price", "30", "-q"]
        )
        assert seeded.exit_code == 0, seeded.output

        refined = runner.invoke(cli, ["last", "--whispersync", "--json"])
        assert refined.exit_code == 0, refined.output
        assert [row["asin"] for row in json.loads(refined.stdout)] == ["WIN"]

        cleared = runner.invoke(cli, ["last", "--no-whispersync", "--json"])
        assert cleared.exit_code == 0, cleared.output
        assert len(json.loads(cleared.stdout)) == 2

    def test_profile_saves_and_applies_whispersync(self, mock_client, tmp_config):
        runner = CliRunner()
        saved = runner.invoke(cli, ["profile", "save", "ws", "--whispersync"])
        assert saved.exit_code == 0, saved.output
        assert config_store_mod.load_profiles()["ws"] == {"whispersync": True}

        mock_client.search_pages.return_value = iter(
            [([_ws(asin="WIN"), make_product(asin="PLAIN", series_name="")], 1, 2)]
        )
        result = runner.invoke(
            cli,
            ["find", "--pages", "1", "--all-languages", "--max-price", "30"]
            + ["--profile", "ws", "--json"],
        )
        assert result.exit_code == 0, result.output
        assert [row["asin"] for row in json.loads(result.stdout)] == ["WIN"]

    def test_detail_command_accepts_kindle_price(self, mock_client, tmp_config):
        mock_client.get_product.return_value = _ws(asin="B0F1GDQ6TP")
        result = CliRunner().invoke(
            cli, ["detail", "B0F1GDQ6TP", "--kindle-price", "9.99"]
        )
        assert result.exit_code == 0, result.output
        assert "saves $5.40" in result.output

    def test_detail_rejects_kindle_price_without_whispersync(
        self, mock_client, tmp_config
    ):
        mock_client.get_product.return_value = make_product(asin="B00PLAIN01")
        result = CliRunner().invoke(
            cli, ["detail", "B00PLAIN01", "--kindle-price", "9.99"]
        )
        assert result.exit_code != 0
        assert "no narration add-on" in result.output

    def test_detail_command_shows_whispersync(self, mock_client, tmp_config):
        mock_client.get_product.return_value = _ws(asin="B0F1GDQ6TP")
        result = CliRunner().invoke(cli, ["detail", "B0F1GDQ6TP"])
        assert result.exit_code == 0, result.output
        assert "Whispersync:" in result.output
        assert "amazon.com/dp/B0DNNGRN3C" in result.output


def test_watch_table_flags_wishlist_books():
    from audible_deals.presentation.reports import display_watch_table

    out = _capture(
        display_watch_table,
        [_ws(asin="WIN", title="Winner"), make_product(asin="PLAIN", title="Plain")],
        {"WIN": None, "PLAIN": None},
        width=160,
    )
    assert out.count("Whispersync") == 1
    words = " ".join(out.replace("│", " ").split())
    assert "$5.60 + Kindle under $15.39" in words
