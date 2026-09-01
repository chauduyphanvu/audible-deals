"""Audible client and product parsing behavior."""

from __future__ import annotations

import hashlib
import json
import logging
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock

import click
import pytest

from audible_deals.audible_transport import AudibleTransport
from audible_deals.auth_store import AuthStore, _captcha_callback
from audible_deals.client import DealsClient, _validate_category_id
from audible_deals.constants import MAX_PAGE_SIZE
from audible_deals.product import (
    _base_price,
    _extract_categories,
    _extract_prices,
    parse_product,
)
from audible_deals.taste import build_profile
from tests.conftest import make_product


def _make_429_exc(retry_after: str | None = None) -> Exception:
    """Build an exception whose .response mimics a 429 HTTP response."""
    resp = SimpleNamespace(
        status_code=429,
        headers={} if retry_after is None else {"Retry-After": retry_after},
    )
    exc = Exception("rate limited")
    exc.status_code = 429
    exc.response = resp
    return exc


def _capture_thread_error(errors, operation):
    try:
        operation()
    except Exception as exc:
        errors.append(exc)


class _BugfixClientRefreshableAuth:
    def __init__(self):
        self.access_token = "old-access"
        self.refresh_token = "old-refresh"
        self.expires = 100

    def to_dict(self):
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires": self.expires,
            "locale_code": "us",
        }


class TestProductProperties:
    def test_full_title_with_subtitle(self):
        p = make_product(title="Main", subtitle="Sub")
        assert p.full_title == "Main: Sub"

    def test_full_title_without_subtitle(self):
        p = make_product(title="Main", subtitle="")
        assert p.full_title == "Main"

    def test_hours_conversion(self):
        p = make_product(length_minutes=150)
        assert p.hours == 2.5

    def test_hours_zero(self):
        p = make_product(length_minutes=0)
        assert p.hours == 0.0

    def test_discount_pct(self):
        p = make_product(price=5.0, list_price=20.0)
        assert p.discount_pct == 75

    def test_discount_pct_no_discount(self):
        p = make_product(price=20.0, list_price=20.0)
        assert p.discount_pct == 0

    def test_discount_pct_no_price(self):
        p = make_product(price=None, list_price=20.0)
        assert p.discount_pct is None

    def test_discount_pct_no_list_price(self):
        p = make_product(price=5.0, list_price=None)
        assert p.discount_pct is None

    def test_discount_pct_zero_list_price(self):
        p = make_product(price=5.0, list_price=0.0)
        assert p.discount_pct is None

    def test_authors_str_truncates(self):
        p = make_product(authors=["A", "B", "C", "D"])
        assert p.authors_str == "A, B, C"

    def test_narrators_str_truncates(self):
        p = make_product(narrators=["N1", "N2", "N3"])
        assert p.narrators_str == "N1, N2"

    def test_url(self):
        p = make_product(asin="B00FOOBAR")
        assert p.url == "https://www.audible.com/pd/B00FOOBAR"


class TestPriceExtraction:
    def test_lowest_price(self):
        raw = {"price": {"lowest_price": {"base": 2.99}, "list_price": {"base": 15.0}}}
        assert _extract_prices(raw) == (2.99, 15.0)

    def test_falls_back_to_list_price(self):
        raw = {"price": {"list_price": {"base": 15.0}}}
        assert _extract_prices(raw) == (15.0, 15.0)

    def test_simple_numeric_price(self):
        raw = {"price": 9.99}
        assert _extract_prices(raw) == (9.99, None)

    def test_no_price(self):
        raw = {}
        assert _extract_prices(raw) == (None, None)

    def test_none_base(self):
        raw = {"price": {"lowest_price": {"base": None}, "list_price": {"base": None}}}
        assert _extract_prices(raw) == (None, None)

    def test_list_price_top_level(self):
        raw = {"list_price": 25.0}
        assert _extract_prices(raw) == (None, 25.0)


class TestParseProduct:
    def test_full_product(self, raw_api_product):
        p = parse_product(raw_api_product)
        assert p.asin == "B00RAWTEST"
        assert p.title == "Raw Title"
        assert p.subtitle == "Raw Sub"
        assert p.authors == ["Author A", "Author B"]
        assert p.narrators == ["Narrator X"]
        assert p.publisher == "Raw Publisher"
        assert p.price == 3.99
        assert p.list_price == 14.99
        assert p.length_minutes == 720
        assert p.rating == 4.5
        assert p.num_ratings == 2500
        assert "Science Fiction & Fantasy" in p.categories
        assert "cat1" in p.category_ids
        assert p.series_name == "Epic Series"
        assert p.series_position == "3"
        assert p.series_asin == "SER001"
        assert p.language == "english"
        assert p.in_plus_catalog is True

    def test_minimal_product(self, raw_api_product_minimal):
        p = parse_product(raw_api_product_minimal)
        assert p.asin == "B00MINIMAL"
        assert p.title == "Minimal"
        assert p.price is None
        assert p.authors == []
        assert p.categories == []
        assert p.in_plus_catalog is False

    def test_category_deduplication(self):
        raw = {
            "asin": "X",
            "title": "X",
            "category_ladders": [
                {
                    "ladder": [
                        {"id": "c1", "name": "Fiction"},
                        {"id": "c2", "name": "Mystery"},
                    ]
                },
                {
                    "ladder": [
                        {"id": "c1", "name": "Fiction"},
                        {"id": "c3", "name": "Thriller"},
                    ]
                },
            ],
        }
        p = parse_product(raw)
        assert p.categories.count("Fiction") == 1
        assert p.category_ids.count("c1") == 1

    def test_plus_detection_ayce(self):
        raw = {"asin": "X", "title": "X", "plans": [{"plan_name": "AYCE Monthly"}]}
        p = parse_product(raw)
        assert p.in_plus_catalog is True

    def test_rating_handles_bad_data(self):
        raw = {
            "asin": "X",
            "title": "X",
            "rating": {
                "overall_distribution": {
                    "display_average_rating": "bad",
                    "num_ratings": "bad",
                }
            },
        }
        p = parse_product(raw)
        assert p.rating == 0.0
        assert p.num_ratings == 0

    def test_null_narrators_and_authors(self):
        """Wishlist API can return null for narrators/authors instead of []."""
        raw = {"asin": "X", "title": "X", "narrators": None, "authors": None}
        p = parse_product(raw)
        assert p.narrators == []
        assert p.authors == []

    def test_null_plans_and_category_ladders(self):
        """Library API can return null for plans/category_ladders instead of []."""
        raw = {
            "asin": "X",
            "title": "X",
            "plans": None,
            "category_ladders": None,
            "series": None,
            "rating": None,
        }
        p = parse_product(raw)
        assert p.in_plus_catalog is False
        assert p.categories == []
        assert p.category_ids == []
        assert p.series_name == ""
        assert p.rating == 0.0


