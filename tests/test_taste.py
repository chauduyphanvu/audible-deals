"""Tests for the taste profile, fit scoring, and the for-me command."""

from __future__ import annotations

import datetime
import json

from click.testing import CliRunner

import audible_deals.constants as constants_mod
import audible_deals.wishlist as wishlist_mod
from audible_deals.cli import cli
from audible_deals.taste import (
    build_profile,
    fit_score,
    load_cached_profile,
    rank_by_fit,
    save_profile,
)
from tests.conftest import make_product


def _lib_book(asin, author, narrator="Narrator One", series="", pos="", **kw):
    return make_product(
        asin=asin,
        authors=[author],
        narrators=[narrator],
        series_name=series,
        series_position=pos,
        series_asin="SERIESA01" if series else "",
        **kw,
    )


# ===================================================================
# build_profile
# ===================================================================


class TestBuildProfile:
    def test_top_authors_require_two_books(self):
        lib = [
            _lib_book("L1", "Repeat Author"),
            _lib_book("L2", "Repeat Author"),
            _lib_book("L3", "One Off"),
        ]
        profile = build_profile(lib)
        names = [a["name"] for a in profile["authors"]]
        assert names == ["Repeat Author"]

    def test_single_book_authors_fall_back_to_top_three(self):
        lib = [_lib_book("L1", "A"), _lib_book("L2", "B")]
        profile = build_profile(lib)
        assert len(profile["authors"]) == 2

    def test_series_needs_two_owned(self):
        lib = [
            _lib_book("L1", "A", series="Bobiverse", pos="1"),
            _lib_book("L2", "A", series="Bobiverse", pos="2"),
            _lib_book("L3", "A", series="Solo", pos="1"),
        ]
        profile = build_profile(lib)
        assert [s["name"] for s in profile["series"]] == ["Bobiverse"]
        assert profile["series"][0]["owned"] == 2
        assert profile["series"][0]["series_asin"] == "SERIESA01"

    def test_genres_and_owned_asins(self):
        lib = [_lib_book("L1", "A"), _lib_book("L2", "B")]
        profile = build_profile(lib)
        assert profile["owned_asins"] == ["L1", "L2"]
        genre_ids = [g["id"] for g in profile["genres"]]
        assert "18580606011" in genre_ids
        assert profile["genres"][0]["name"]


# ===================================================================
# Profile cache
# ===================================================================


class TestProfileCache:
    def test_missing_returns_none(self, tmp_config):
        assert load_cached_profile() is None

    def test_fresh_roundtrip(self, tmp_config):
        profile = build_profile([_lib_book("L1", "A")])
        save_profile(profile)
        loaded = load_cached_profile()
        assert loaded is not None
        assert loaded["owned_asins"] == ["L1"]

    def test_stale_returns_none(self, tmp_config):
        profile = build_profile([_lib_book("L1", "A")])
        profile["built_at"] = (
            datetime.datetime.now() - datetime.timedelta(days=2)
        ).isoformat(timespec="seconds")
        save_profile(profile)
        assert load_cached_profile() is None

    def test_corrupt_built_at_returns_none(self, tmp_config):
        constants_mod.TASTE_CACHE_FILE.write_text(json.dumps({"built_at": "soon"}))
        assert load_cached_profile() is None


# ===================================================================
# Fit scoring
# ===================================================================


def _profile():
    return {
        "authors": [{"name": "Fav Author", "count": 4}],
        "narrators": [{"name": "Fav Narrator", "count": 3}],
        "genres": [{"id": "G1", "name": "Science Fiction", "count": 8}],
        "series": [{"name": "Bobiverse", "owned": 3, "series_asin": "SERIESA01"}],
    }


