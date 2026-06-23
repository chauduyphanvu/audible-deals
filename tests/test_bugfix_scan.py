"""Regression tests for scan-command bug fixes.

Bug 0: a profile-supplied genre must not block an explicit --category override.
Bug 1: `library --json --stats` must emit stats JSON, not the product list.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from audible_deals.cli import cli
from tests.conftest import make_product


def _setup_search_mock(mock_client, products):
    mock_client.search_pages.return_value = iter([(products, 1, len(products))])


def _setup_library_mock(mock_client, products):
    mock_client.get_library_pages.return_value = iter([(products, 1)])


# ---------------------------------------------------------------------------
# Bug 0: profile genre + explicit --category override
# ---------------------------------------------------------------------------


class TestProfileGenreCategoryOverride:
    def test_find_profile_genre_with_category_override(self, tmp_config, mock_client):
        """--category overrides a profile's genre instead of erroring out."""
        (tmp_config / "profiles.json").write_text(
            json.dumps({"scifi": {"genre": "sci-fi", "max_price": 5.0}})
        )
        _setup_search_mock(mock_client, [make_product(asin="OV1", price=2.99)])
        mock_client.get_category_name.return_value = "Fantasy"
        result = CliRunner().invoke(
            cli,
            ["find", "--profile", "scifi", "--category", "18580606011"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        # The explicit category was resolved; the profile genre did not win.
        mock_client.get_category_name.assert_called_once_with("18580606011")
        mock_client.resolve_genre.assert_not_called()

    def test_search_profile_genre_with_category_override(self, tmp_config, mock_client):
        (tmp_config / "profiles.json").write_text(
            json.dumps({"scifi": {"genre": "sci-fi"}})
        )
        _setup_search_mock(mock_client, [make_product(asin="OV2", price=2.99)])
        mock_client.get_category_name.return_value = "Fantasy"
        result = CliRunner().invoke(
            cli,
            ["search", "robots", "--profile", "scifi", "--category", "18580606011"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        mock_client.get_category_name.assert_called_once_with("18580606011")
        mock_client.resolve_genre.assert_not_called()

    def test_find_cli_genre_with_category_still_conflicts(
        self, tmp_config, mock_client
    ):
        """An explicit --genre on the CLI plus --category still errors out."""
        result = CliRunner().invoke(
            cli, ["find", "--genre", "sci-fi", "--category", "cat1"]
        )
        assert result.exit_code != 0
        assert "not both" in result.output

    def test_search_cli_genre_with_category_still_conflicts(
        self, tmp_config, mock_client
    ):
        result = CliRunner().invoke(
            cli, ["search", "test", "--genre", "sci-fi", "--category", "cat1"]
        )
        assert result.exit_code != 0
        assert "not both" in result.output


# ---------------------------------------------------------------------------
# Bug 1: library --json --stats emits stats, not the product list
# ---------------------------------------------------------------------------


class TestLibraryJsonStats:
    def test_library_json_stats_emits_stats_object(self, tmp_config, mock_client):
        products = [
            make_product(
                asin="JS1",
                authors=["Jane Author"],
                narrators=["Nick Narrator"],
                categories=["Science Fiction"],
                length_minutes=600,
                rating=4.5,
            ),
            make_product(
                asin="JS2",
                authors=["Jane Author"],
                narrators=["Nick Narrator"],
                categories=["Science Fiction"],
                length_minutes=300,
                rating=4.0,
            ),
        ]
        _setup_library_mock(mock_client, products)
        result = CliRunner().invoke(
            cli, ["library", "--json", "--stats"], catch_exceptions=False
        )
        assert result.exit_code == 0
        payload = json.loads(result.output[result.output.index("{") :])
        # Stats object, not a product array.
        assert isinstance(payload, dict)
        assert payload["total_books"] == 2
        assert payload["total_hours"] == 15.0
        assert payload["top_authors"][0]["name"] == "Jane Author"
        assert payload["top_authors"][0]["count"] == 2

    def test_library_json_without_stats_still_emits_products(
        self, tmp_config, mock_client
    ):
        """--json alone keeps emitting the product list (no regression)."""
        products = [make_product(asin="JS3"), make_product(asin="JS4")]
        _setup_library_mock(mock_client, products)
        result = CliRunner().invoke(cli, ["library", "--json"], catch_exceptions=False)
        assert result.exit_code == 0
        payload = json.loads(result.output[result.output.index("[") :])
        assert isinstance(payload, list)
        assert {p["asin"] for p in payload} == {"JS3", "JS4"}
