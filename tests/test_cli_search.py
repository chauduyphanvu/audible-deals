"""Tests for the catalog search command."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from audible_deals.cli import cli
from tests.conftest import make_product


class TestSearchCommand:
    def test_search_basic(self, mock_client, tmp_config):
        products = [make_product(asin="S1", price=5.0)]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])

        out_file = tmp_config / "search.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "test query",
                "--pages",
                "1",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 1
        assert data[0]["asin"] == "S1"

    def test_json_stdout_is_not_prefixed_by_progress(self, mock_client, tmp_config):
        product = make_product(asin="SJSON", price=5.0, language="english")
        mock_client.search_pages.return_value = iter([([product], 1, 1)])
        output = tmp_config / "search-json-output.json"

        result = CliRunner().invoke(
            cli,
            [
                "search",
                "test",
                "--pages",
                "1",
                "--json",
                "--output",
                str(output),
            ],
        )

        assert result.exit_code == 0, result.output
        assert [item["asin"] for item in json.loads(result.stdout)] == ["SJSON"]
        assert "Exported 1 items" in result.stderr
        assert json.loads(output.read_text())[0]["asin"] == "SJSON"

    def test_output_implies_quiet(self, mock_client, tmp_config):
        """When -o is set without -q, quiet should be implied (no table in stdout)."""
        products = [make_product(asin="S2", price=5.0)]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        out_file = tmp_config / "search_implied.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "test",
                "--pages",
                "1",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        # Table should not appear; export message should appear
        assert 'Search: "test"' not in result.output
        assert "Exported" in result.output

    def test_output_with_explicit_quiet(self, mock_client, tmp_config):
        """Explicit -q with -o also suppresses table."""
        products = [make_product(asin="S3", price=5.0)]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        out_file = tmp_config / "search_explicit.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "test",
                "--pages",
                "1",
                "--output",
                str(out_file),
                "-q",
            ],
        )
        assert result.exit_code == 0, result.output
        assert 'Search: "test"' not in result.output


class TestSearchDeepFlag:
    def test_search_deep_flag_exists(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "--help"])
        assert result.exit_code == 0
        assert "--deep" in result.output

    def test_search_deep_deduplicates(self, mock_client, tmp_config):
        """search --deep fetches 3 sort orders and deduplicates."""
        pass1 = [
            make_product(asin="SD1", price=3.0, series_name="", series_position=""),
            make_product(asin="SD2", price=4.0, series_name="", series_position=""),
        ]
        pass2 = [
            make_product(asin="SD2", price=4.0, series_name="", series_position=""),
            make_product(asin="SD3", price=5.0, series_name="", series_position=""),
        ]
        pass3 = [
            make_product(asin="SD1", price=3.0, series_name="", series_position=""),
            make_product(asin="SD4", price=2.0, series_name="", series_position=""),
        ]

        call_count = 0

        def fake_search_pages(**kwargs):
            nonlocal call_count
            if kwargs.get("title"):
                yield [], 1, 0
                return
            data = [pass1, pass2, pass3][call_count]
            call_count += 1
            yield data, 1, len(data)

        mock_client.search_pages.side_effect = fake_search_pages
        out_file = tmp_config / "search_deep.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "test",
                "--deep",
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
        asins = sorted(d["asin"] for d in data)
        assert asins == ["SD1", "SD2", "SD3", "SD4"]


class TestSearchQueryOptional:
    def test_search_no_query_no_genre_raises(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["search"])
        assert result.exit_code != 0

    def test_search_with_genre_no_query(self, mock_client, tmp_config):
        products = [
            make_product(asin="SG1", price=5.0, series_name="", series_position="")
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        mock_client.resolve_genre.return_value = ("cat99", "Mystery")
        out_file = tmp_config / "search_genre.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "--genre",
                "mystery",
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
        assert len(data) == 1

    def test_search_with_category_no_query(self, mock_client, tmp_config):
        products = [
            make_product(asin="SC1", price=5.0, series_name="", series_position="")
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        mock_client.get_category_name.return_value = "Thriller"
        out_file = tmp_config / "search_cat.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "--category",
                "123456",
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
        assert len(data) == 1

    def test_search_with_query_still_works(self, mock_client, tmp_config):
        products = [
            make_product(asin="SQ1", price=5.0, series_name="", series_position="")
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        out_file = tmp_config / "search_q.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "test query",
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
        assert len(data) == 1


class TestSearchDefaultLimit:
    def test_search_default_limit_is_25(self):
        """search --help shows default 25 in help text."""
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "--help"])
        assert result.exit_code == 0
        assert "25" in result.output

    def test_search_returns_25_by_default(self, mock_client, tmp_config):
        """search without -n returns at most 25 results."""
        products = [
            make_product(
                asin=f"SQ{i:02d}", price=float(i), series_name="", series_position=""
            )
            for i in range(1, 41)
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 40)])
        out_file = tmp_config / "search_def.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "test",
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
        assert len(data) == 25


class TestSearchAuthorHint:
    def test_hint_shown_for_exact_fetched_author(self, mock_client, tmp_config):
        """search shows --author tip when fetched data confirms the author."""
        products = [
            make_product(
                asin="AH1",
                price=5.0,
                authors=["Andy Weir"],
                series_name="",
                series_position="",
            )
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "Andy Weir",
                "--pages",
                "1",
                "--all-languages",
                "-n",
                "0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "--author" in result.output
        assert "Andy Weir" in result.output

    def test_hint_not_shown_without_author_evidence(self, mock_client, tmp_config):
        products = [
            make_product(
                asin="AHNO1",
                title="Andy Weir: A Biography",
                authors=["Someone Else"],
                series_name="",
                series_position="",
            )
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])

        result = CliRunner().invoke(
            cli,
            [
                "search",
                "Andy Weir",
                "--pages",
                "1",
                "--all-languages",
                "-n",
                "0",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Tip:" not in result.output

    def test_hint_not_shown_when_author_already_set(self, mock_client, tmp_config):
        """search does NOT show tip when --author is already used."""
        products = [
            make_product(asin="AH2", price=5.0, series_name="", series_position="")
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "Andy Weir",
                "--author",
                "Andy Weir",
                "--pages",
                "1",
                "--all-languages",
                "-n",
                "0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Tip:" not in result.output

    def test_hint_not_shown_for_non_name_query(self, mock_client, tmp_config):
        """search does NOT show tip for a single-word query."""
        products = [
            make_product(asin="AH3", price=5.0, series_name="", series_position="")
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "Dune",
                "--pages",
                "1",
                "--all-languages",
                "-n",
                "0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Tip:" not in result.output

    def test_hint_not_shown_with_quiet(self, mock_client, tmp_config):
        """search does NOT show tip in quiet mode."""
        products = [
            make_product(asin="AH4", price=5.0, series_name="", series_position="")
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "Andy Weir",
                "--pages",
                "1",
                "--all-languages",
                "-q",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Tip:" not in result.output


class TestDryRunSearch:
    def test_search_dry_run_shows_summary(self, mock_client, tmp_config):
        """search --dry-run prints scan summary and does not call search_pages."""
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "fantasy", "--dry-run", "--pages", "3"])
        assert result.exit_code == 0, result.output
        assert "Dry run" in result.output
        assert "Query: fantasy" in result.output
        assert "Sort orders" in result.output
        assert "API calls" in result.output
        mock_client.search_pages.assert_not_called()

    def test_search_dry_run_does_not_call_catalog(self, mock_client, tmp_config):
        """search --dry-run does not call search_catalog."""
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "test", "--dry-run"])
        assert result.exit_code == 0, result.output
        mock_client.search_catalog.assert_not_called()

    def test_search_dry_run_json_is_machine_readable(self, mock_client, tmp_config):
        result = CliRunner().invoke(
            cli, ["search", "fantasy", "--dry-run", "--json", "--pages", "2"]
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["dry_run"] is True
        assert payload["query"] == "fantasy"
        assert payload["api_calls"] == 3

    def test_search_dry_run_rejects_empty_or_query_before_planning(
        self, tmp_config, monkeypatch
    ):
        import audible_deals.cli.catalog as catalog_mod

        monkeypatch.setattr(
            catalog_mod,
            "_get_client",
            lambda locale: pytest.fail("dry run constructed a client"),
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["search", " | ", "--dry-run"])
        assert result.exit_code != 0
        assert "No keywords found" in result.output

    def test_find_dry_run_subcategories_marks_live_count_unknown(
        self, tmp_config, monkeypatch
    ):
        import audible_deals.cli.catalog as catalog_mod

        monkeypatch.setattr(
            catalog_mod,
            "_get_client",
            lambda locale: pytest.fail("dry run constructed a client"),
        )
        runner = CliRunner()
        result = runner.invoke(
            cli, ["find", "--genre", "fantasy", "--subcategories", "--dry-run"]
        )
        assert result.exit_code == 0, result.output
        assert "Subcategories: unknown" in result.output
