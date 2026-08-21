from __future__ import annotations

import dataclasses
import datetime
import json
import subprocess
import sys
from io import StringIO

import click
from click.testing import CliRunner
import pytest
from rich.console import Console

from audible_deals import constants
from audible_deals import wishlist as wishlist_store
from audible_deals.cli import cli
from audible_deals.result_processing import (
    process_session_recipe as apply_result_recipe,
    result_recipe,
)
from audible_deals.result_refinement import (
    CachedRefinementRequest,
    FetchBoundPatch,
    FetchBoundRefinementError,
    refine_cached_results,
)
from audible_deals.result_publication import (
    ResultPublicationRequest,
    ResultSessionSpec,
    publish_discovery,
)
from audible_deals.presentation.products import display_products
from audible_deals.results_cache import (
    load_result_session,
    save_result_session,
)
from audible_deals.result_models import (
    DiscoveryResult,
    FilterContext,
    FilterOutcome,
    RecipePatch,
    ResultRecipe,
    ResultSession,
)
from audible_deals.selectors import resolve_selectors
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
        baseline_recipe=recipe,
        current_recipe=recipe,
        visible_asins=visible,
        constraints={"drop_zero_length": True},
    )


def test_result_models_are_frozen_and_normalize_mutable_inputs():
    model_types = (
        ResultRecipe,
        FilterContext,
        FilterOutcome,
        RecipePatch,
        DiscoveryResult,
    )
    assert all(dataclasses.is_dataclass(model) for model in model_types)
    assert all(model.__dataclass_params__.frozen for model in model_types)

    authors = ["Author"]
    recipe = ResultRecipe(exclude_authors=authors)
    authors.append("Other")
    assert recipe.exclude_authors == ("Author",)
    assert recipe.to_dict()["exclude_authors"] == ["Author"]
    assert ResultRecipe.from_mapping(recipe.to_dict()) == recipe
    json.dumps(recipe.to_dict())


def test_recipe_patch_distinguishes_omission_from_explicit_clear():
    recipe = ResultRecipe(max_price=5, language="english")
    omitted = RecipePatch(language="").merge(recipe)
    cleared = RecipePatch(max_price=None).merge(recipe)
    assert omitted.max_price == 5
    assert omitted.language == ""
    assert cleared.max_price is None
    assert cleared.language == "english"


def test_result_session_keeps_v2_recipe_wire_shape(tmp_config):
    product = make_product(asin="WIRE0001", series_name="")
    recipe = ResultRecipe(exclude_authors=("Author",), limit=None)
    save_result_session(_session([product], visible=[product.asin], recipe=recipe))
    cached = json.loads(constants.LAST_RESULTS_FILE.read_text())
    assert cached["version"] == 2
    assert cached["baseline_recipe"]["exclude_authors"] == ["Author"]
    assert cached["baseline_recipe"]["limit"] is None
    assert cached["results"][0]["asin"] == product.asin


def test_session_v2_round_trip():
    product = make_product(asin="ROUND0001", series_name="")
    session = _session(
        [product],
        visible=[product.asin],
        recipe=ResultRecipe(exclude_authors=("Author",), limit=None),
        locale="uk",
    )

    wire = session.to_dict()
    restored = ResultSession.from_dict(wire)

    assert restored.to_dict() == wire
    assert restored.version == 2
    assert restored.baseline_recipe.exclude_authors == ("Author",)


def test_session_v2_round_trip_preserves_unknown_recipe_keys():
    product = make_product(asin="FUTURE001", series_name="")
    wire = _session([product], visible=[product.asin]).to_dict()
    wire["baseline_recipe"]["future_filter"] = {"values": ["one", "two"]}
    wire["current_recipe"]["future_sort"] = "experimental"

    restored = ResultSession.from_dict(wire)

    assert restored.to_dict()["baseline_recipe"]["future_filter"] == {
        "values": ["one", "two"]
    }
    assert restored.to_dict()["current_recipe"]["future_sort"] == "experimental"
    with pytest.raises(ValueError, match="Unknown result recipe field: future_filter"):
        result_recipe(future_filter=True)


