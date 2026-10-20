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
from tests.conftest import make_raw, seed_history

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


def _stdout_json(result) -> Any:
    """The JSON on stdout alone (the command may also print notes on stderr)."""
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


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
    seed_history(
        "GOLDWISH01",
        [
            {
                "date": (today - datetime.timedelta(days=days)).isoformat(),
                "price": price,
                "title": "Golden Wishlist Book",
            }
            for days, price in ((6, 12.0), (3, 9.0), (1, 5.0))
        ],
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


# ---------------------------------------------------------------------------
# JSON key structure: outputs that predate schema versions (all schema v1)
# ---------------------------------------------------------------------------


def test_library_json_structure(mock_client, tmp_config):
    from tests.conftest import make_product

    mock_client.get_library_pages.return_value = iter(
        [([make_product(asin="LIBGOLD001"), make_product(asin="LIBGOLD002")], 1)]
    )

    products = _stdout_json(CliRunner().invoke(cli, ["library", "--json"]))

    assert len(products) == 2
    check_golden("json/library.json", structure([products]))


def test_library_stats_json_structure(mock_client, tmp_config):
    from tests.conftest import make_product

    mock_client.get_library_pages.return_value = iter(
        [([make_product(asin="LIBGOLD001")], 1)]
    )

    payload = _stdout_json(CliRunner().invoke(cli, ["library", "--stats", "--json"]))

    check_golden("json/library_stats.json", structure([payload]))


def test_series_json_structure(mock_client, tmp_config):
    from tests.conftest import make_product, make_series_products_batch

    owned = [
        make_product(
            asin=f"SERGOLD00{n}",
            series_name="Golden Series",
            series_position=str(n),
            series_asin="SER_GOLD",
        )
        for n in (1, 2)
    ]
    next_book = make_product(
        asin="SERGOLD003",
        series_name="Golden Series",
        series_position="3",
        series_asin="SER_GOLD",
    )
    mock_client.get_library.return_value = owned
    mock_client.get_series_products_many.return_value = make_series_products_batch(
        {"SER_GOLD": [*owned, next_book]}
    )

    products = _stdout_json(CliRunner().invoke(cli, ["series", "--json"]))

    assert products
    check_golden("json/series.json", structure([products]))


def test_series_gaps_json_structure(mock_client, tmp_config):
    from tests.conftest import make_product, make_series_products_batch

    owned = [
        make_product(
            asin=f"GAPGOLD00{n}",
            series_name="Gap Series",
            series_position=str(n),
            series_asin="SER_GAP",
        )
        for n in (1, 2)
    ]
    missing = make_product(
        asin="GAPGOLD003",
        series_name="Gap Series",
        series_position="3",
        series_asin="SER_GAP",
    )
    mock_client.get_library.return_value = owned
    mock_client.get_series_products_many.return_value = make_series_products_batch(
        {"SER_GAP": [*owned, missing]}
    )

    gaps = _stdout_json(CliRunner().invoke(cli, ["series", "--gaps", "--json"]))

    assert gaps and gaps[0]["missing"]
    check_golden("json/series_gaps.json", structure([gaps]))


def test_notify_json_structure(mock_client, tmp_config):
    from audible_deals import wishlist as wishlist_mod
    from tests.conftest import make_product

    wishlist_mod.save_wishlist(
        [{"asin": "NOTIGOLD01", "title": "T", "max_price": 5.0, "added": ""}]
    )
    mock_client.get_products_batch.return_value = [
        make_product(asin="NOTIGOLD01", price=3.0)
    ]

    payload = _stdout_json(CliRunner().invoke(cli, ["notify"]))

    assert payload["count"] == 1
    check_golden("json/notify.json", structure([payload]))


# ---------------------------------------------------------------------------
# JSON key structure: outputs with "schema_version": 1
# ---------------------------------------------------------------------------


def test_doctor_json_structure(mock_client, tmp_config):
    payload = json.loads(CliRunner().invoke(cli, ["doctor", "--json"]).stdout)

    check_golden("json/doctor.json", structure([payload]))


def test_watch_json_structure(mock_client, tmp_config):
    from audible_deals import wishlist as wishlist_mod
    from tests.conftest import make_product

    wishlist_mod.save_wishlist(
        [
            {"asin": "WATCHGOLD1", "title": "T", "max_price": 5.0, "added": ""},
            {"asin": "WATCHGOLD2", "title": "T", "max_price": 5.0, "added": ""},
            {"asin": "WATCHGOLD3", "title": "Gone", "max_price": 5.0, "added": ""},
            {"type": "author", "author": "A", "max_price": 4.0, "added": ""},
        ]
    )
    paths.config_file().write_text(json.dumps({"credit_price": 10}))
    mock_client.get_products_batch.return_value = [
        make_product(asin="WATCHGOLD1", price=3.0),
        make_product(asin="WATCHGOLD2", price=None, list_price=None),
    ]

    payload = _stdout_json(CliRunner().invoke(cli, ["watch", "--json"]))

    assert payload["missing"] == ["WATCHGOLD3"]
    check_golden("json/watch.json", structure([payload]))


def test_track_status_json_structure(tmp_config, monkeypatch):
    import audible_deals.cli.track as track_module
    from audible_deals.state_store import (
        TrackState,
        save_install_record,
        save_track_state,
    )

    monkeypatch.setattr(track_module.scheduler, "installed", lambda: (True, "plist"))
    save_install_record(
        {"every": "6h", "method": "launchd agent", "installed_at": "2026-06-01T09:00"}
    )
    run = {
        "at": "2026-06-09T08:00:00",
        "duration_s": 4.2,
        "wishlist_checked": 3,
        "extra_tracked_checked": 7,
        "hits": 1,
        "monitors_checked": 0,
        "monitor_events": 0,
    }
    save_track_state(TrackState(runs=[run]))

    payload = _stdout_json(
        CliRunner().invoke(cli, ["track", "status", "--history", "--json"])
    )

    assert payload["history"]
    check_golden("json/track_status.json", structure([payload]))


def _seed_monitor_and_profile():
    from audible_deals.automation_models import MonitorSnapshot
    from audible_deals.config_store import save_monitors, save_profiles
    from audible_deals.state_store import save_monitor_snapshot

    save_monitors(
        {
            "golden": {
                "version": 1,
                "name": "golden",
                "enabled": True,
                "locale": "us",
                "mode": "search",
                "query": "sanderson",
                "profile": "scifi",
                "settings": {"pages": 2, "limit": None, "sort": "price"},
                "webhook": "https://hooks.example.test/x",
            }
        }
    )
    save_monitor_snapshot("golden", MonitorSnapshot(initialized=True))
    save_profiles({"scifi": {"genre": "sci-fi", "max_price": 5.0}})


def test_monitor_list_json_structure(tmp_config):
    _seed_monitor_and_profile()

    payload = _stdout_json(CliRunner().invoke(cli, ["monitor", "list", "--json"]))

    assert payload["monitors"]
    check_golden("json/monitor_list.json", structure([payload]))


def test_monitor_show_json_structure(tmp_config):
    _seed_monitor_and_profile()

    payload = _stdout_json(
        CliRunner().invoke(cli, ["monitor", "show", "golden", "--json"])
    )

    check_golden("json/monitor_show.json", structure([payload]))


def test_config_json_structures(tmp_config):
    paths.config_file().write_text(
        json.dumps({"max_price": 5.0, "webhook": "https://hooks.example.test/x"})
    )
    runner = CliRunner()

    listing = _stdout_json(runner.invoke(cli, ["config", "list", "--json"]))
    one = _stdout_json(runner.invoke(cli, ["config", "get", "max-price", "--json"]))

    check_golden("json/config_list.json", structure([listing]))
    check_golden("json/config_get.json", structure([one]))


def test_profile_json_structures(tmp_config):
    _seed_monitor_and_profile()
    runner = CliRunner()

    listing = _stdout_json(runner.invoke(cli, ["profile", "list", "--json"]))
    one = _stdout_json(runner.invoke(cli, ["profile", "show", "scifi", "--json"]))

    check_golden("json/profile_list.json", structure([listing]))
    check_golden("json/profile_show.json", structure([one]))


def test_categories_json_structure(mock_client, tmp_config):
    mock_client.get_categories.return_value = [{"id": "1", "name": "Mystery"}]

    payload = _stdout_json(CliRunner().invoke(cli, ["categories", "--json"]))

    check_golden("json/categories.json", structure([payload]))


def test_detail_json_structure(mock_client, tmp_config):
    from tests.conftest import make_product

    paths.config_file().write_text(json.dumps({"credit_price": 10}))
    mock_client.get_product.return_value = make_product(asin="DETAILGOLD")

    payload = _stdout_json(CliRunner().invoke(cli, ["detail", "DETAILGOLD", "--json"]))

    check_golden("json/detail.json", structure([payload]))


def test_compare_json_structure(mock_client, tmp_config):
    from tests.conftest import make_product

    paths.config_file().write_text(json.dumps({"credit_price": 10}))
    mock_client.get_products_batch.return_value = [
        make_product(asin="COMPAREGD1"),
        make_product(asin="COMPAREGD2"),
    ]

    payload = _stdout_json(
        CliRunner().invoke(cli, ["compare", "COMPAREGD1", "COMPAREGD2", "--json"])
    )

    check_golden("json/compare.json", structure([payload]))


def test_every_new_json_payload_declares_its_schema_version():
    # The older outputs keep their (list or unversioned) shapes and are schema
    # version 1 by definition; everything added since carries the number.
    for name in (
        "doctor",
        "watch",
        "track_status",
        "monitor_list",
        "monitor_show",
        "config_list",
        "config_get",
        "profile_list",
        "profile_show",
        "categories",
        "detail",
        "compare",
    ):
        shape = json.loads(
            (GOLDEN_DIR / "json" / f"{name}.json").read_text(encoding="utf-8")
        )
        assert shape["schema_version"] == "int", name
