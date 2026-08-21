from __future__ import annotations

import json
from io import StringIO

import click
from click.testing import CliRunner
import pytest
from rich.console import Console

from audible_deals import constants
from audible_deals import wishlist as wishlist_store
from audible_deals.cli import cli
from audible_deals.cli.pipeline import result_recipe
from audible_deals.display import display_products
from audible_deals.results_cache import (
    ResultSession,
    load_result_session,
    resolve_selectors,
    save_result_session,
)
from audible_deals.serialization import serialize_product
from tests.conftest import make_product


def _session(products, *, visible, recipe=None, locale="us"):
    recipe = recipe or result_recipe(limit=0)
    return ResultSession(
        producer="find",
        locale=locale,
        title="Cached deals",
        source={"command": "deals find --pages 1", "pages": 1},
        candidates=[serialize_product(product) for product in products],
        baseline_recipe=recipe.copy(),
        current_recipe=recipe.copy(),
        visible_asins=visible,
        constraints={"drop_zero_length": True},
    )


def test_last_widens_cumulatively_and_reset_restores_baseline(tmp_config):
    products = [
        make_product(asin="CACHE1", title="Cheap", price=3, series_name=""),
        make_product(asin="CACHE2", title="Expensive", price=8, series_name=""),
    ]
    recipe = result_recipe(max_price=5, limit=1, sort="price")
    save_result_session(_session(products, visible=["CACHE1"], recipe=recipe))
    runner = CliRunner()

    widened = runner.invoke(cli, ["last", "--max-price", "10", "-n", "0", "--json"])
    assert widened.exit_code == 0, widened.output
    assert [item["asin"] for item in json.loads(widened.output)] == [
        "CACHE1",
        "CACHE2",
    ]

    inherited = runner.invoke(cli, ["last", "--json"])
    assert inherited.exit_code == 0, inherited.output
    assert len(json.loads(inherited.output)) == 2

    reset = runner.invoke(cli, ["last", "--reset", "--json"])
    assert reset.exit_code == 0, reset.output
    assert [item["asin"] for item in json.loads(reset.output)] == ["CACHE1"]


def test_find_caches_pre_filter_candidates_for_api_free_widening(
    tmp_config, mock_client
):
    products = [
        make_product(asin="FETCHED01", price=3, series_name="", language="english"),
        make_product(asin="FETCHED02", price=8, series_name="", language="english"),
    ]
    mock_client.search_pages.return_value = iter([(products, 1, 2)])
    runner = CliRunner()
    initial = runner.invoke(
        cli,
        ["find", "--max-price", "5", "--pages", "1", "--all-languages", "-q"],
    )
    assert initial.exit_code == 0, initial.output
    session = load_result_session()
    assert len(session.candidates) == 2
    assert session.visible_asins == ["FETCHED01"]
    calls = mock_client.search_pages.call_count

    widened = runner.invoke(cli, ["last", "--max-price", "10", "--json"])
    assert widened.exit_code == 0, widened.output
    assert {item["asin"] for item in json.loads(widened.output)} == {
        "FETCHED01",
        "FETCHED02",
    }
    assert mock_client.search_pages.call_count == calls


def test_reset_then_clear_then_override_and_repeatable_replacement(tmp_config):
    products = [
        make_product(asin="RECIPE01", price=3, language="english", title="Old keyword"),
        make_product(asin="RECIPE02", price=9, language="french", title="Other"),
    ]
    baseline = result_recipe(
        max_price=5,
        language="english",
        exclude_keywords=["abridged"],
        on_sale=True,
    )
    save_result_session(_session(products, visible=[], recipe=baseline))
    runner = CliRunner()
    changed = runner.invoke(
        cli,
        [
            "last",
            "--max-price",
            "8",
            "--exclude-keyword",
            "old",
            "--no-on-sale",
            "--json",
        ],
    )
    assert changed.exit_code == 0, changed.output
    assert load_result_session().current_recipe["exclude_keywords"] == ["old"]

    reset = runner.invoke(
        cli,
        [
            "last",
            "--reset",
            "--clear-filter",
            "language",
            "--max-price",
            "10",
            "--json",
        ],
    )
    assert reset.exit_code == 0, reset.output
    current = load_result_session().current_recipe
    assert current["max_price"] == 10
    assert current["language"] == ""
    assert current["exclude_keywords"] == ["abridged"]
    assert current["on_sale"] is True