class TestFitScore:
    def test_series_next_dominates(self):
        p = make_product(asin="C1", authors=["X"], narrators=["Y"], category_ids=[])
        points, reasons = fit_score(p, _profile(), {"C1": "Bobiverse"})
        assert points == 5.0
        assert reasons == ["next in Bobiverse"]

    def test_author_narrator_genre_stack(self):
        p = make_product(
            asin="C2",
            authors=["Fav Author"],
            narrators=["Fav Narrator"],
            category_ids=["G1"],
        )
        points, reasons = fit_score(p, _profile(), {})
        assert points == 6.0
        assert "author: Fav Author" in reasons
        assert "narrator: Fav Narrator" in reasons

    def test_genre_only(self):
        p = make_product(asin="C3", authors=["X"], narrators=["Y"], category_ids=["G1"])
        points, reasons = fit_score(p, _profile(), {})
        assert points == 1.0
        assert reasons == ["favorite genre"]

    def test_no_match(self):
        p = make_product(asin="C4", authors=["X"], narrators=["Y"], category_ids=[])
        points, reasons = fit_score(p, _profile(), {})
        assert points == 0.0
        assert reasons == []


class TestRankByFit:
    def test_orders_by_fit_then_value_and_drops_zero(self):
        series_book = make_product(
            asin="R1", authors=["X"], narrators=["Y"], category_ids=[], price=5.0
        )
        author_book = make_product(
            asin="R2", authors=["Fav Author"], narrators=["Y"], category_ids=[]
        )
        genre_cheap = make_product(
            asin="R3", authors=["X"], narrators=["Y"], category_ids=["G1"], price=1.0
        )
        genre_pricey = make_product(
            asin="R4", authors=["X"], narrators=["Y"], category_ids=["G1"], price=20.0
        )
        no_match = make_product(
            asin="R5", authors=["X"], narrators=["Y"], category_ids=[]
        )
        ranked, match = rank_by_fit(
            [genre_pricey, no_match, genre_cheap, author_book, series_book],
            _profile(),
            {"R1": "Bobiverse"},
        )
        assert [p.asin for p in ranked] == ["R1", "R2", "R3", "R4"]
        assert match["R1"] == "next in Bobiverse"
        assert "R5" not in match

    def test_atl_bonus_appended_to_reason(self):
        p = make_product(
            asin="A1", authors=["Fav Author"], narrators=["Y"], category_ids=[]
        )
        ranked, match = rank_by_fit([p], _profile(), {}, atl_asins={"A1"})
        assert ranked == [p]
        assert "all-time low" in match["A1"]

    def test_below_median_bonus_appended_to_reason(self):
        p = make_product(
            asin="B1", authors=["Fav Author"], narrators=["Y"], category_ids=[]
        )
        ranked, match = rank_by_fit([p], _profile(), {}, hist_context={"B1": -15})
        assert ranked == [p]
        assert "below median" in match["B1"]

    def test_atl_takes_precedence_over_below_median(self):
        # When asin is in atl_asins AND hist_context is negative, only ATL fires.
        p = make_product(
            asin="C1", authors=["Fav Author"], narrators=["Y"], category_ids=[]
        )
        ranked, match = rank_by_fit(
            [p], _profile(), {}, atl_asins={"C1"}, hist_context={"C1": -20}
        )
        assert "all-time low" in match["C1"]
        assert "below median" not in match["C1"]

    def test_zero_fit_not_rescued_by_price_signal(self):
        p = make_product(
            asin="Z1", authors=["Unknown"], narrators=["Y"], category_ids=[]
        )
        ranked, match = rank_by_fit(
            [p], _profile(), {}, atl_asins={"Z1"}, hist_context={"Z1": -50}
        )
        assert ranked == []
        assert "Z1" not in match

    def test_price_signal_boosts_ranking(self):
        # ATL item should rank above a higher-fit item when boost pushes score up.
        # author_book: fit=3.0; atl_genre: fit=1.0+1.5=2.5 — author still wins
        # but with a series+atl: fit=5+1.5=6.5 > author 3.0
        series_atl = make_product(
            asin="S1", authors=["X"], narrators=["Y"], category_ids=[], price=5.0
        )
        author_book = make_product(
            asin="A1", authors=["Fav Author"], narrators=["Y"], category_ids=[]
        )
        ranked, match = rank_by_fit(
            [author_book, series_atl],
            _profile(),
            {"S1": "Bobiverse"},
            atl_asins={"S1"},
        )
        assert ranked[0].asin == "S1"
        assert "all-time low" in match["S1"]