@pytest.mark.parametrize(
    ("payload", "title"),
    [
        ([{"asin": "LEGACY001", "locale": "uk"}], "Last results"),
        (
            {
                "title": "Legacy object",
                "results": [{"asin": "LEGACY002", "locale": "de"}],
            },
            "Legacy object",
        ),
    ],
)
def test_legacy_cache_shapes(payload, title):
    session = ResultSession.from_dict(payload)

    assert session.title == title
    assert session.locale in {"uk", "de"}
    assert session.visible_asins == [session.candidates[0]["asin"]]
    assert session.legacy is True
    assert session.to_dict()["version"] == 2


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "producer",
            None,
            "Last results cache is corrupt: producer must be str.",
        ),
        (
            "locale",
            "invalid",
            "Last results cache is corrupt: invalid locale.",
        ),
        (
            "visible_asins",
            [1],
            "Last results cache is corrupt: visible_asins must be strings.",
        ),
    ],
)
def test_corrupt_session_diagnostics_are_unchanged(tmp_config, field, value, message):
    product = make_product(asin="CORRUPT001", series_name="")
    payload = _session([product], visible=[product.asin]).to_dict()
    payload[field] = value
    constants.LAST_RESULTS_FILE.write_text(json.dumps(payload))

    with pytest.raises(click.ClickException) as exc_info:
        load_result_session()

    assert str(exc_info.value) == message


def test_result_cache_missing_and_malformed_diagnostics(tmp_config):
    with pytest.raises(click.ClickException) as missing:
        load_result_session()
    assert str(missing.value) == (
        "No cached results found. Run 'deals find', 'deals search', "
        "'deals for-me', or 'deals series' first."
    )

    constants.LAST_RESULTS_FILE.write_text("{")
    with pytest.raises(click.ClickException, match="Could not read last results cache"):
        load_result_session()


def test_result_cache_io_errors_stay_at_persistence_boundary(tmp_config, monkeypatch):
    class BrokenCache:
        def exists(self):
            return True

        def read_text(self, **kwargs):
            raise OSError("permission denied")

    monkeypatch.setattr(constants, "LAST_RESULTS_FILE", BrokenCache())

    with pytest.raises(click.ClickException) as exc_info:
        load_result_session()

    assert str(exc_info.value) == "Could not read last results cache: permission denied"


def test_apply_recipe_reuses_history_snapshot_and_preserves_ranking(monkeypatch):
    products = [
        make_product(asin="RANK0002", price=4, series_name=""),
        make_product(asin="RANK0001", price=5, series_name=""),
        make_product(asin="RANK0003", price=6, series_name=""),
    ]
    session = _session(
        products,
        visible=[product.asin for product in products],
        recipe=result_recipe(hist_below=50, require_history=True),
    )
    session.constraints["history_percentiles"] = {
        "RANK0002": 10,
        "RANK0001": 20,
        "RANK0003": 30,
    }
    session.ranking_context = {
        "allowed_asins": ["RANK0002", "RANK0001"],
        "fit_scores": {"RANK0002": 9, "RANK0001": 8},
        "match_reasons": {"RANK0002": "best", "RANK0001": "next"},
    }
    monkeypatch.setattr(
        "audible_deals.result_processing.load_price_history",
        lambda *args: pytest.fail("loaded history instead of using snapshot"),
    )

    result = apply_result_recipe(session, session.current_recipe, credit_price=None)

    assert [product.asin for product in result.products] == [
        "RANK0002",
        "RANK0001",
    ]
    assert dict(result.match_reasons or {}) == {
        "RANK0002": "best",
        "RANK0001": "next",
    }
    assert session.ranking_context["fit_scores"] == {
        "RANK0002": 9,
        "RANK0001": 8,
    }


def test_cached_refinement_service_is_api_free_and_does_not_mutate_session():
    product = make_product(asin="PURE0001", price=4, series_name="")
    session = _session(
        [product],
        visible=[product.asin],
        recipe=result_recipe(max_price=5),
    )
    before = session.to_dict()

    outcome = refine_cached_results(
        session,
        CachedRefinementRequest(recipe_patch=RecipePatch(max_price=4)),
    )

    assert outcome.total_count == 1
    assert outcome.session is not session
    assert outcome.session.current_recipe.max_price == 4
    assert session.to_dict() == before


