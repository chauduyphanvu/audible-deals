"""Shared fixtures for audible-deals tests."""

from __future__ import annotations

import copy
import itertools
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from audible_deals.client import CatalogSearchResult, SeriesProductsBatch
from audible_deals.product import Product

# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------


def skip_on_windows(reason: str):
    """``skipif(win32)`` for the few tests that are genuinely POSIX-only."""
    return pytest.mark.skipif(sys.platform == "win32", reason=reason)


#: The generators for cron, systemd and launchd files only ever run on Linux
#: and macOS, and their tests compare against text built from POSIX paths.
posix_scheduler_text = skip_on_windows(
    "cron, systemd and launchd files are written on Linux/macOS; "
    "the expected text holds POSIX path literals"
)


def assert_mode(path: Path, expected: int) -> None:
    """Assert POSIX permission bits (Windows has no 0600/0700 to compare)."""
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == expected, oct(
            stat.S_IMODE(path.stat().st_mode)
        )


def bare_env(**extra: str) -> dict[str, str]:
    """A minimal environment for a child Python (Windows needs SYSTEMROOT to start it)."""
    keep = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT") if k in os.environ}
    return {**keep, **extra}


# ---------------------------------------------------------------------------
# Product factory
# ---------------------------------------------------------------------------


def make_product(**overrides) -> Product:
    """Build a Product with sensible defaults. Override any field via kwargs."""
    defaults = dict(
        asin="B000TEST01",
        title="Test Book",
        subtitle="",
        authors=["Author One"],
        narrators=["Narrator One"],
        publisher="Test Publisher",
        price=9.99,
        list_price=19.99,
        length_minutes=600,  # 10 hours
        rating=4.5,
        num_ratings=1000,
        categories=["Science Fiction & Fantasy", "Fantasy"],
        category_ids=["18580606011", "18580607011"],
        series_name="Test Series",
        series_position="1",
        series_asin="",
        language="english",
        release_date="2024-01-15",
        in_plus_catalog=False,
    )
    defaults.update(overrides)
    return Product(**defaults)


def make_series_products_batch(
    products=None,
    *,
    failures=None,
    missing_asins=None,
    product_failures=None,
):
    return SeriesProductsBatch(
        products={
            series_asin: tuple(series_products)
            for series_asin, series_products in (products or {}).items()
        },
        failures=dict(failures or {}),
        missing_asins={
            series_asin: tuple(asins)
            for series_asin, asins in (missing_asins or {}).items()
        },
        product_failures={
            series_asin: tuple(errors)
            for series_asin, errors in (product_failures or {}).items()
        },
    )


def seed_history(asin, entries, locale="us"):
    """Record dated prices through the real write path.

    *entries* are ``(date, price)`` pairs or ``{"date", "price", "title"}`` dicts.
    """
    from audible_deals.price_history import record_prices

    for entry in entries:
        if isinstance(entry, dict):
            date, price, title = entry["date"], entry["price"], entry.get("title", "")
        else:
            (date, price), title = entry, "T"
        record_prices(
            [make_product(asin=asin, locale=locale, price=price, title=title)],
            observation_date=date,
        )


def seed_prices(asin, prices, locale="us", title="T"):
    """Record *prices* on consecutive days starting 2024-01-01."""
    seed_history(
        asin,
        [
            {"date": f"2024-01-{i + 1:02d}", "price": price, "title": title}
            for i, price in enumerate(prices)
        ],
        locale=locale,
    )


def raw_session() -> str | None:
    """The stored last-results JSON text, or None when there is no session."""
    from audible_deals import db

    row = (
        db.state().execute("SELECT body FROM result_sessions WHERE slot = 1").fetchone()
    )
    return None if row is None else row[0]


def put_raw_session(text: str) -> None:
    """Store *text* as the last-results session without validating it."""
    from audible_deals import db

    with db.transaction(db.state()) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO result_sessions (slot, body, saved_at) "
            "VALUES (1, ?, '2026-01-01T00:00:00+00:00')",
            (text,),
        )


def count_rows(table: str) -> int:
    from audible_deals import db

    return db.state().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


@pytest.fixture
def sample_product():
    return make_product()


@pytest.fixture
def products_for_filtering():
    """A set of products that exercises every filter path."""
    return [
        make_product(
            asin="CHEAP1",
            price=2.99,
            list_price=10.0,
            rating=4.5,
            length_minutes=600,
            language="english",
            category_ids=["cat_fiction"],
        ),
        make_product(
            asin="CHEAP2",
            price=4.99,
            list_price=20.0,
            rating=3.0,
            length_minutes=120,
            language="english",
            category_ids=["cat_fiction"],
        ),
        make_product(
            asin="EXPENSIVE",
            price=25.00,
            list_price=25.0,
            rating=5.0,
            length_minutes=900,
            language="english",
            category_ids=["cat_scifi"],
        ),
        make_product(
            asin="NO_PRICE",
            price=None,
            list_price=None,
            rating=4.0,
            length_minutes=300,
            language="english",
            category_ids=["cat_fiction"],
        ),
        make_product(
            asin="FRENCH",
            price=3.00,
            list_price=15.0,
            rating=4.0,
            length_minutes=400,
            language="french",
            category_ids=["cat_fiction"],
        ),
        make_product(
            asin="EROTICA",
            price=1.99,
            list_price=10.0,
            rating=4.0,
            length_minutes=200,
            language="english",
            category_ids=["cat_erotica"],
        ),
    ]


