"""Tests for audible_deals.state — persistence and I/O functions."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from audible_deals.cli import cli
from audible_deals.state import load_seen_asins, save_seen_asins
from tests.conftest import make_product


# ===================================================================
# load_seen_asins / save_seen_asins
# ===================================================================

class TestLoadSeenAsins:
    def test_loads_from_seen_file(self, tmp_config):
        import audible_deals.state as state_mod
        state_mod.SEEN_ASINS_FILE.write_text(json.dumps(["A1", "A2"]))
        seen = load_seen_asins()
        assert seen == {"A1", "A2"}

    def test_empty_when_no_file(self, tmp_config):
        seen = load_seen_asins()
        assert seen == set()

    def test_returns_set_from_list(self, tmp_config):
        import audible_deals.state as state_mod
        state_mod.SEEN_ASINS_FILE.write_text(json.dumps(["B1", "B2", "B1"]))
        seen = load_seen_asins()
        assert seen == {"B1", "B2"}

    def test_empty_on_corrupt_file(self, tmp_config):
        import audible_deals.state as state_mod
        state_mod.SEEN_ASINS_FILE.write_text("not valid json")
        seen = load_seen_asins()
        assert seen == set()


class TestCumulativeSeenAsins:
    def test_save_and_load(self, tmp_config):
        save_seen_asins({"A1", "A2"})
        assert load_seen_asins() == {"A1", "A2"}

    def test_cumulative_append(self, tmp_config):
        save_seen_asins({"A1", "A2"})
        save_seen_asins({"A3", "A4"})
        assert load_seen_asins() == {"A1", "A2", "A3", "A4"}

    def test_no_duplicates(self, tmp_config):
        import audible_deals.state as state_mod
        save_seen_asins({"A1", "A2"})
        save_seen_asins({"A2", "A3"})
        seen = load_seen_asins()
        assert seen == {"A1", "A2", "A3"}
        data = json.loads(state_mod.SEEN_ASINS_FILE.read_text())
        assert data == sorted(data)

    def test_empty_when_no_file(self, tmp_config):
        assert load_seen_asins() == set()

    def test_clear_seen_command(self, tmp_config, mock_client):
        save_seen_asins({"A1", "A2"})
        runner = CliRunner()
        result = runner.invoke(cli, ["last", "--clear-seen"])
        assert result.exit_code == 0
        assert "cleared" in result.output.lower()
        assert load_seen_asins() == set()