def test_cached_refinement_reset_uses_baseline_before_override():
    products = [
        make_product(asin="RESET001", price=4, series_name=""),
        make_product(asin="RESET002", price=8, series_name=""),
    ]
    session = _session(
        products,
        visible=["RESET001"],
        recipe=result_recipe(max_price=10),
    )
    session.current_recipe = result_recipe(max_price=5)

    outcome = refine_cached_results(
        session,
        CachedRefinementRequest(
            reset=True,
            recipe_patch=RecipePatch(max_price=9),
        ),
    )

    assert outcome.total_count == 2
    assert outcome.session.current_recipe.max_price == 9


def test_cached_refinement_fetch_bound_reports_rerun_command():
    session = _session(
        [make_product(asin="BOUND001", series_name="")],
        visible=["BOUND001"],
    )

    with pytest.raises(FetchBoundRefinementError) as exc_info:
        refine_cached_results(
            session,
            CachedRefinementRequest(fetch_bound_patch=FetchBoundPatch(pages=2)),
        )

    assert str(exc_info.value) == (
        "--pages changes what must be fetched and cannot refine cached results. "
        "Rerun: deals find --pages 1"
    )


def test_last_series_is_local_filter_for_non_series_session(tmp_config):
    products = [
        make_product(asin="LOCALSER01", series_name="Alpha Series"),
        make_product(asin="LOCALSER02", series_name="Beta Series"),
    ]
    save_result_session(
        _session(products, visible=[product.asin for product in products])
    )

    result = CliRunner().invoke(cli, ["last", "--series", "Alpha", "--json"])

    assert result.exit_code == 0, result.output
    assert [item["asin"] for item in json.loads(result.output)] == ["LOCALSER01"]
    assert load_result_session().current_recipe.series == "Alpha"


def test_last_series_is_fetch_bound_for_series_session(tmp_config):
    product = make_product(asin="FETCHSER01", series_name="Alpha Series")
    session = _session([product], visible=[product.asin])
    session.producer = "series"
    session.source = {"command": "deals series --max-series 20"}
    save_result_session(session)

    result = CliRunner().invoke(cli, ["last", "--series", "Alpha", "--json"])

    assert result.exit_code != 0
    assert (
        "--series selects which series must be fetched for this session. "
        "Rerun: deals series --max-series 20"
    ) in result.output


def test_publication_snapshots_histories_before_recording_prices(tmp_config):
    product = make_product(asin="SNAPSHOT01", price=10, series_name="")
    history_file = constants.HISTORY_DIR / f"{product.asin}.json"
    constants.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    history_file.write_text(
        json.dumps(
            {
                "marketplaces": {
                    "us": [
                        {
                            "date": (
                                datetime.date.today() - datetime.timedelta(days=day)
                            ).isoformat(),
                            "price": 5.0,
                            "title": product.title,
                        }
                        for day in range(5, 0, -1)
                    ]
                }
            }
        )
    )

    outcome = publish_discovery(
        ResultPublicationRequest(
            result=DiscoveryResult((product,)),
            title="Snapshot",
            limit=0,
            output=None,
            json_flag=False,
            quiet=True,
            max_price=None,
            currency="$",
            candidates=(product,),
            session_spec=ResultSessionSpec(
                producer="find",
                locale="us",
                recipe=result_recipe(limit=0),
                source={"command": "deals find"},
                constraints={"drop_zero_length": True},
            ),
        )
    )

    assert outcome.session is not None
    assert outcome.session.constraints["history_percentiles"] == {product.asin: 100}
    assert outcome.session.constraints["price_drop_pcts"] == {product.asin: -100.0}
    persisted = load_result_session()
    assert persisted.constraints["history_percentiles"] == {product.asin: 100}
    assert persisted.constraints["price_drop_pcts"] == {product.asin: -100.0}


@pytest.mark.parametrize(
    "module",
    (
        "audible_deals.result_publication",
        "audible_deals.presentation.result_output",
    ),
)
def test_result_modules_import_in_clean_interpreter(module):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


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
    assert load_result_session().current_recipe.exclude_keywords == ("old",)

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
    assert current.max_price == 10
    assert current.language == ""
    assert current.exclude_keywords == ("abridged",)
    assert current.on_sale is True


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
    count = runner.invoke(cli, ["last", "--count"])
    assert count.exit_code == 0, count.output
    assert count.output.strip() == "1"
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
    from audible_deals.presentation import terminal as display

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
    from audible_deals.presentation import terminal as display

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