# ===================================================================
# for-me command
# ===================================================================


def _seed_profile_cache():
    profile = {
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "library_size": 10,
        "owned_asins": ["B00OWNED01"],
        "authors": [{"name": "Fav Author", "count": 4}],
        "narrators": [{"name": "Fav Narrator", "count": 3}],
        "genres": [{"id": "G1", "name": "Science Fiction", "count": 8}],
        "series": [{"name": "Bobiverse", "owned": 3, "series_asin": "SERIESA01"}],
    }
    constants_mod.TASTE_CACHE_FILE.write_text(json.dumps(profile))
    return profile


def _widen_console():
    """The Match column needs more than the 80-col test default to render."""
    from audible_deals.presentation import terminal as display_mod

    display_mod.console.width = 150


def _json_payload(output: str):
    """Parse the JSON body, skipping the scan progress line above it."""
    return json.loads(output[output.index("[") :])


def _wire_scans(mock_client):
    series_book = make_product(
        asin="B00GAP0004",
        title="Bobiverse 4",
        series_name="Bobiverse",
        series_position="4",
        price=4.99,
        category_ids=["G1"],
        categories=["Science Fiction"],
    )
    owned_book = make_product(asin="B00OWNED01", title="Owned")
    author_book = make_product(
        asin="B00AUTH001",
        title="Zeppelin Book",
        authors=["Fav Author"],
        category_ids=[],
        categories=[],
        series_name="",
        series_position="",
    )
    genre_book = make_product(
        asin="B00GENRE01",
        title="Alpha Book",
        authors=["Someone"],
        category_ids=["G1"],
        categories=["Science Fiction"],
        series_name="",
        series_position="",
    )
    mock_client.get_series_products.return_value = [series_book, owned_book]

    def fake_search_pages(**kwargs):
        if kwargs.get("category_id"):
            return iter([([genre_book], 1, 1)])
        return iter([([author_book], 1, 1)])

    mock_client.search_pages.side_effect = fake_search_pages