# ---------------------------------------------------------------------------
# Raw API response fixture
# ---------------------------------------------------------------------------

RAW_API_PRODUCT = {
    "asin": "B00RAWTEST",
    "title": "Raw Title",
    "subtitle": "Raw Sub",
    "authors": [{"name": "Author A"}, {"name": "Author B"}],
    "narrators": [{"name": "Narrator X"}],
    "publisher_name": "Raw Publisher",
    "runtime_length_min": 720,
    "language": "english",
    "release_date": "2023-06-01",
    "price": {
        "lowest_price": {"base": 3.99},
        "list_price": {"base": 14.99},
    },
    "rating": {
        "overall_distribution": {
            "display_average_rating": "4.5",
            "num_ratings": "2500",
        }
    },
    "category_ladders": [
        {
            "ladder": [
                {"id": "cat1", "name": "Science Fiction & Fantasy"},
                {"id": "cat2", "name": "Science Fiction"},
            ]
        }
    ],
    "series": [{"title": "Epic Series", "sequence": "3", "asin": "SER001"}],
    "plans": [{"plan_name": "Audible Plus"}],
}

RAW_API_PRODUCT_MINIMAL = {
    "asin": "B00MINIMAL",
    "title": "Minimal",
}


@pytest.fixture
def raw_api_product():
    return RAW_API_PRODUCT.copy()


@pytest.fixture
def raw_api_product_minimal():
    return RAW_API_PRODUCT_MINIMAL.copy()


# ---------------------------------------------------------------------------
# Temp config dir
# ---------------------------------------------------------------------------


_tmp_counter = itertools.count()


@pytest.fixture
def tmp_path(tmp_path_factory):
    """A fresh, empty directory per test, created without pytest's directory scan.

    pytest's own ``tmp_path`` looks for the next free number by listing every
    sibling directory, which grows with each test in the session; with a couple
    of thousand tests that listing costs several seconds. A counter gives the
    same isolation (every test gets its own new directory) without the scan.
    """
    path = tmp_path_factory.getbasetemp() / f"t{next(_tmp_counter):04d}"
    path.mkdir()
    return path


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "fresh_schema: build the databases from scratch instead of copying the "
        "session's schema-only template",
    )


@pytest.fixture(scope="session")
def _database_templates(tmp_path_factory):
    """Schema-only copies of both databases, built once with the real code.

    Creating a database costs a few milliseconds (WAL setup plus every table);
    hundreds of tests would pay that for nothing. Copying these bytes into a
    test's own data directory gives the same empty databases much faster. The
    JSON-import marker is deliberately absent, so a first open still imports.
    """
    from audible_deals import db

    directory = tmp_path_factory.mktemp("db-templates")
    templates = {}
    for kind, name in (("state", "state.db"), ("cache", "cache.db")):
        path = directory / name
        conn = db._connect(path)
        try:
            db._apply_schema(conn, kind)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        templates[name] = path.read_bytes()
    return templates


