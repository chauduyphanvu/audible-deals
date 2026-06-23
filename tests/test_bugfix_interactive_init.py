"""Regression tests for interactive_init bug batch (bugs 34, 35)."""

from __future__ import annotations

import click
from click.testing import CliRunner

from audible_deals.cli import cli
import audible_deals.wishlist as wishlist_mod
from tests.conftest import make_product


def _run_browse(products, user_input, tmp_config):
    """Drive _interactive_browse via a thin Click wrapper with simulated input."""
    from audible_deals.cli.interactive import _interactive_browse

    @click.command()
    def _cmd():
        _interactive_browse(products)

    runner = CliRunner()
    return runner.invoke(_cmd, input=user_input, catch_exceptions=False)


# ---------------------------------------------------------------------------
# Bug 34 — interactive wishlist target-price validation
# ---------------------------------------------------------------------------


class TestInteractiveTargetPrice:
    def test_typo_target_price_warns_and_sets_no_target(self, tmp_config):
        """A non-numeric typo warns instead of silently adding with no target."""
        products = [make_product(asin="TP1", title="Typo Book")]
        result = _run_browse(products, "w 1\n5o\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        assert "Invalid price" in result.output
        items = wishlist_mod.load_wishlist()
        assert len(items) == 1
        assert items[0]["asin"] == "TP1"
        assert items[0]["max_price"] is None

    def test_negative_target_price_rejected(self, tmp_config):
        """A negative target is rejected and leaves no target set."""
        products = [make_product(asin="NEG1", title="Neg Book")]
        result = _run_browse(products, "w 1\n-5\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        items = wishlist_mod.load_wishlist()
        assert len(items) == 1
        assert items[0]["asin"] == "NEG1"
        assert items[0]["max_price"] is None

    def test_zero_target_price_rejected(self, tmp_config):
        """A zero target is rejected and leaves no target set."""
        products = [make_product(asin="ZERO1", title="Zero Book")]
        result = _run_browse(products, "w 1\n0\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        items = wishlist_mod.load_wishlist()
        assert len(items) == 1
        assert items[0]["max_price"] is None

    def test_valid_target_price_still_set(self, tmp_config):
        """A valid positive target is still stored as before."""
        products = [make_product(asin="OK1", title="Ok Book")]
        result = _run_browse(products, "w 1\n3.99\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        items = wishlist_mod.load_wishlist()
        assert items[0]["max_price"] == 3.99

    def test_skip_target_price_still_works(self, tmp_config):
        """Pressing Enter to skip still adds with no target and no warning."""
        products = [make_product(asin="SK1", title="Skip Book")]
        result = _run_browse(products, "w 1\n\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        assert "Invalid price" not in result.output
        items = wishlist_mod.load_wishlist()
        assert items[0]["max_price"] is None


# ---------------------------------------------------------------------------
# Bug 35 — --locale validation on the command line
# ---------------------------------------------------------------------------


class TestLocaleValidation:
    def test_invalid_locale_rejected(self, tmp_config):
        """An unknown --locale fails with a clear error instead of silent US fallback."""
        result = CliRunner().invoke(cli, ["--locale", "xx", "search", "test"])
        assert result.exit_code != 0
        assert "Invalid locale" in result.output

    def test_valid_locale_accepted(self, tmp_config, mock_client):
        """A known --locale is accepted and runs without error."""
        products = [make_product(asin="UK01", price=3.99)]
        mock_client.search_pages.return_value = iter([(products, 1, len(products))])
        result = CliRunner().invoke(
            cli, ["--locale", "uk", "search", "test"], catch_exceptions=False
        )
        assert result.exit_code == 0, result.output
