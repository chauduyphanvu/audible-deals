"""Regression tests for config_store numeric range validation bugs."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from audible_deals.cli import cli
from audible_deals.config_store import coerce_config_value


def _run(runner, args):
    return runner.invoke(cli, args, catch_exceptions=False)


# ---------------------------------------------------------------------------
# Bug 30 / 32: config set must reject values the equivalent CLI flags reject.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key, value",
    [
        ("min_discount", "200"),  # --min-discount is IntRange(0, 100)
        ("min_discount", "-1"),
        ("max_price", "-5"),  # --max-price is FloatRange(min=0)
        ("max_pph", "-0.5"),  # --max-price-per-hour is FloatRange(min=0)
        ("pages", "0"),  # --pages is IntRange(min=1)
        ("limit", "-1"),  # --limit is IntRange(min=0)
        ("min_rating", "-1"),
        ("min_ratings", "-10"),
        ("min_hours", "-2"),
    ],
)
def test_coerce_rejects_out_of_range(key, value):
    import click

    with pytest.raises(click.ClickException):
        coerce_config_value(key, value)


@pytest.mark.parametrize(
    "key, value, expected",
    [
        ("min_discount", "100", 100),
        ("min_discount", "0", 0),
        ("max_price", "0", 0.0),
        ("pages", "1", 1),
        ("limit", "0", 0),
        ("min_rating", "4.5", 4.5),
    ],
)
def test_coerce_accepts_in_range(key, value, expected):
    assert coerce_config_value(key, value) == expected


def test_config_set_rejects_min_discount_over_100(tmp_config, mock_client):
    result = CliRunner().invoke(cli, ["config", "set", "min-discount", "200"])
    assert result.exit_code != 0
    cfg_file = tmp_config / "config.json"
    if cfg_file.exists():
        assert "min_discount" not in json.loads(cfg_file.read_text())


def test_config_set_rejects_negative_max_price(tmp_config, mock_client):
    result = CliRunner().invoke(cli, ["config", "set", "max-price", "-5"])
    assert result.exit_code != 0
    cfg_file = tmp_config / "config.json"
    if cfg_file.exists():
        assert "max_price" not in json.loads(cfg_file.read_text())


def test_config_set_rejects_zero_pages(tmp_config, mock_client):
    result = CliRunner().invoke(cli, ["config", "set", "pages", "0"])
    assert result.exit_code != 0


def test_config_set_accepts_valid_values(tmp_config, mock_client):
    result = _run(CliRunner(), ["config", "set", "min-discount", "70"])
    assert result.exit_code == 0
    assert json.loads((tmp_config / "config.json").read_text())["min_discount"] == 70