@pytest.fixture(autouse=True)
def _isolated_data_dir(request, tmp_path, monkeypatch):
    """Point every test at its own data directory (never the real one).

    ``DEALS_HOME`` is read at call time, so one environment variable redirects
    config, auth, the lock file and both SQLite databases. A test that exercises
    database creation itself opts out of the template copy with
    ``@pytest.mark.fresh_schema``.
    """
    from audible_deals import db

    monkeypatch.setenv("DEALS_HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # Anything that still asks for the user's home (scheduler unit files, the
    # legacy data directory) lands in the sandbox, never in the real one.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    if request.node.get_closest_marker("fresh_schema") is None:
        templates = request.getfixturevalue("_database_templates")
        real_connect = db._connect

        def connect_from_template(path):
            if not path.exists() and path.name in templates:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(templates[path.name])
            return real_connect(path)

        monkeypatch.setattr(db, "_connect", connect_from_template)
    yield
    db.close_all()


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """The isolated data directory, with the Rich console fixed up for Click."""
    from rich.console import Console

    from audible_deals.presentation import terminal as display_mod

    # Replace the Rich console with one that writes to a fresh stderr-like
    # stream, so it doesn't conflict with Click's CliRunner file handling.
    # force_terminal=False avoids ANSI codes; force_interactive=False avoids
    # the "I/O operation on closed file" crash.
    test_console = Console(force_terminal=False, force_interactive=False)
    console_modules = _cli_console_modules()
    monkeypatch.setattr(display_mod, "console", test_console)
    for mod in console_modules:
        monkeypatch.setattr(mod, "console", test_console)

    return tmp_path


# ---------------------------------------------------------------------------
# Mock DealsClient
# ---------------------------------------------------------------------------


def _cli_console_modules():
    """All cli submodules that bind the shared Rich console by name."""
    import audible_deals.cli.catalog as cli_catalog_mod
    import audible_deals.cli.config as cli_config_mod
    import audible_deals.cli.foryou as cli_foryou_mod
    import audible_deals.cli.helpers as cli_helpers_mod
    import audible_deals.cli.history as cli_history_mod
    import audible_deals.cli.interactive as cli_interactive_mod
    import audible_deals.cli.last as cli_last_mod
    import audible_deals.cli.library as cli_library_mod
    import audible_deals.cli.misc as cli_misc_mod
    import audible_deals.cli.monitor as cli_monitor_mod
    import audible_deals.cli.notify as cli_notify_mod
    import audible_deals.cli.series as cli_series_mod
    import audible_deals.cli.track as cli_track_mod
    import audible_deals.cli.wishlist as cli_wishlist_mod
    import audible_deals.notification_workflow as notification_workflow_mod
    import audible_deals.presentation.dry_run as presentation_dry_run_mod
    import audible_deals.presentation.result_output as result_output_mod
    import audible_deals.result_publication as result_publication_mod

    return (
        cli_helpers_mod,
        result_output_mod,
        result_publication_mod,
        presentation_dry_run_mod,
        cli_interactive_mod,
        cli_catalog_mod,
        cli_config_mod,
        cli_history_mod,
        cli_library_mod,
        cli_series_mod,
        cli_last_mod,
        cli_wishlist_mod,
        cli_monitor_mod,
        cli_notify_mod,
        notification_workflow_mod,
        cli_misc_mod,
        cli_track_mod,
        cli_foryou_mod,
    )


@pytest.fixture
def mock_client(monkeypatch):
    """Patch _get_client to return a mock that doesn't hit the network."""
    import audible_deals.cli.catalog as cli_catalog_mod
    import audible_deals.cli.foryou as cli_foryou_mod
    import audible_deals.cli.helpers as cli_helpers_mod
    import audible_deals.cli.library as cli_library_mod
    import audible_deals.cli.misc as cli_misc_mod
    import audible_deals.cli.notify as cli_notify_mod
    import audible_deals.cli.series as cli_series_mod
    import audible_deals.cli.track as cli_track_mod
    import audible_deals.cli.wishlist as cli_wishlist_mod

    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)

    def search_segments(requests, page_callback=None):
        results = []
        for index, request in enumerate(requests):
            pages = []
            error = None
            try:
                query_args = (
                    {"title": request.title}
                    if request.title
                    else {"keywords": request.keywords}
                )
                for products, page, total in client.search_pages(
                    **query_args,
                    category_id=request.category_id,
                    sort_by=request.sort_by,
                    max_pages=request.max_pages,
                ):
                    pages.append((products, page, total))
                    if page_callback is not None:
                        page_callback(index, products, page, total)
            except Exception as exc:
                error = exc
                if not request.optional:
                    raise
                if not pages and page_callback is not None:
                    page_callback(index, [], 1, 0)
            results.append(CatalogSearchResult(tuple(pages), error))
        return results

    client.search_segments.side_effect = search_segments
    client.get_series_products_many.return_value = make_series_products_batch()

    def _get_mock(locale):
        return client

    for mod in (
        cli_helpers_mod,
        cli_catalog_mod,
        cli_library_mod,
        cli_series_mod,
        cli_notify_mod,
        cli_wishlist_mod,
        cli_misc_mod,
        cli_track_mod,
        cli_foryou_mod,
    ):
        monkeypatch.setattr(mod, "_get_client", _get_mock)
    return client


# ---------------------------------------------------------------------------
# Raw API response builder
# ---------------------------------------------------------------------------


def make_raw(asin: str = "B00RAW", **overrides) -> dict:
    """Build a raw API response dict from the RAW_API_PRODUCT template."""
    raw = copy.deepcopy(RAW_API_PRODUCT)
    raw["asin"] = asin
    if "title" not in overrides:
        raw["title"] = f"Title {asin}"
    raw.update(overrides)
    return raw


# ---------------------------------------------------------------------------
# Low-level API mock (patches audible.Client, not DealsClient)
# ---------------------------------------------------------------------------


@pytest.fixture
def api(tmp_config, monkeypatch):
    """Mock at the audible.Client.get level so DealsClient methods run real code.

    Returns SimpleNamespace(get_mock, tmp_path) where get_mock is the
    mock for audible.Client().get — set .return_value or .side_effect
    to program API responses.
    """
    # Write a dummy auth file so the transport's lazy client can load.
    auth_file = tmp_config / "auth.json"
    auth_file.write_text(json.dumps({"encryption": False, "locale_code": "us"}))

    mock_client_instance = MagicMock()
    get_mock = mock_client_instance.get

    # Patch audible.Authenticator.from_file to skip real auth
    mock_auth = MagicMock()
    monkeypatch.setattr("audible.Authenticator.from_file", lambda *a, **kw: mock_auth)
    # Patch audible.Client to return our mock
    monkeypatch.setattr("audible.Client", lambda *a, **kw: mock_client_instance)

    return SimpleNamespace(get_mock=get_mock, tmp_path=tmp_config)