def test_count_ignores_limit_and_does_not_change_last_displayed_order(tmp_config):
    products = [
        make_product(asin="COUNT001", series_name=""),
        make_product(asin="COUNT002", series_name=""),
    ]
    save_result_session(
        _session(products, visible=["COUNT001"], recipe=result_recipe(limit=1))
    )
    result = CliRunner().invoke(cli, ["last", "--count"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "2"
    assert load_result_session().visible_asins == ["COUNT001"]


def test_failed_last_export_leaves_session_unchanged(tmp_config):
    product = make_product(asin="ROLLBACK1", series_name="")
    save_result_session(_session([product], visible=[product.asin]))
    before = constants.LAST_RESULTS_FILE.read_text()
    output = tmp_config / "blocked.json"
    output.mkdir()
    result = CliRunner().invoke(
        cli, ["last", "--max-price", "1", "--output", str(output)]
    )
    assert result.exit_code != 0
    assert constants.LAST_RESULTS_FILE.read_text() == before


def test_conflicting_inferred_locales_error_but_explicit_locale_wins(tmp_config):
    product = make_product(asin="B00LOCALE1", locale="us", series_name="")
    save_result_session(_session([product], visible=[product.asin]))
    uk_url = "https://www.audible.co.uk/pd/example/B00LOCALE2"
    with pytest.raises(click.ClickException, match="conflicting Audible marketplaces"):
        resolve_selectors(("@1", uk_url))
    resolved, locale = resolve_selectors(("@1", uk_url), explicit_locale="de")
    assert [item.asin for item in resolved] == ["B00LOCALE1", "B00LOCALE2"]
    assert locale == "de"


def test_selectors_are_limited_to_visible_rows_and_ranges_deduplicate(
    tmp_config, mock_client
):
    products = [
        make_product(asin="B00CACHE01", title="One", series_name=""),
        make_product(asin="B00CACHE02", title="Two", series_name=""),
        make_product(asin="B00HIDDEN3", title="Hidden", series_name=""),
    ]
    save_result_session(_session(products, visible=["B00CACHE01", "B00CACHE02"]))
    mock_client.get_products_batch.return_value = products[:2]

    result = CliRunner().invoke(cli, ["compare", "@1-2,1"])
    assert result.exit_code == 0, result.output
    mock_client.get_products_batch.assert_called_once_with(["B00CACHE01", "B00CACHE02"])

    hidden = CliRunner().invoke(cli, ["detail", "@3"])
    assert hidden.exit_code != 0
    assert "current view has 2" in hidden.output


def test_audible_url_infers_locale_unless_explicit(tmp_config, monkeypatch):
    import audible_deals.cli.misc as misc

    locales = []
    product = make_product(asin="B00EXAMPLE")

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_product(self, asin):
            assert asin == "B00EXAMPLE"
            return product

    monkeypatch.setattr(
        misc, "_get_client", lambda locale: locales.append(locale) or Client()
    )
    url = "https://www.audible.co.uk/pd/example/B00EXAMPLE"
    inferred = CliRunner().invoke(cli, ["detail", url])
    explicit = CliRunner().invoke(cli, ["--locale", "de", "detail", url])
    assert inferred.exit_code == 0, inferred.output
    assert explicit.exit_code == 0, explicit.output
    assert locales == ["uk", "de"]


def test_audible_apex_url_and_lowercase_asin_are_canonicalized(tmp_config):
    url = "https://audible.co.uk/pd/example/B00EXAMPLE"
    resolved, locale = resolve_selectors((url, "b00example"))
    assert [item.asin for item in resolved] == ["B00EXAMPLE"]
    assert locale == "uk"


def test_malformed_audible_url_has_a_click_diagnostic(tmp_config):
    result = CliRunner().invoke(cli, ["detail", "https://[bad/pd/B00EXAMPLE"])
    assert result.exit_code != 0
    assert "Invalid Audible URL" in result.output


def test_candidate_locale_is_validated(tmp_config):
    product = make_product(asin="B00BADLOC1", series_name="")
    session = _session([product], visible=[product.asin])
    session.candidates[0]["locale"] = "invalid"
    constants.LAST_RESULTS_FILE.write_text(json.dumps(session.to_dict()))
    with pytest.raises(click.ClickException, match="candidate locale is invalid"):
        load_result_session()


def test_legacy_refinement_persists_cumulatively(tmp_config):
    products = [
        make_product(asin="LEGACY001", price=3, series_name=""),
        make_product(asin="LEGACY002", price=8, series_name=""),
    ]
    constants.LAST_RESULTS_FILE.write_text(
        json.dumps(
            {
                "title": "Legacy results",
                "results": [serialize_product(product) for product in products],
            }
        )
    )
    runner = CliRunner()
    narrowed = runner.invoke(cli, ["last", "--max-price", "5", "--json"])
    assert narrowed.exit_code == 0, narrowed.output
    inherited = runner.invoke(cli, ["last", "--json"])
    assert inherited.exit_code == 0, inherited.output
    assert [item["asin"] for item in json.loads(inherited.output)] == ["LEGACY001"]
    assert load_result_session().legacy is True


def test_last_uses_frozen_credit_price(tmp_config):
    product = make_product(asin="CREDIT001", price=15, series_name="")
    recipe = result_recipe(max_effective_price=10)
    session = _session([product], visible=[product.asin], recipe=recipe, locale="uk")
    session.constraints["credit_price"] = 8.0
    save_result_session(session)
    constants.CONFIG_FILE.write_text(json.dumps({"credit_price": 20.0, "locale": "de"}))

    result = CliRunner().invoke(cli, ["last", "--json"])
    assert result.exit_code == 0, result.output
    assert [item["asin"] for item in json.loads(result.output)] == ["CREDIT001"]


def test_session_history_refinement_uses_scan_snapshot(tmp_config, mock_client):
    product = make_product(
        asin="HISTORY001",
        price=10,
        language="english",
        series_name="",
    )
    constants.HISTORY_DIR.mkdir()
    constants.HISTORY_DIR.joinpath(f"{product.asin}.json").write_text(
        json.dumps(
            {
                "marketplaces": {
                    "us": [
                        {"date": f"2026-08-{day:02d}", "price": 5.0}
                        for day in range(1, 6)
                    ]
                }
            }
        )
    )
    mock_client.search_pages.return_value = iter([([product], 1, 1)])
    initial = CliRunner().invoke(cli, ["find", "--pages", "1", "--all-languages", "-q"])
    assert initial.exit_code == 0, initial.output
    assert load_result_session().constraints["history_percentiles"] == {
        product.asin: 100
    }

    refined = CliRunner().invoke(
        cli,
        ["last", "--hist-below", "90", "--require-history", "--json"],
    )
    assert refined.exit_code == 0, refined.output
    assert json.loads(refined.output) == []


def test_wishlist_keeps_inferred_locale_for_later_watch(tmp_config, monkeypatch):
    import audible_deals.cli.wishlist as wishlist_cli

    locales = []

    class Client:
        def __init__(self, locale):
            self.locale = locale

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_product(self, asin):
            return make_product(asin=asin, locale=self.locale, series_name="")

        def get_products_batch(self, asins):
            locales.append(self.locale)
            return [
                make_product(asin=asin, locale=self.locale, series_name="")
                for asin in asins
            ]

    monkeypatch.setattr(wishlist_cli, "_get_client", Client)
    runner = CliRunner()
    added = runner.invoke(
        cli,
        [
            "wishlist",
            "add",
            "https://www.audible.co.uk/pd/example/B00WISH001",
        ],
    )
    assert added.exit_code == 0, added.output
    assert wishlist_store.load_wishlist()[0]["locale"] == "uk"

    watched = runner.invoke(cli, ["watch"])
    assert watched.exit_code == 0, watched.output
    assert locales == ["uk"]


def test_notify_fetches_wishlist_entries_from_their_saved_locales(
    tmp_config, monkeypatch
):
    import audible_deals.cli.notify as notify_cli

    wishlist_store.save_wishlist(
        [
            {"asin": "B00NOTIFY1", "title": "UK", "max_price": 1, "locale": "uk"},
            {"asin": "B00NOTIFY2", "title": "DE", "max_price": 1, "locale": "de"},
        ]
    )
    calls = []

    class Client:
        def __init__(self, locale):
            self.locale = locale

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_products_batch(self, asins):
            calls.append((self.locale, list(asins)))
            return [
                make_product(asin=asin, locale=self.locale, price=10) for asin in asins
            ]

    monkeypatch.setattr(notify_cli, "_get_client", Client)
    result = CliRunner().invoke(cli, ["notify"])
    assert result.exit_code == 0, result.output
    assert calls == [("uk", ["B00NOTIFY1"]), ("de", ["B00NOTIFY2"])]


def test_product_renderer_uses_cards_below_80_without_losing_values(monkeypatch):
    import audible_deals.display as display

    output = StringIO()
    monkeypatch.setattr(
        display, "console", Console(file=output, width=60, force_terminal=False)
    )
    product = make_product(
        asin="B00CARD001",
        title="A very long title that should wrap fully on a narrow terminal",
        price=20.6,
        list_price=103.0,
        num_ratings=18_432,
        series_name="Long Series",
    )
    display_products(
        [product],
        credit_price=11.25,
        hist_context={product.asin: -20},
        match_context={product.asin: "favorite narrator and exact author"},
    )
    rendered = output.getvalue()
    assert "@1" in rendered
    assert "$20.60" in rendered
    assert "18,432" in rendered
    assert "credit" in rendered
    assert "-20%" in rendered
    assert "favorite narrator" in rendered
    assert all(len(line) <= 60 for line in rendered.splitlines())


@pytest.mark.parametrize("width", [60, 79, 80, 100, 119, 120, 160])
def test_product_renderer_preserves_signals_at_layout_boundaries(monkeypatch, width):
    import audible_deals.display as display

    output = StringIO()
    monkeypatch.setattr(
        display, "console", Console(file=output, width=width, force_terminal=False)
    )
    product = make_product(
        asin="B00BOUND01",
        title="Boundary title " * 8,
        price=20.6,
        list_price=103.0,
        length_minutes=1_236,
        num_ratings=18_432,
        series_name="Boundary Series",
    )
    display_products(
        [product],
        show_url=True,
        credit_price=11.25,
        hist_context={product.asin: -20},
        match_context={product.asin: "exact author and narrator match"},
    )
    rendered = output.getvalue()
    for expected in (
        "@1",
        "$20.60",
        "20.6",
        "18,432",
        "credit",
        "-20%",
        "B00BOUND01",
        "exact author",
        product.url,
    ):
        assert expected in rendered
    assert all(len(line) <= width for line in rendered.splitlines())