class TestCategoryCache:
    def test_save_and_load(self, tmp_config):
        from audible_deals.client import DealsClient

        dc = DealsClient(locale="us")
        dc.auth_file = tmp_config / "auth.json"

        cats = [{"id": "1", "name": "Fiction"}, {"id": "2", "name": "SciFi"}]
        dc._save_categories_cache(cats)

        loaded = dc._load_categories_cache()
        assert loaded == cats

    def test_expired_cache(self, tmp_config, monkeypatch):
        from audible_deals.constants import CATEGORIES_CACHE_TTL

        dc = DealsClient(locale="us")

        cats = [{"id": "1", "name": "Fiction"}]
        dc._save_categories_cache(cats)

        # Simulate stale cache by shifting time forward
        real_time = time.time
        monkeypatch.setattr(
            time, "time", lambda: real_time() + CATEGORIES_CACHE_TTL + 1
        )
        loaded = dc._load_categories_cache()
        assert loaded is None

    def test_missing_cache(self, tmp_config):
        from audible_deals.client import DealsClient

        dc = DealsClient(locale="us")
        assert dc._load_categories_cache() is None

    def test_corrupt_cache(self, tmp_config):
        from audible_deals.constants import CATEGORIES_CACHE_FILE

        cache_file = CATEGORIES_CACHE_FILE.with_suffix(".us.json")
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text("not json{{{")

        dc = DealsClient(locale="us")
        assert dc._load_categories_cache() is None


class TestResolveGenre:
    def _make_client_with_cats(self, cats):
        from audible_deals.client import DealsClient

        dc = DealsClient(locale="us")
        dc._categories_cache = cats
        return dc

    def test_exact_match(self):
        cats = [{"id": "1", "name": "Romance"}, {"id": "2", "name": "History"}]
        dc = self._make_client_with_cats(cats)
        assert dc.resolve_genre("romance") == ("1", "Romance")

    def test_alias_expansion(self):
        cats = [{"id": "1", "name": "Science Fiction & Fantasy"}]
        dc = self._make_client_with_cats(cats)
        assert dc.resolve_genre("sci-fi") == ("1", "Science Fiction & Fantasy")

    def test_substring_match(self):
        cats = [{"id": "1", "name": "Mystery, Thriller & Suspense"}]
        dc = self._make_client_with_cats(cats)
        cid, name = dc.resolve_genre("thriller")
        assert cid == "1"

    def test_ambiguous_raises(self):
        cats = [{"id": "1", "name": "Art History"}, {"id": "2", "name": "Art & Design"}]
        dc = self._make_client_with_cats(cats)
        with pytest.raises(ValueError, match="Ambiguous"):
            dc.resolve_genre("art")

    def test_no_match_raises(self):
        cats = [{"id": "1", "name": "Romance"}]
        dc = self._make_client_with_cats(cats)
        with pytest.raises(ValueError, match="No genre matching"):
            dc.resolve_genre("zzzznothing")

    def test_alias_horror(self):
        cats = [{"id": "1", "name": "Mystery, Thriller & Suspense"}]
        dc = self._make_client_with_cats(cats)
        cid, _ = dc.resolve_genre("horror")
        assert cid == "1"

    def test_alias_true_crime(self):
        cats = [{"id": "1", "name": "Mystery, Thriller & Suspense"}]
        dc = self._make_client_with_cats(cats)
        cid, _ = dc.resolve_genre("true crime")
        assert cid == "1"

    def test_alias_historical_fiction(self):
        cats = [{"id": "3", "name": "Literature & Fiction"}]
        dc = self._make_client_with_cats(cats)
        cid, _ = dc.resolve_genre("historical fiction")
        assert cid == "3"

    def test_alias_historical(self):
        cats = [{"id": "4", "name": "History"}]
        dc = self._make_client_with_cats(cats)
        cid, _ = dc.resolve_genre("historical")
        assert cid == "4"


class TestCategoryIdValidation:
    def test_valid_numeric_id(self):
        _validate_category_id("18580606011")  # should not raise

    def test_valid_alphanumeric_id(self):
        _validate_category_id("ABC123")  # should not raise

    def test_valid_with_underscore(self):
        _validate_category_id("cat_fiction")  # should not raise

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="Invalid category ID"):
            _validate_category_id("../../etc/passwd")

    def test_rejects_slash(self):
        with pytest.raises(ValueError, match="Invalid category ID"):
            _validate_category_id("cat/sub")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="Invalid category ID"):
            _validate_category_id("")

    def test_rejects_too_long(self):
        with pytest.raises(ValueError, match="Invalid category ID"):
            _validate_category_id("a" * 31)

    def test_rejects_query_injection(self):
        with pytest.raises(ValueError, match="Invalid category ID"):
            _validate_category_id("123?foo=bar")


