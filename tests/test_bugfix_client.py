"""Regression tests for confirmed bugs in audible_deals.client."""

from __future__ import annotations

import json
import logging
import stat

from audible_deals.client import DealsClient


# ===================================================================
# Bug 5: categories cache with valid-JSON-but-non-dict content must
# be treated as a cache miss, not crash with AttributeError.
# ===================================================================


class TestNonDictCategoriesCache:
    def _write_cache(self, content: str) -> None:
        from audible_deals.client import CATEGORIES_CACHE_FILE

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


# ===================================================================
# Bug 6: get_wishlist must drop entries missing asin/title, matching
# get_library_pages and get_products_batch.
# ===================================================================


class TestWishlistFiltersIncompleteEntries:
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


class _RefreshableAuth:
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


class TestRefreshedAuthPersistence:
    def _client(self, api, monkeypatch, auth):
        monkeypatch.setattr("audible.Authenticator.from_file", lambda *a, **kw: auth)
        return DealsClient(auth_file=api.tmp_path / "auth.json", locale="us")

    def test_refresh_is_atomically_saved_with_owner_only_mode(self, api, monkeypatch):
        auth = _RefreshableAuth()

        def refresh(*args, **kwargs):
            auth.access_token = "new-access"
            auth.refresh_token = "new-refresh"
            auth.expires = 200
            return {"products": []}

        api.get_mock.side_effect = refresh
        dc = self._client(api, monkeypatch, auth)

        with dc:
            response = dc._api_get("catalog")

        assert response == {"products": []}
        saved = json.loads(dc.auth_file.read_text())
        assert saved["access_token"] == "new-access"
        assert saved["refresh_token"] == "new-refresh"
        assert saved["expires"] == 200
        assert stat.S_IMODE(dc.auth_file.stat().st_mode) == 0o600
        assert list(dc.auth_file.parent.glob(".tmp-*")) == []

    def test_unchanged_auth_is_not_rewritten(self, api, monkeypatch):
        import audible_deals.client as client_mod

        auth = _RefreshableAuth()
        original = b'{"keep": "exact bytes"}\n'
        auth_file = api.tmp_path / "auth.json"
        auth_file.write_bytes(original)
        dc = self._client(api, monkeypatch, auth)
        monkeypatch.setattr(
            client_mod,
            "_atomic_write",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("unchanged auth was rewritten")
            ),
        )
        api.get_mock.return_value = {"ok": True}

        with dc:
            assert dc._api_get("catalog") == {"ok": True}

        assert auth_file.read_bytes() == original

    def test_save_failure_warns_once_and_retries_on_close(
        self, api, monkeypatch, caplog
    ):
        import audible_deals.client as client_mod

        auth = _RefreshableAuth()
        real_atomic_write = client_mod._atomic_write
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

        monkeypatch.setattr(client_mod, "_atomic_write", flaky_save)
        api.get_mock.side_effect = refresh
        dc = self._client(api, monkeypatch, auth)

        with caplog.at_level(logging.WARNING, logger="audible_deals.client"):
            with dc:
                response = dc._api_get("catalog")

        assert response == {"ok": True}
        assert attempts == 2
        assert caplog.text.count("Could not save refreshed authentication") == 1
        assert json.loads(dc.auth_file.read_text())["access_token"] == "new-access"

    def test_concurrent_auth_replacement_is_not_overwritten(
        self, api, monkeypatch, caplog
    ):
        auth = _RefreshableAuth()
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
        dc = self._client(api, monkeypatch, auth)

        with caplog.at_level(logging.WARNING, logger="audible_deals.client"):
            with dc:
                first_response = dc._api_get("catalog")
                second_response = dc._api_get("catalog")

        assert first_response == {"ok": True}
        assert second_response == {"ok": True}
        assert json.loads(dc.auth_file.read_text()) == replacement
        assert caplog.text.count("changed after this client loaded it") == 1
