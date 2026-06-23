"""Regression tests for confirmed bugs in cli/foryou.py and cli/pipeline.py."""

from __future__ import annotations

import datetime
import json

from click.testing import CliRunner

import audible_deals.constants as constants_mod
from audible_deals.cli import cli
from tests.conftest import make_product


# ===================================================================
# Bug 24: for-you --dry-run must make no API calls and no state changes
# ===================================================================


def _seed_profile_cache():
    profile = {
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "library_size": 10,
        "owned_asins": ["B00OWNED01"],
        "authors": [{"name": "Fav Author", "count": 4}],
        "narrators": [{"name": "Fav Narrator", "count": 3}],
        "genres": [{"id": "G1", "name": "Science Fiction", "count": 8}],
        "series": [{"name": "Bobiverse", "owned": 3, "series_asin": "SERIESA01"}],
    }
    constants_mod.TASTE_CACHE_FILE.write_text(json.dumps(profile))
    return profile


class TestForYouDryRunNoSideEffects:
    def test_dry_run_no_cache_does_not_fetch_or_write(self, mock_client, tmp_config):
        # No cached profile: --dry-run must not hit the library API nor write
        # the taste cache; it should error telling the user to build first.
        runner = CliRunner()
        result = runner.invoke(cli, ["for-you", "--dry-run"])
        assert result.exit_code != 0
        mock_client.get_library_pages.assert_not_called()
        assert not constants_mod.TASTE_CACHE_FILE.exists()

    def test_refresh_dry_run_does_not_fetch_or_overwrite(self, mock_client, tmp_config):
        # --refresh forces profile=None; combined with --dry-run it must still
        # make no API calls and must not overwrite the existing cache.
        seeded = _seed_profile_cache()
        runner = CliRunner()
        result = runner.invoke(cli, ["for-you", "--refresh", "--dry-run"])
        assert result.exit_code != 0
        mock_client.get_library_pages.assert_not_called()
        # The on-disk cache is untouched.
        assert json.loads(constants_mod.TASTE_CACHE_FILE.read_text()) == seeded

    def test_dry_run_with_cache_still_prints_plan(self, mock_client, tmp_config):
        _seed_profile_cache()
        runner = CliRunner()
        result = runner.invoke(cli, ["for-you", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "Bobiverse" in result.output
        mock_client.get_library_pages.assert_not_called()
        mock_client.search_pages.assert_not_called()


# ===================================================================
# Bug 21: 'vs median' badge must not depend on whether --hist-below was passed
# ===================================================================


def _seed_history(asin: str, prices: list[float]) -> None:
    """Write prior-day history entries for an ASIN (one per past day)."""
    constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today()
    entries = [
        {
            "date": (today - datetime.timedelta(days=len(prices) - i)).isoformat(),
            "price": price,
            "title": "Test Book",
        }
        for i, price in enumerate(prices)
    ]
    (constants_mod.HISTORY_DIR / f"{asin}.json").write_text(json.dumps(entries))


def _capture_hist_context(monkeypatch):
    """Patch display_products to record the hist_context it receives."""
    import audible_deals.cli.pipeline as pipeline_mod

    captured: dict[str, dict] = {}

    def fake_display_products(filtered, **kwargs):
        captured["hist_context"] = kwargs.get("hist_context")

    monkeypatch.setattr(pipeline_mod, "display_products", fake_display_products)
    return captured


class TestHistMedianBadgeFlagIndependence:
    def test_vs_median_independent_of_hist_below_flag(
        self, mock_client, tmp_config, monkeypatch
    ):
        # Exactly 2 prior on-disk entries. The 'vs median' badge requires >=3
        # entries; today's just-recorded price must be excluded (matching ATL),
        # so the badge must be absent in BOTH runs regardless of --hist-below.
        product = make_product(asin="F1", price=5.0, series_name="", series_position="")

        def reset_and_run(args):
            _seed_history("F1", [9.0, 8.0])
            mock_client.search_pages.return_value = iter([([product], 1, 1)])
            captured = _capture_hist_context(monkeypatch)
            runner = CliRunner()
            result = runner.invoke(cli, args)
            assert result.exit_code == 0, result.output
            return captured["hist_context"]

        plain = reset_and_run(["find", "--pages", "1"])
        with_flag = reset_and_run(["find", "--pages", "1", "--hist-below", "100"])

        assert plain == with_flag
        assert "F1" not in plain
