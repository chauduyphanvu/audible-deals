"""Regression tests for cache/config bug fixes (bugs 29, 33)."""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

from audible_deals.cli import cli
import audible_deals.config_store as config_store_mod
import audible_deals.constants as constants_mod
from audible_deals.results_cache import resolve_last_references


class TestResolveLastReferenceMissingAsin:
    def test_old_format_entry_without_asin_raises_clickexception(self, tmp_config):
        """A cache entry lacking 'asin' must surface as a clean ClickException."""
        data = [{"title": "NoAsinItem"}]
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(data))
        with pytest.raises(click.ClickException, match="no ASIN"):
            resolve_last_references((1,))

    def test_new_format_entry_without_asin_raises_clickexception(self, tmp_config):
        """New-format results entry with no 'asin' must also raise ClickException."""
        cache_obj = {"title": "Last results", "results": [{"title": "NoAsinItem"}]}
        constants_mod.LAST_RESULTS_FILE.write_text(json.dumps(cache_obj))
        with pytest.raises(click.ClickException, match="no ASIN"):
            resolve_last_references((1,))


class TestProfileSaveSortValidation:
    def test_invalid_sort_rejected(self, tmp_config):
        """profile save --sort nonsense must fail rather than persist a bogus sort."""
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "save", "bad", "--sort", "nonsense"])
        assert result.exit_code != 0
        assert "Invalid sort" in result.output
        assert "bad" not in config_store_mod.load_profiles()

    def test_valid_sort_persisted(self, tmp_config):
        """A valid --sort value is still accepted and saved."""
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "save", "good", "--sort", "rating"])
        assert result.exit_code == 0, result.output
        assert config_store_mod.load_profiles()["good"]["sort"] == "rating"

    def test_no_sort_still_saves(self, tmp_config):
        """Omitting --sort must not trigger validation (default is dropped)."""
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "save", "nosort", "--genre", "sci-fi"])
        assert result.exit_code == 0, result.output
        profile = config_store_mod.load_profiles()["nosort"]
        assert "sort" not in profile
        assert profile["genre"] == "sci-fi"