class TestImportAuthValidation:
    def test_rejects_oversized_file(self, api):
        from audible_deals.client import DealsClient

        big_file = api.tmp_path / "big.json"
        big_file.write_text("x" * 1_100_000)

        dc = DealsClient(auth_file=api.tmp_path / "auth.json", locale="us")
        with pytest.raises(ValueError, match="too large"):
            dc.import_auth(big_file)

    def test_rejects_missing_access_token(self, api):
        from audible_deals.client import DealsClient

        src = api.tmp_path / "bad.json"
        src.write_text(json.dumps({"refresh_token": "rt"}))

        dc = DealsClient(auth_file=api.tmp_path / "auth.json", locale="us")
        with pytest.raises(ValueError, match="access_token"):
            dc.import_auth(src)

    def test_rejects_missing_refresh_token(self, api):
        from audible_deals.client import DealsClient

        src = api.tmp_path / "bad.json"
        src.write_text(json.dumps({"access_token": "at"}))

        dc = DealsClient(auth_file=api.tmp_path / "auth.json", locale="us")
        with pytest.raises(ValueError, match="refresh_token"):
            dc.import_auth(src)

    def test_rejects_invalid_locale_code(self, api):
        from audible_deals.client import DealsClient

        src = api.tmp_path / "bad.json"
        src.write_text(
            json.dumps(
                {
                    "access_token": "at",
                    "refresh_token": "rt",
                    "locale_code": "xx_invalid",
                }
            )
        )

        dc = DealsClient(auth_file=api.tmp_path / "auth.json", locale="us")
        with pytest.raises(ValueError, match="Unknown locale_code"):
            dc.import_auth(src)

    def test_accepts_valid_auth(self, api):
        from audible_deals.client import DealsClient

        src = api.tmp_path / "good.json"
        src.write_text(
            json.dumps(
                {
                    "access_token": "at",
                    "refresh_token": "rt",
                    "locale_code": "us",
                }
            )
        )

        dc = DealsClient(auth_file=api.tmp_path / "auth.json", locale="us")
        dc.import_auth(src)
        written = json.loads(dc.auth_file.read_text())
        assert written["access_token"] == "at"
        assert written["encryption"] is False

    def test_libation_rejects_missing_tokens(self, api):
        from audible_deals.client import DealsClient

        src = api.tmp_path / "libation_bad.json"
        src.write_text(
            json.dumps({"Accounts": [{"IdentityTokens": {"access_token": ""}}]})
        )

        dc = DealsClient(auth_file=api.tmp_path / "auth.json", locale="us")
        with pytest.raises(ValueError, match="Libation auth missing"):
            dc.import_auth(src)

    def test_import_sets_owner_only_directory_and_file_modes(self, tmp_path):
        import stat

        source = tmp_path / "source.json"
        source.write_text(json.dumps({"access_token": "at", "refresh_token": "rt"}))
        auth_file = tmp_path / "private" / "auth.json"

        AuthStore(auth_file, "us").import_auth(source)

        assert stat.S_IMODE(auth_file.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600

    def test_external_login_uses_callback_without_network(self, tmp_path, monkeypatch):
        import stat

        callback_calls = []
        auth_file = tmp_path / "private" / "auth.json"

        class FakeAuth:
            def to_file(self, path):
                path.write_text("saved")

        def fake_external(*, locale, login_url_callback):
            callback_calls.append((locale, login_url_callback("oauth-url")))
            return FakeAuth()

        monkeypatch.setattr("audible.Authenticator.from_login_external", fake_external)
        store = AuthStore(auth_file, "us")

        store.login_external(login_url_callback=lambda url: f"callback:{url}")

        assert callback_calls == [("us", "callback:oauth-url")]
        assert auth_file.read_text() == "saved"
        assert stat.S_IMODE(auth_file.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600

    def test_credential_login_sets_owner_only_modes(self, tmp_path, monkeypatch):
        import stat

        auth_file = tmp_path / "private" / "auth.json"
        callbacks = []

        class FakeAuth:
            def to_file(self, path):
                path.write_text("saved")

        def fake_login(username, password, *, locale, with_username, captcha_callback):
            callbacks.append(captcha_callback)
            return FakeAuth()

        monkeypatch.setattr("audible.Authenticator.from_login", fake_login)

        AuthStore(auth_file, "us").login("user", "password")

        assert callbacks == [_captcha_callback]
        assert stat.S_IMODE(auth_file.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600

    def test_captcha_callback_opens_browser_and_normalizes_answer(
        self, monkeypatch, capsys
    ):
        opened = []
        monkeypatch.setattr("audible_deals.auth_store.webbrowser.open", opened.append)
        monkeypatch.setattr("builtins.input", lambda prompt: "  AbC  ")

        answer = _captcha_callback("https://example.com/captcha")

        assert answer == "abc"
        assert opened == ["https://example.com/captcha"]
        assert "https://example.com/captcha" in capsys.readouterr().out

    def test_captcha_callback_allows_manual_open_when_browser_fails(self, monkeypatch):
        def fail_to_open(url):
            raise OSError("browser unavailable")

        monkeypatch.setattr("audible_deals.auth_store.webbrowser.open", fail_to_open)
        monkeypatch.setattr("builtins.input", lambda prompt: "answer")

        assert _captcha_callback("https://example.com/captcha") == "answer"

    def test_external_login_rejects_both_callback_modes(self, tmp_path):
        store = AuthStore(tmp_path / "auth.json", "us")

        with pytest.raises(ValueError, match="Use either"):
            store.login_external(
                callback_url_file=tmp_path / "callback.txt",
                login_url_callback=lambda url: url,
            )


class TestLibraryPagination:
    @staticmethod
    def _client(transport):
        client = object.__new__(DealsClient)
        client.locale = "us"
        client._transport = transport
        return client

    def test_reported_total_fetches_remaining_pages_concurrently_in_order(self):
        class Transport:
            def __init__(self):
                self.concurrent_pages_started = threading.Event()
                self.lock = threading.Lock()
                self.calls = []
                self.started = set()
                self.active = 0
                self.max_active = 0

            def request(self, path, **params):
                assert path == "1.0/library"
                page = params["page"]
                with self.lock:
                    self.calls.append(page)
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                try:
                    if page > 1:
                        with self.lock:
                            self.started.add(page)
                            if len(self.started) == 3:
                                self.concurrent_pages_started.set()
                        assert self.concurrent_pages_started.wait(10)
                    count = MAX_PAGE_SIZE if page < 4 else 1
                    start = (page - 1) * MAX_PAGE_SIZE
                    return {
                        "items": [
                            {"asin": f"B{start + index:09d}", "title": "Book"}
                            for index in range(count)
                        ],
                        "total_results": 3 * MAX_PAGE_SIZE + 1,
                    }
                finally:
                    with self.lock:
                        self.active -= 1

            def cancel(self):
                pass

            def reset_abort(self):
                pass

        transport = Transport()
        pages = list(self._client(transport).get_library_pages())

        assert [page for _, page in pages] == [1, 2, 3, 4]
        assert [products[0].asin for products, _ in pages] == [
            "B000000000",
            "B000000050",
            "B000000100",
            "B000000150",
        ]
        assert set(transport.calls) == {1, 2, 3, 4}
        assert transport.max_active == 3

    def test_missing_total_results_preserves_serial_pagination(self):
        class Transport:
            def __init__(self):
                self.calls = []

            def request(self, path, **params):
                page = params["page"]
                self.calls.append(page)
                count = MAX_PAGE_SIZE if page == 1 else 1
                return {
                    "items": [
                        {"asin": f"B{page:09d}{index}", "title": "Book"}
                        for index in range(count)
                    ]
                }

        transport = Transport()
        pages = list(self._client(transport).get_library_pages())

        assert [page for _, page in pages] == [1, 2]
        assert transport.calls == [1, 2]

    def test_two_page_total_avoids_executor_overhead(self, monkeypatch):
        import audible_deals.client as client_mod

        class Transport:
            def request(self, path, **params):
                page = params["page"]
                count = MAX_PAGE_SIZE if page == 1 else 1
                return {
                    "items": [
                        {"asin": f"B{page:09d}{index}", "title": "Book"}
                        for index in range(count)
                    ],
                    "total_results": MAX_PAGE_SIZE + 1,
                }

        monkeypatch.setattr(
            client_mod,
            "ThreadPoolExecutor",
            lambda *args, **kwargs: pytest.fail("two pages do not need an executor"),
        )

        pages = list(self._client(Transport()).get_library_pages())

        assert [page for _, page in pages] == [1, 2]

    def test_huge_total_results_does_not_overflow(self):
        class Transport:
            def __init__(self):
                self.cancel_count = 0

            def request(self, path, **params):
                page = params["page"]
                count = MAX_PAGE_SIZE if page == 1 else 1
                return {
                    "items": [
                        {"asin": f"B{page:09d}{index}", "title": "Book"}
                        for index in range(count)
                    ],
                    "total_results": 10**1000,
                }

            def cancel(self):
                self.cancel_count += 1

            def reset_abort(self):
                pass

        transport = Transport()
        pages = list(self._client(transport).get_library_pages())

        assert [page for _, page in pages] == [1, 2]
        assert transport.cancel_count == 1

    def test_underreported_total_continues_until_a_short_page(self):
        class Transport:
            def __init__(self):
                self.calls = []

            def request(self, path, **params):
                page = params["page"]
                self.calls.append(page)
                count = MAX_PAGE_SIZE if page < 3 else 1
                return {
                    "items": [
                        {"asin": f"B{page:09d}{index}", "title": "Book"}
                        for index in range(count)
                    ],
                    "total_results": MAX_PAGE_SIZE,
                }

            def cancel(self):
                pass

            def reset_abort(self):
                pass

        transport = Transport()
        pages = list(self._client(transport).get_library_pages())

        assert [page for _, page in pages] == [1, 2, 3]
        assert transport.calls == [1, 2, 3]

    def test_overreported_total_keeps_prefetch_bounded(self):
        class Transport:
            def __init__(self):
                self.prefetch_started = threading.Event()
                self.cancelled = threading.Event()
                self.lock = threading.Lock()
                self.calls = []
                self.started = set()
                self.cancel_count = 0
                self.reset_count = 0

            def request(self, path, **params):
                page = params["page"]
                with self.lock:
                    self.calls.append(page)
                    if page > 1:
                        self.started.add(page)
                        if len(self.started) == 4:
                            self.prefetch_started.set()
                if page > 1:
                    assert self.prefetch_started.wait(10)
                if page > 2:
                    assert self.cancelled.wait(10)
                count = MAX_PAGE_SIZE if page == 1 or page > 2 else 1
                return {
                    "items": [
                        {"asin": f"B{page:09d}{index}", "title": "Book"}
                        for index in range(count)
                    ],
                    "total_results": 10 * MAX_PAGE_SIZE,
                }

            def cancel(self):
                self.cancel_count += 1
                self.cancelled.set()

            def reset_abort(self):
                self.reset_count += 1

        transport = Transport()
        pages = list(self._client(transport).get_library_pages())

        assert [page for _, page in pages] == [1, 2]
        assert set(transport.calls) == {1, 2, 3, 4, 5}
        assert transport.cancel_count == 1
        assert transport.reset_count == 1

    def test_concurrent_page_failure_cancels_and_resets_transport(self):
        class Transport:
            def __init__(self):
                self.cancel_count = 0
                self.reset_count = 0

            def request(self, path, **params):
                page = params["page"]
                if page == 2:
                    raise click.ClickException("page failed")
                return {
                    "items": [
                        {"asin": f"B{page:09d}{index}", "title": "Book"}
                        for index in range(MAX_PAGE_SIZE)
                    ],
                    "total_results": 4 * MAX_PAGE_SIZE,
                }

            def cancel(self):
                self.cancel_count += 1

            def reset_abort(self):
                self.reset_count += 1

        transport = Transport()

        with pytest.raises(click.ClickException, match="page failed"):
            list(self._client(transport).get_library_pages())

        assert transport.cancel_count == 1
        assert transport.reset_count == 1

    def test_speculative_failure_after_short_page_is_ignored(self):
        class Transport:
            def __init__(self):
                self.later_page_failed = threading.Event()
                self.cancel_count = 0
                self.reset_count = 0

            def request(self, path, **params):
                page = params["page"]
                if page == 2:
                    assert self.later_page_failed.wait(10)
                    count = 1
                elif page == 3:
                    self.later_page_failed.set()
                    raise click.ClickException("unneeded page failed")
                else:
                    count = MAX_PAGE_SIZE
                return {
                    "items": [
                        {"asin": f"B{page:09d}{index}", "title": "Book"}
                        for index in range(count)
                    ],
                    "total_results": 10 * MAX_PAGE_SIZE,
                }

            def cancel(self):
                self.cancel_count += 1

            def reset_abort(self):
                self.reset_count += 1

        transport = Transport()
        pages = list(self._client(transport).get_library_pages())

        assert [page for _, page in pages] == [1, 2]
        assert transport.cancel_count == 1
        assert transport.reset_count == 1

    def test_closing_generator_cancels_and_resets_prefetch(self):
        class Transport:
            def __init__(self):
                self.prefetch_started = threading.Event()
                self.cancelled = threading.Event()
                self.lock = threading.Lock()
                self.calls = []
                self.started = set()
                self.cancel_count = 0
                self.reset_count = 0

            def request(self, path, **params):
                page = params["page"]
                with self.lock:
                    self.calls.append(page)
                    if page > 1:
                        self.started.add(page)
                        if len(self.started) == 4:
                            self.prefetch_started.set()
                if page > 1:
                    assert self.prefetch_started.wait(10)
                if page > 2:
                    assert self.cancelled.wait(10)
                return {
                    "items": [
                        {"asin": f"B{page:09d}{index}", "title": "Book"}
                        for index in range(MAX_PAGE_SIZE)
                    ],
                    "total_results": 10 * MAX_PAGE_SIZE,
                }

            def cancel(self):
                self.cancel_count += 1
                self.cancelled.set()

            def reset_abort(self):
                self.reset_count += 1

        transport = Transport()
        pages = self._client(transport).get_library_pages()

        assert next(pages)[1] == 1
        assert next(pages)[1] == 2
        pages.close()

        assert transport.cancel_count == 1
        assert transport.reset_count == 1
        assert set(transport.calls) <= {1, 2, 3, 4, 5}


class TestRetryAfterBackoff:
    @staticmethod
    def _make_transport(api_fixture):
        store = AuthStore(api_fixture.tmp_path / "auth.json", "us")
        return AudibleTransport(store)

    @staticmethod
    def _capture_retry_waits(monkeypatch, transport):
        """Record retry delays without actually sleeping (never aborts)."""
        sleeps = []

        def fake_wait(delay):
            sleeps.append(delay)
            return False

        monkeypatch.setattr(transport._abort, "wait", fake_wait)
        return sleeps

    def test_429_with_retry_after_sleeps_at_least_header_value(self, api, monkeypatch):
        exc = _make_429_exc(retry_after="30")
        api.get_mock.side_effect = [exc, {"products": []}]
        transport = self._make_transport(api)
        sleeps = self._capture_retry_waits(monkeypatch, transport)
        try:
            transport.request("library", num_results=1)
        finally:
            transport.close()
        assert sleeps, "expected at least one sleep"
        assert sleeps[0] >= 30

    def test_429_without_retry_after_uses_normal_delay(self, api, monkeypatch):
        exc = _make_429_exc(retry_after=None)
        api.get_mock.side_effect = [exc, {"products": []}]
        transport = self._make_transport(api)
        sleeps = self._capture_retry_waits(monkeypatch, transport)
        try:
            transport.request("library", num_results=1)
        finally:
            transport.close()
        assert sleeps, "expected at least one sleep"
        assert sleeps[0] < 30

    def test_429_retry_after_capped_at_120(self, api, monkeypatch):
        exc = _make_429_exc(retry_after="9999")
        api.get_mock.side_effect = [exc, {"products": []}]
        transport = self._make_transport(api)
        sleeps = self._capture_retry_waits(monkeypatch, transport)
        try:
            transport.request("library", num_results=1)
        finally:
            transport.close()
        assert sleeps, "expected at least one sleep"
        assert sleeps[0] <= 120

    def test_missing_auth_is_not_retried(self, tmp_path, monkeypatch):
        transport = AudibleTransport(AuthStore(tmp_path / "missing.json", "us"))
        sleeps = self._capture_retry_waits(monkeypatch, transport)

        with pytest.raises(RuntimeError, match="Not authenticated"):
            transport.request("library", num_results=1)

        assert sleeps == []

    def test_client_setup_permission_error_is_not_retried(self, api, monkeypatch):
        transport = self._make_transport(api)
        sleeps = self._capture_retry_waits(monkeypatch, transport)
        get_client = mock.Mock(side_effect=PermissionError("auth lock denied"))
        monkeypatch.setattr(transport, "_get_client", get_client)

        with pytest.raises(PermissionError, match="auth lock denied"):
            transport.request("library", num_results=1)

        assert get_client.call_count == 1
        assert api.get_mock.call_count == 0
        assert sleeps == []

    @pytest.mark.parametrize("status", [None, 500])
    def test_statusless_and_5xx_failures_retry(self, api, monkeypatch, status):
        exc = Exception("temporary")
        if status is not None:
            exc.status_code = status
        api.get_mock.side_effect = [exc, exc, {"ok": True}]
        transport = self._make_transport(api)
        sleeps = self._capture_retry_waits(monkeypatch, transport)

        try:
            assert transport.request("catalog") == {"ok": True}
        finally:
            transport.close()

        assert api.get_mock.call_count == 3
        assert len(sleeps) == 2

    def test_retry_gives_up_after_exactly_three_attempts(self, api, monkeypatch):
        api.get_mock.side_effect = Exception("still unavailable")
        transport = self._make_transport(api)
        sleeps = self._capture_retry_waits(monkeypatch, transport)

        with pytest.raises(Exception, match="still unavailable"):
            transport.request("catalog")

        assert api.get_mock.call_count == 3
        assert len(sleeps) == 2

    @pytest.mark.parametrize(
        "exc",
        [
            SimpleNamespace(status_code=400),
            click.ClickException("invalid request"),
        ],
    )
    def test_nonretryable_failures_are_immediate(self, api, monkeypatch, exc):
        if not isinstance(exc, Exception):
            error = Exception("bad request")
            error.status_code = exc.status_code
            exc = error
        api.get_mock.side_effect = exc
        transport = self._make_transport(api)
        sleeps = self._capture_retry_waits(monkeypatch, transport)

        with pytest.raises(type(exc)):
            transport.request("catalog")

        assert api.get_mock.call_count == 1
        assert sleeps == []

    def test_abort_during_backoff_then_reset_allows_reuse(self, api, monkeypatch):
        api.get_mock.side_effect = [Exception("cancelled retry"), {"ok": True}]
        transport = self._make_transport(api)
        transport.cancel()

        with pytest.raises(Exception, match="cancelled retry"):
            transport.request("catalog")

        transport.reset_abort()
        assert transport.request("catalog") == {"ok": True}
        transport.close()
        assert api.get_mock.call_count == 2

    def test_parallel_requests_create_one_lazy_client(self, api, monkeypatch):
        created = []
        start = threading.Barrier(17)
        construction_started = threading.Event()
        release_construction = threading.Event()
        fake_client = SimpleNamespace(get=lambda *args, **kwargs: {"ok": True})

        def make_client(*args, **kwargs):
            created.append((args, kwargs))
            construction_started.set()
            assert release_construction.wait(1)
            return fake_client

        monkeypatch.setattr("audible.Client", make_client)
        transport = self._make_transport(api)

        def request():
            start.wait()
            return transport.request("catalog")

        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(request) for _ in range(16)]
            start.wait()
            assert construction_started.wait(1)
            release_construction.set()
            results = [future.result() for future in futures]

        assert results == [{"ok": True}] * 16
        assert len(created) == 1


class TestTransportCloseLifecycle:
    def test_close_drains_active_get_and_waiting_request_reuses_transport(
        self, api, monkeypatch
    ):
        entered = threading.Event()
        release = threading.Event()
        order = []

        class FirstClient:
            def get(self, endpoint, **params):
                order.append("first-get-start")
                entered.set()
                assert release.wait(1)
                order.append("first-get-end")
                return {"request": "first"}

            def close(self):
                order.append("first-close")

        class SecondClient:
            def get(self, endpoint, **params):
                order.append("second-get")
                return {"request": "second"}

            def close(self):
                order.append("second-close")

        clients = iter([FirstClient(), SecondClient()])
        monkeypatch.setattr("audible.Client", lambda **kwargs: next(clients))
        transport = AudibleTransport(AuthStore(api.tmp_path / "auth.json", "us"))
        results = []

        first = threading.Thread(
            target=lambda: results.append(transport.request("first"))
        )
        first.start()
        assert entered.wait(1)

        closer = threading.Thread(target=transport.close)
        closer.start()
        assert transport._abort.wait(1)
        assert "first-close" not in order

        second = threading.Thread(
            target=lambda: results.append(transport.request("second"))
        )
        second.start()
        release.set()
        for thread in (first, closer, second):
            thread.join(1)
            assert not thread.is_alive()

        assert order[:4] == [
            "first-get-start",
            "first-get-end",
            "first-close",
            "second-get",
        ]
        assert results == [{"request": "first"}, {"request": "second"}]
        transport.close()

    def test_close_aborts_retry_sleep_then_closes(self, api, monkeypatch):
        get_called = threading.Event()
        close_called = threading.Event()

        class RetryingClient:
            def get(self, endpoint, **params):
                get_called.set()
                raise Exception("temporary")

            def close(self):
                close_called.set()

        fake_client = RetryingClient()
        monkeypatch.setattr("audible.Client", lambda **kwargs: fake_client)
        transport = AudibleTransport(AuthStore(api.tmp_path / "auth.json", "us"))
        errors = []

        request = threading.Thread(
            target=lambda: _capture_thread_error(
                errors, lambda: transport.request("catalog")
            )
        )
        request.start()
        assert get_called.wait(1)

        closer = threading.Thread(target=transport.close)
        closer.start()
        for thread in (request, closer):
            thread.join(1)
            assert not thread.is_alive()

        assert len(errors) == 1
        assert str(errors[0]) == "temporary"
        assert close_called.is_set()
        assert transport._active_requests == 0

    def test_close_failure_retains_client_and_auth_for_retry(self, api, monkeypatch):
        class FlakyCloseClient:
            def __init__(self):
                self.close_calls = 0

            def get(self, endpoint, **params):
                return {"ok": True}

            def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise OSError("close failed")

        fake_client = FlakyCloseClient()
        monkeypatch.setattr("audible.Client", lambda **kwargs: fake_client)
        store = AuthStore(api.tmp_path / "auth.json", "us")
        transport = AudibleTransport(store)
        assert transport.request("catalog") == {"ok": True}
        loaded_auth = store._authenticator

        with pytest.raises(OSError, match="close failed"):
            transport.close()

        assert transport._client is fake_client
        assert store._authenticator is loaded_auth
        assert not transport._closing
        assert not transport._abort.is_set()
        assert transport.request("catalog") == {"ok": True}

        transport.close()
        assert fake_client.close_calls == 2
        assert transport._client is None
        assert store._authenticator is None


def test_client_does_not_reexport_product_or_transport_internals():
    import audible_deals.client as client_mod

    for name in (
        "Product",
        "parse_product",
        "_extract_prices",
        "_log_request_params",
        "_retryable_status",
        "AuthStore",
        "AudibleTransport",
        "constants",
        "product",
    ):
        assert not hasattr(client_mod, name)


class TestBugfixClientNonDictCategoriesCache:
    def _write_cache(self, content: str) -> None:
        from audible_deals.constants import CATEGORIES_CACHE_FILE

        cache_file = CATEGORIES_CACHE_FILE.with_suffix(".us.json")
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(content)

    def test_list_content_is_cache_miss(self, tmp_config):
        self._write_cache(json.dumps([1, 2, 3]))
        dc = DealsClient(locale="us")
        assert dc._load_categories_cache() is None

    def test_string_content_is_cache_miss(self, tmp_config):
        self._write_cache(json.dumps("just a string"))
        dc = DealsClient(locale="us")
        assert dc._load_categories_cache() is None

    def test_number_content_is_cache_miss(self, tmp_config):
        self._write_cache(json.dumps(42))
        dc = DealsClient(locale="us")
        assert dc._load_categories_cache() is None


class TestBugfixClientWishlistFiltersIncompleteEntries:
    def _make_dc(self, api):
        return DealsClient(auth_file=api.tmp_path / "auth.json", locale="us")

    def test_drops_entries_without_asin_or_title(self, api):
        api.get_mock.return_value = {
            "products": [
                {"asin": "B001GOOD", "title": "Good Book"},
                {"asin": "", "title": "No ASIN"},
                {"asin": "B002NOTITLE", "title": ""},
                {"title": "Missing ASIN key"},
            ]
        }
        dc = self._make_dc(api)
        with dc:
            products = dc.get_wishlist()
        assert [p.asin for p in products] == ["B001GOOD"]

    def test_pagination_uses_raw_length_not_filtered(self, api):
        from audible_deals.constants import MAX_PAGE_SIZE

        # A full first page where every entry lacks an asin: filtered list is
        # empty but pagination must continue because the raw page was full.
        page0 = {"products": [{"asin": "", "title": "x"} for _ in range(MAX_PAGE_SIZE)]}
        page1 = {"products": [{"asin": "B00REAL", "title": "Real Book"}]}
        api.get_mock.side_effect = [page0, page1]
        dc = self._make_dc(api)
        with dc:
            products = dc.get_wishlist()
        assert api.get_mock.call_count == 2
        assert [p.asin for p in products] == ["B00REAL"]


class TestBugfixClientRefreshedAuthPersistence:
    def _transport(self, api, monkeypatch, auth):
        monkeypatch.setattr("audible.Authenticator.from_file", lambda *a, **kw: auth)
        store = AuthStore(api.tmp_path / "auth.json", "us")
        return store, AudibleTransport(store)

    def test_refresh_is_atomically_saved_with_owner_only_mode(self, api, monkeypatch):
        auth = _BugfixClientRefreshableAuth()

        def refresh(*args, **kwargs):
            auth.access_token = "new-access"
            auth.refresh_token = "new-refresh"
            auth.expires = 200
            return {"products": []}

        api.get_mock.side_effect = refresh
        store, transport = self._transport(api, monkeypatch, auth)

        try:
            response = transport.request("catalog")
        finally:
            transport.close()

        assert response == {"products": []}
        saved = json.loads(store.auth_file.read_text())
        assert saved["access_token"] == "new-access"
        assert saved["refresh_token"] == "new-refresh"
        assert saved["expires"] == 200
        assert stat.S_IMODE(store.auth_file.stat().st_mode) == 0o600
        assert list(store.auth_file.parent.glob(".tmp-*")) == []

    def test_unchanged_auth_is_not_rewritten(self, api, monkeypatch):
        import audible_deals.auth_store as auth_store_mod

        auth = _BugfixClientRefreshableAuth()
        original = b'{"keep": "exact bytes"}\n'
        auth_file = api.tmp_path / "auth.json"
        auth_file.write_bytes(original)
        _, transport = self._transport(api, monkeypatch, auth)
        monkeypatch.setattr(
            auth_store_mod,
            "_atomic_write_bytes",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("unchanged auth was rewritten")
            ),
        )
        api.get_mock.return_value = {"ok": True}

        try:
            assert transport.request("catalog") == {"ok": True}
        finally:
            transport.close()

        assert auth_file.read_bytes() == original

    def test_save_failure_warns_once_and_retries_on_close(
        self, api, monkeypatch, caplog
    ):
        import audible_deals.auth_store as auth_store_mod

        auth = _BugfixClientRefreshableAuth()
        real_atomic_write = auth_store_mod._atomic_write_bytes
        attempts = 0

        def flaky_save(path, content):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("disk busy")
            real_atomic_write(path, content)

        def refresh(*args, **kwargs):
            auth.access_token = "new-access"
            auth.expires = 200
            return {"ok": True}

        monkeypatch.setattr(auth_store_mod, "_atomic_write_bytes", flaky_save)
        api.get_mock.side_effect = refresh
        store, transport = self._transport(api, monkeypatch, auth)

        with caplog.at_level(logging.WARNING, logger="audible_deals.auth_store"):
            try:
                response = transport.request("catalog")
            finally:
                transport.close()

        assert response == {"ok": True}
        assert attempts == 2
        assert caplog.text.count("Could not save refreshed authentication") == 1
        assert json.loads(store.auth_file.read_text())["access_token"] == "new-access"

    def test_concurrent_auth_replacement_is_not_overwritten(
        self, api, monkeypatch, caplog
    ):
        auth = _BugfixClientRefreshableAuth()
        replacement = {
            "access_token": "login-access",
            "refresh_token": "login-refresh",
            "expires": 999,
            "locale_code": "us",
        }
        calls = 0

        def refresh(*args, **kwargs):
            nonlocal calls
            calls += 1
            auth.access_token = f"refreshed-old-user-{calls}"
            auth.expires = 100 + calls
            if calls == 1:
                (api.tmp_path / "auth.json").write_text(json.dumps(replacement))
            return {"ok": True}

        api.get_mock.side_effect = refresh
        store, transport = self._transport(api, monkeypatch, auth)

        with caplog.at_level(logging.WARNING, logger="audible_deals.auth_store"):
            try:
                first_response = transport.request("catalog")
                second_response = transport.request("catalog")
            finally:
                transport.close()

        assert first_response == {"ok": True}
        assert second_response == {"ok": True}
        assert json.loads(store.auth_file.read_text()) == replacement
        assert caplog.text.count("changed after this client loaded it") == 1

    def test_saved_fingerprint_uses_written_bytes_without_post_write_read(
        self, api, monkeypatch
    ):
        import audible_deals.auth_store as auth_store_mod

        auth = _BugfixClientRefreshableAuth()
        store, _ = self._transport(api, monkeypatch, auth)
        store.load_authenticator()
        auth.access_token = "new-access"
        real_read_bytes = type(store.auth_file).read_bytes
        written = []
        write_completed = False

        def tracked_write(path, content):
            nonlocal write_completed
            written.append(content)
            path.write_bytes(content)
            write_completed = True

        def fail_after_write(path):
            if write_completed:
                raise OSError("post-write reads forbidden")
            return real_read_bytes(path)

        monkeypatch.setattr(auth_store_mod, "_atomic_write_bytes", tracked_write)
        monkeypatch.setattr(type(store.auth_file), "read_bytes", fail_after_write)
        store.persist_refreshed_auth()

        assert not store._auth_save_pending
        assert not store._auth_persistence_disabled
        assert store._auth_file_fingerprint == hashlib.sha256(written[0]).digest()

        write_completed = False
        auth.access_token = "newer-access"
        store.persist_refreshed_auth()

        assert len(written) == 2
        assert not store._auth_save_pending
        assert not store._auth_persistence_disabled
        assert store._auth_file_fingerprint == hashlib.sha256(written[1]).digest()


def test_bugfixclient_restrictive_umask_is_process_wide_and_restores_original(
    monkeypatch,
):
    import audible_deals.auth_store as auth_store_mod

    current_umask = 0o022
    calls = []
    first_entered = threading.Event()
    second_attempted = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    def fake_umask(value):
        nonlocal current_umask
        previous = current_umask
        current_umask = value
        calls.append((value, previous))
        return previous

    monkeypatch.setattr(auth_store_mod.os, "umask", fake_umask)

    def first():
        with auth_store_mod._restrictive_umask():
            first_entered.set()
            assert release_first.wait(1)

    def second():
        assert first_entered.wait(1)
        second_attempted.set()
        with auth_store_mod._restrictive_umask():
            second_entered.set()

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    assert first_entered.wait(1)
    assert second_attempted.wait(1)
    assert not second_entered.is_set()
    release_first.set()
    for thread in (first_thread, second_thread):
        thread.join(1)
        assert not thread.is_alive()

    assert second_entered.is_set()
    assert current_umask == 0o022
    assert calls == [
        (0o177, 0o022),
        (0o022, 0o177),
        (0o177, 0o022),
        (0o022, 0o177),
    ]


class TestBugfixProductBasePriceNonNumeric:
    @pytest.mark.parametrize("bad", ["", "N/A", {"nested": 1}, [1, 2]])
    def test_base_price_returns_none_on_non_numeric(self, bad):
        assert _base_price({"base": bad}) is None

    def test_extract_prices_empty_string_base(self):
        raw = {"price": {"lowest_price": {"base": ""}}}
        assert _extract_prices(raw) == (None, None)

    def test_extract_prices_na_base(self):
        raw = {
            "price": {"lowest_price": {"base": "N/A"}, "list_price": {"base": 14.99}}
        }
        # Falls back to list_price when the sale price is unparsable.
        assert _extract_prices(raw) == (14.99, 14.99)

    def test_extract_prices_string_numeric_still_parses(self):
        raw = {"price": {"lowest_price": {"base": "12.99"}}}
        assert _extract_prices(raw) == (12.99, None)

    def test_parse_product_survives_bad_price(self):
        raw = {"asin": "X", "title": "X", "price": {"lowest_price": {"base": ""}}}
        p = parse_product(raw)
        assert p.asin == "X"
        assert p.price is None


class TestBugfixProductCategoryAlignment:
    def test_missing_name_does_not_misalign(self):
        raw = {
            "category_ladders": [
                {
                    "ladder": [
                        {"name": "", "id": "ID_A"},
                        {"name": "Mystery", "id": "ID_B"},
                    ]
                }
            ]
        }
        categories, category_ids = _extract_categories(raw)
        assert len(categories) == len(category_ids)
        pairs = dict(zip(category_ids, categories))
        # The unnamed entry is dropped (no blank genre leaks into display/stats),
        # and the named entry keeps its correct id->name pairing.
        assert "ID_A" not in pairs
        assert pairs["ID_B"] == "Mystery"

    def test_existing_dedup_behavior_preserved(self):
        raw = {
            "category_ladders": [
                {"ladder": [{"id": "c1", "name": "Fiction"}]},
                {
                    "ladder": [
                        {"id": "c1", "name": "Fiction"},
                        {"id": "c3", "name": "Thriller"},
                    ]
                },
            ]
        }
        categories, category_ids = _extract_categories(raw)
        assert category_ids.count("c1") == 1
        assert categories.count("Fiction") == 1

    def test_build_profile_keeps_correct_genre_label(self):
        raw = {
            "asin": "B1",
            "title": "Book",
            "category_ladders": [
                {
                    "ladder": [
                        {"name": "", "id": "ID_A"},
                        {"name": "Mystery", "id": "ID_B"},
                    ]
                }
            ],
        }
        p = parse_product(raw)
        profile = build_profile([p])
        genres = {g["id"]: g["name"] for g in profile["genres"]}
        # The real Mystery genre (ID_B) must be present and correctly labelled,
        # and the unnamed ID_A must not be mislabelled as Mystery.
        assert genres.get("ID_B") == "Mystery"
        assert genres.get("ID_A", "") != "Mystery"