class TestForMeCommand:
    def test_ranked_results_with_match_column(self, mock_client, tmp_config):
        _seed_profile_cache()
        _wire_scans(mock_client)
        _widen_console()
        runner = CliRunner()
        result = runner.invoke(cli, ["for-me"])
        assert result.exit_code == 0, result.output
        assert "For you" in result.output
        assert "Match" in result.output
        assert "Bobiverse" in result.output
        assert "B00OWNED01" not in result.output

    def test_json_order_is_fit_ranked(self, mock_client, tmp_config):
        _seed_profile_cache()
        _wire_scans(mock_client)
        runner = CliRunner()
        result = runner.invoke(cli, ["for-me", "--json"])
        assert result.exit_code == 0, result.output
        asins = [d["asin"] for d in _json_payload(result.output)]
        assert asins == ["B00GAP0004", "B00AUTH001", "B00GENRE01"]

    def test_dry_run_makes_no_api_calls(self, mock_client, tmp_config):
        _seed_profile_cache()
        runner = CliRunner()
        result = runner.invoke(cli, ["for-me", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "Bobiverse" in result.output
        assert "Fav Author" in result.output
        mock_client.get_series_products.assert_not_called()
        mock_client.search_pages.assert_not_called()

    def test_builds_profile_from_library_when_no_cache(self, mock_client, tmp_config):
        lib = [
            _lib_book("B00OWNED01", "Fav Author", series="Bobiverse", pos="1"),
            _lib_book("B00OWNED02", "Fav Author", series="Bobiverse", pos="2"),
        ]
        mock_client.get_library_pages.return_value = iter([(lib, 1)])
        _wire_scans(mock_client)
        runner = CliRunner()
        result = runner.invoke(cli, ["for-me"])
        assert result.exit_code == 0, result.output
        assert "Taste profile built from 2 books" in result.output
        assert constants_mod.TASTE_CACHE_FILE.exists()

    def test_empty_library_errors(self, mock_client, tmp_config):
        mock_client.get_library_pages.return_value = iter([])
        runner = CliRunner()
        result = runner.invoke(cli, ["for-me"])
        assert result.exit_code != 0
        assert "library is empty" in result.output.lower()

    def test_wishlist_tag_in_match(self, mock_client, tmp_config):
        _seed_profile_cache()
        _wire_scans(mock_client)
        _widen_console()
        wishlist_mod.save_wishlist(
            [{"asin": "B00GAP0004", "title": "Bobiverse 4", "max_price": 5.0}]
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["for-me"])
        assert result.exit_code == 0, result.output
        assert "wishlisted" in result.output

    def test_max_price_filter_applies(self, mock_client, tmp_config):
        _seed_profile_cache()
        _wire_scans(mock_client)
        runner = CliRunner()
        result = runner.invoke(cli, ["for-me", "--json", "--max-price", "5"])
        assert result.exit_code == 0, result.output
        asins = [d["asin"] for d in _json_payload(result.output)]
        assert asins == ["B00GAP0004"]

    def test_narrow_terminal_folds_match_into_title(self, mock_client, tmp_config):
        # Default test console is 80 cols — the reason should appear inline
        _seed_profile_cache()
        _wire_scans(mock_client)
        runner = CliRunner()
        result = runner.invoke(cli, ["for-me"])
        assert result.exit_code == 0, result.output
        assert "next in" in result.output

    def test_narrator_filter_excludes_non_matching(self, mock_client, tmp_config):
        _seed_profile_cache()
        _wire_scans(mock_client)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["for-me", "--json", "--narrator", "NoSuchNarrator"]
        )
        assert result.exit_code == 0, result.output
        assert _json_payload(result.output) == []

    def test_exclude_author_removes_matching(self, mock_client, tmp_config):
        _seed_profile_cache()
        _wire_scans(mock_client)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["for-me", "--json", "--exclude-author", "Fav Author"]
        )
        assert result.exit_code == 0, result.output
        asins = [d["asin"] for d in _json_payload(result.output)]
        assert "B00AUTH001" not in asins

    def test_skip_plus_and_only_plus_mutual_exclusion(self, mock_client, tmp_config):
        _seed_profile_cache()
        runner = CliRunner()
        result = runner.invoke(cli, ["for-me", "--skip-plus", "--only-plus"])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower()

    def test_sort_reorders_results(self, mock_client, tmp_config):
        _seed_profile_cache()
        _wire_scans(mock_client)
        runner = CliRunner()
        # Fit rank order is: Bobiverse 4, Zeppelin Book, Alpha Book (not alphabetical).
        # --sort title must reorder to alphabetical regardless of fit rank.
        result = runner.invoke(cli, ["for-me", "--json", "--sort", "title"])
        assert result.exit_code == 0, result.output
        titles = [d["title"] for d in _json_payload(result.output)]
        assert titles == ["Alpha Book", "Bobiverse 4", "Zeppelin Book"]

    def test_deprecated_for_you_alias_warns_and_remains_available(
        self, mock_client, tmp_config
    ):
        _seed_profile_cache()
        runner = CliRunner()
        result = runner.invoke(cli, ["for-you", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "Bobiverse" in result.output
        assert result.stderr == (
            "Warning: `deals for-you` is deprecated; use `deals for-me`.\n"
        )

    def test_deprecated_for_you_alias_help_explains_migration(self, tmp_config):
        result = CliRunner().invoke(cli, ["for-you", "--help"])
        assert result.exit_code == 0, result.output
        help_text = " ".join(result.output.split())
        assert "Deprecated: use `deals for-me` instead." in help_text
        assert "will be removed in a future release" in help_text
        assert "\n  Builds a local profile" in result.output
        assert "\n  Examples:" in result.output
