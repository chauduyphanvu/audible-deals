"""Small full-routing smoke checks for the Click command tree."""

from click.testing import CliRunner

from audible_deals.cli import cli
from tests.conftest import make_product


def test_expected_top_level_commands_are_registered():
    expected = {
        "categories",
        "compare",
        "completions",
        "config",
        "detail",
        "doctor",
        "find",
        "for-me",
        "history",
        "import-auth",
        "last",
        "library",
        "login",
        "monitor",
        "notify",
        "open",
        "profile",
        "recap",
        "search",
        "series",
        "track",
        "watch",
        "wishlist",
    }
    assert expected <= set(cli.commands)


def test_root_help_smoke(tmp_config):
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Audible deal finder" in result.output


def test_representative_success_smoke(tmp_config, mock_client):
    mock_client.search_pages.return_value = iter(
        [([make_product(asin="SMOKE1", price=3.99)], 1, 1)]
    )
    result = CliRunner().invoke(
        cli, ["find", "--pages", "1", "--all-languages", "--quiet", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert "SMOKE1" in result.output


def test_representative_failure_smoke(tmp_config):
    result = CliRunner().invoke(cli, ["detail", "not/an/asin"])
    assert result.exit_code != 0
    assert "Invalid ASIN" in result.output
