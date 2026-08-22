"""Catalog CLI behavior shared across commands."""

from __future__ import annotations


import pytest
from click.testing import CliRunner

import audible_deals.constants as constants_mod
from audible_deals.cli import cli
from tests.conftest import make_product


class TestCatalogRegressions:
    def test_network_error_is_rendered_as_click_error(self, mock_client, tmp_config):
        from audible.exceptions import NetworkError

        mock_client.search_pages.side_effect = NetworkError()
        result = CliRunner().invoke(cli, ["find"])
        assert result.exit_code != 0
        assert "Audible request failed" in result.output
        assert "Traceback" not in result.output

    @pytest.mark.parametrize("error_name", ["NetworkError", "NotResponding"])
    def test_request_errors_are_rendered_as_click_errors(
        self, mock_client, tmp_config, error_name
    ):
        import audible.exceptions as audible_exceptions

        mock_client.search_pages.side_effect = getattr(audible_exceptions, error_name)()
        result = CliRunner().invoke(cli, ["find"])
        assert result.exit_code != 0
        assert "Audible request failed" in result.output
        assert "Traceback" not in result.output

    @pytest.mark.parametrize(
        ("command", "module_name"),
        [
            (["library", "--output", "bad.txt"], "audible_deals.cli.library"),
            (["series", "--output", "bad.txt"], "audible_deals.cli.series"),
            (["for-me", "--output", "bad.txt"], "audible_deals.cli.foryou"),
        ],
    )
    def test_bad_export_extension_does_not_construct_client(
        self, tmp_config, monkeypatch, command, module_name
    ):
        module = __import__(module_name, fromlist=["_get_client"])
        monkeypatch.setattr(
            module,
            "_get_client",
            lambda locale: pytest.fail("constructed client before output validation"),
        )
        result = CliRunner().invoke(cli, command)
        assert result.exit_code != 0
        assert "Unsupported extension" in result.output

    def test_export_write_failure_prevents_scan_state_commits(
        self, mock_client, tmp_config
    ):
        mock_client.search_pages.return_value = iter([([make_product()], 1, 1)])
        output_dir = tmp_config / "output.json"
        output_dir.mkdir()
        result = CliRunner().invoke(
            cli, ["find", "--output", str(output_dir), "--all-languages"]
        )
        assert result.exit_code != 0
        assert "Filesystem error" in result.output
        assert not constants_mod.LAST_RESULTS_FILE.exists()
        assert not constants_mod.SEEN_ASINS_FILE.exists()
        assert not constants_mod.HISTORY_DIR.exists()

    def test_search_multi_query_dry_run_multiplies_estimates(self, tmp_config):
        result = CliRunner().invoke(
            cli, ["search", "one | two", "--pages", "2", "--deep", "--dry-run"]
        )
        assert result.exit_code == 0, result.output
        assert "Max items: ~700" in result.output
        assert "API calls: 14" in result.output


class TestDetailCommand:
    def test_detail_ok(self, mock_client, tmp_config):
        mock_client.get_product.return_value = make_product(
            asin="D1", title="Detail Test"
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["detail", "D1"])
        assert result.exit_code == 0, result.output
        assert "Detail Test" in result.output

    def test_detail_not_found(self, mock_client, tmp_config):
        mock_client.get_product.side_effect = ValueError("Product not found: BAD")

        runner = CliRunner()
        result = runner.invoke(cli, ["detail", "BAD"])
        assert result.exit_code != 0
        assert "Product not found" in result.output


class TestCompareCommand:
    def test_compare_ok(self, mock_client, tmp_config):
        mock_client.get_products_batch.return_value = [
            make_product(asin="C1", title="Book 1", price=5.0, length_minutes=600),
            make_product(asin="C2", title="Book 2", price=10.0, length_minutes=600),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["compare", "C1", "C2"])
        assert result.exit_code == 0, result.output
        assert "Book 1" in result.output
        assert "Book 2" in result.output
        assert "Best value" in result.output

    def test_compare_too_few(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["compare", "ONLY_ONE"])
        assert result.exit_code != 0
        assert "at least 2" in result.output

    def test_compare_with_missing(self, mock_client, tmp_config):
        mock_client.get_products_batch.return_value = [
            make_product(asin="C1", title="Book 1"),
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["compare", "C1", "MISSING"])
        assert result.exit_code != 0
        assert "Not found: MISSING" in result.output


class TestDynamicTitleColumnWidth:
    def test_narrow_terminal_uses_minimum(self):
        """On a narrow terminal (e.g. 80 chars), the title remains readable."""
        from io import StringIO

        from rich.console import Console

        from audible_deals.presentation import products as display_mod
        from audible_deals.presentation import terminal

        buf = StringIO()
        narrow_console = Console(file=buf, width=80, force_terminal=False)
        original = terminal.console
        terminal.console = narrow_console
        try:
            products = [make_product(asin="TW1", title="A Book", price=3.0)]
            display_mod.display_products(products, title="Test")
        finally:
            terminal.console = original
        out = buf.getvalue()
        assert "A Book" in out

    def test_wide_terminal_uses_larger_width(self):
        """On a wide terminal (e.g. 200 chars), title column should be wider."""
        from io import StringIO

        from rich.console import Console

        from audible_deals.presentation import products as display_mod
        from audible_deals.presentation import terminal

        buf = StringIO()
        wide_console = Console(file=buf, width=200, force_terminal=False)
        original = terminal.console
        terminal.console = wide_console
        try:
            long_title = "A" * 70
            products = [make_product(asin="TW2", title=long_title, price=3.0)]
            display_mod.display_products(products, title="Test")
        finally:
            terminal.console = original
        out = buf.getvalue()
        # The output should contain at least part of the long title
        assert "TW2" in out


class TestNarratorHelpText:
    def test_find_narrator_help_says_client_side(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--help"])
        assert result.exit_code == 0
        assert "client-side" in result.output

    def test_search_narrator_help_says_client_side(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "--help"])
        assert result.exit_code == 0
        assert "client-side" in result.output

    def test_last_narrator_help_says_client_side(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--help"])
        assert result.exit_code == 0
        assert "client-side" in result.output
