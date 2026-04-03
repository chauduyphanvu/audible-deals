"""Shared fixtures for audible-deals tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from audible_deals.client import Product


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
        language="english",
        release_date="2024-01-15",
        in_plus_catalog=False,
    )
    defaults.update(overrides)
    return Product(**defaults)


@pytest.fixture
def sample_product():
    return make_product()


@pytest.fixture
def products_for_filtering():
    """A set of products that exercises every filter path."""
    return [
        make_product(asin="CHEAP1", price=2.99, list_price=10.0, rating=4.5,
                     length_minutes=600, language="english",
                     category_ids=["cat_fiction"]),
        make_product(asin="CHEAP2", price=4.99, list_price=20.0, rating=3.0,
                     length_minutes=120, language="english",
                     category_ids=["cat_fiction"]),
        make_product(asin="EXPENSIVE", price=25.00, list_price=25.0, rating=5.0,
                     length_minutes=900, language="english",
                     category_ids=["cat_scifi"]),
        make_product(asin="NO_PRICE", price=None, list_price=None, rating=4.0,
                     length_minutes=300, language="english",
                     category_ids=["cat_fiction"]),
        make_product(asin="FRENCH", price=3.00, list_price=15.0, rating=4.0,
                     length_minutes=400, language="french",
                     category_ids=["cat_fiction"]),
        make_product(asin="EROTICA", price=1.99, list_price=10.0, rating=4.0,
                     length_minutes=200, language="english",
                     category_ids=["cat_erotica"]),
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
    "series": [{"title": "Epic Series", "sequence": "3"}],
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

@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Redirect all config paths to a temp directory and fix Rich console for Click."""
    import audible_deals.client as client_mod
    import audible_deals.cli as cli_mod
    import audible_deals.display as display_mod
    from rich.console import Console

    monkeypatch.setattr(client_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(client_mod, "AUTH_FILE", tmp_path / "auth.json")
    monkeypatch.setattr(client_mod, "CATEGORIES_CACHE_FILE", tmp_path / "categories_cache.json")
    monkeypatch.setattr(cli_mod, "WISHLIST_FILE", tmp_path / "wishlist.json")
    monkeypatch.setattr(cli_mod, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(cli_mod, "_history_dir_created", False)
    monkeypatch.setattr(cli_mod, "LAST_RESULTS_FILE", tmp_path / "last_results.json")
    monkeypatch.setattr(cli_mod, "SEEN_ASINS_FILE", tmp_path / "seen_asins.json")
    monkeypatch.setattr(cli_mod, "CONFIG_FILE", tmp_path / "config.json")

    # Replace the Rich console with one that writes to a fresh stderr-like
    # stream, so it doesn't conflict with Click's CliRunner file handling.
    # force_terminal=False avoids ANSI codes; force_interactive=False avoids
    # the "I/O operation on closed file" crash.
    test_console = Console(force_terminal=False, force_interactive=False)
    monkeypatch.setattr(display_mod, "console", test_console)
    monkeypatch.setattr(cli_mod, "console", test_console)

    return tmp_path


# ---------------------------------------------------------------------------
# Mock DealsClient
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_client(monkeypatch):
    """Patch _get_client to return a mock that doesn't hit the network."""
    import audible_deals.cli as cli_mod

    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)

    def _get_mock(locale):
        return client

    monkeypatch.setattr(cli_mod, "_get_client", _get_mock)
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
    # Write a dummy auth file so DealsClient.client doesn't raise
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
