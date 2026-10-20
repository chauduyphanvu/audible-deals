"""Characterization ("golden") tests for the CLI surface.

Two contracts are pinned here so later refactors cannot change them silently:

* the set of option names of every command and subcommand (names only, never
  help text or wrapping), and
* the JSON key structure emitted by the machine-readable commands.

The expected values live in ``tests/golden/``. When a change *intends* to alter
the surface, regenerate them and explain why in the commit message::

    UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py
"""

from __future__ import annotations

import datetime
import difflib
import json
import os
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from audible_deals import paths
from audible_deals.cli import cli
from tests.conftest import make_raw

GOLDEN_DIR = Path(__file__).parent / "golden"
REGENERATE = "UPDATE_GOLDEN"


def check_golden(relative_name: str, actual: Any) -> None:
    """Compare *actual* with the stored golden file, or rewrite it on request."""
    path = GOLDEN_DIR / relative_name
    text = json.dumps(actual, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if os.environ.get(REGENERATE):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    if not path.exists():
        pytest.fail(
            f"Missing golden file {path}. Create it with {REGENERATE}=1 "
            "uv run pytest tests/test_golden.py"
        )
    expected = path.read_text(encoding="utf-8")
    if expected != text:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                text.splitlines(keepends=True),
                fromfile=f"golden/{relative_name}",
                tofile="actual",
            )
        )
        pytest.fail(
            f"{relative_name} changed. If this is intended, regenerate with "
            f"{REGENERATE}=1 uv run pytest tests/test_golden.py and explain the "
            f"change in the commit message.\n{diff}"
        )


# ---------------------------------------------------------------------------
# Option names per command
# ---------------------------------------------------------------------------


def _option_names(command: click.Command) -> list[str]:
    names: set[str] = set()
    for param in command.params:
        if isinstance(param, click.Option):
            names.update(param.opts)
            names.update(param.secondary_opts)
    return sorted(names)


def _command_options(command: click.Command, path: str = "deals") -> dict[str, list]:
    surface = {path: _option_names(command)}
    if isinstance(command, click.Group):
        ctx = click.Context(command)
        for name in command.list_commands(ctx):
            subcommand = command.get_command(ctx, name)
            assert subcommand is not None, f"{path} {name}"
            surface.update(_command_options(subcommand, f"{path} {name}"))
    return surface


def test_option_names_per_command():
    check_golden("cli_options.json", _command_options(cli))


# ---------------------------------------------------------------------------
# JSON key structure
# ---------------------------------------------------------------------------


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def structure(values: list[Any]) -> Any:
    """Describe the shape shared by *values* (keys and value types, no values)."""
    kinds = sorted({_kind(v) for v in values})
    if kinds == ["object"]:
        keys = sorted({key for value in values for key in value})
        shape = {}
        for key in keys:
            present = [value[key] for value in values if key in value]
            label = key if len(present) == len(values) else f"{key}?"
            shape[label] = structure(present)
        return shape
    if kinds == ["array"]:
        items = [item for value in values for item in value]
        return [structure(items)] if items else []
    return "|".join(kinds)


def _json_output(result) -> Any:
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


@pytest.fixture
def catalog(api):
    """A small deterministic catalog served through the mocked HTTP boundary."""
    products = [
        make_raw(
            "GOLD000001",
            price={"lowest_price": {"base": 3.99}, "list_price": {"base": 14.99}},
        ),
        make_raw(
            "GOLD000002",
            price={"lowest_price": {"base": 4.5}, "list_price": {"base": 9.0}},
            series=[],
            plans=[],
        ),
    ]
    api.get_mock.return_value = {"products": products, "total_results": len(products)}
    return api


def _seed_history_and_wishlist():
    today = datetime.date.today()
    history_dir = paths.history_dir()
    history_dir.mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "date": (today - datetime.timedelta(days=days)).isoformat(),
            "price": price,
            "title": "Golden Wishlist Book",
        }
        for days, price in ((6, 12.0), (3, 9.0), (1, 5.0))
    ]
    (history_dir / "GOLDWISH01.json").write_text(
        json.dumps({"marketplaces": {"us": entries}})
    )
    paths.wishlist_file().write_text(
        json.dumps(
            [
                {
                    "asin": "GOLDWISH01",
                    "title": "Golden Wishlist Book",
                    "max_price": 6.0,
                    "added": today.isoformat(),
                    "locale": "us",
                },
                {
                    "type": "author",
                    "author": "Golden Author",
                    "max_price": 5.0,
                    "added": today.isoformat(),
                },
            ]
        )
    )


FIND_ARGS = ["find", "--pages", "1", "--all-languages", "--max-price", "20"]


def test_find_json_structure(catalog):
    result = CliRunner().invoke(cli, [*FIND_ARGS, "--json"])

    products = _json_output(result)
    assert len(products) == 2
    check_golden("json/find.json", structure([products]))


def test_search_json_structure(catalog):
    result = CliRunner().invoke(
        cli, ["search", "golden", "--pages", "1", "--all-languages", "--json"]
    )

    products = _json_output(result)
    assert products
    check_golden("json/search.json", structure([products]))


def test_last_json_structure(catalog):
    runner = CliRunner()
    assert runner.invoke(cli, [*FIND_ARGS, "--quiet"]).exit_code == 0

    products = _json_output(runner.invoke(cli, ["last", "--json"]))

    assert products
    check_golden("json/last.json", structure([products]))


def test_find_dry_run_json_structure(api):
    result = CliRunner().invoke(
        cli,
        [*FIND_ARGS, "--category", "18580606011", "--keywords", "golden"]
        + ["--dry-run", "--json"],
    )

    payload = _json_output(result)
    check_golden("json/find_dry_run.json", structure([payload]))


def test_wishlist_list_json_structure(tmp_config):
    _seed_history_and_wishlist()

    payload = _json_output(CliRunner().invoke(cli, ["wishlist", "list", "--json"]))

    assert payload["items"] and payload["author_watches"]
    check_golden("json/wishlist_list.json", structure([payload]))


def test_history_json_structure(tmp_config):
    _seed_history_and_wishlist()

    entries = _json_output(CliRunner().invoke(cli, ["history", "GOLDWISH01", "--json"]))

    assert len(entries) == 3
    check_golden("json/history.json", structure([entries]))


def test_history_all_json_structure(tmp_config):
    _seed_history_and_wishlist()

    payload = _json_output(CliRunner().invoke(cli, ["history", "--all", "--json"]))

    assert "GOLDWISH01" in payload
    check_golden("json/history_all.json", structure([payload]))


def test_recap_json_structure(tmp_config):
    _seed_history_and_wishlist()

    payload = _json_output(CliRunner().invoke(cli, ["recap", "--days", "7", "--json"]))

    assert payload["drops"]
    check_golden("json/recap.json", structure([payload]))
