"""Configuration and support CLI behavior."""

from __future__ import annotations

import json
import subprocess as sp
import time
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

import audible_deals.config_store as config_store_mod
import audible_deals.constants as constants_mod
import audible_deals.wishlist as wishlist_mod
from audible_deals.cli import cli
from audible_deals.config_store import coerce_config_value
from audible_deals.serialization import (
    serialize_product as _serialize_product,
)
from audible_deals.settings import SettingsResolutionRequest, resolve_settings
from audible_deals.validation import validate_webhook_url
from audible_deals.validation import validate_webhook_url as _validate_webhook_url
from tests.conftest import make_product


def _resolve_settings(ctx, *, config, profile, cli_flags):
    explicit_options = {
        key
        for key in cli_flags
        if ctx.get_parameter_source(key) == click.core.ParameterSource.COMMANDLINE
    }
    return resolve_settings(
        SettingsResolutionRequest(
            config=config,
            profile=profile,
            cli_flags=cli_flags,
            explicit_options=explicit_options,
        )
    )


def _routes_run(runner, args, **kwargs):
    """Invoke the CLI and return the result; fail on unexpected errors."""
    result = runner.invoke(cli, args, catch_exceptions=False, **kwargs)
    return result


def _routes_setup_search_mock(mock_client, products):
    """Configure mock_client.search_pages to yield a single page of products."""
    mock_client.search_pages.return_value = iter([(products, 1, len(products))])


def _routes_seed_last_results(tmp_config, products):
    """Write a last_results.json cache file."""

    data = {
        "title": "Test Results",
        "results": [_serialize_product(p) for p in products],
    }
    (tmp_config / "last_results.json").write_text(json.dumps(data))


class TestSupportRegressions:
    def test_profile_save_rejects_negative_pages(self, tmp_config):
        result = CliRunner().invoke(
            cli, ["profile", "save", "bad-pages", "--pages", "-1"]
        )
        assert result.exit_code != 0
        assert "Invalid value" in result.output
        assert "bad-pages" not in config_store_mod.load_profiles()

    def test_resolved_profile_plus_flags_are_mutually_exclusive(
        self, tmp_config, monkeypatch
    ):
        import audible_deals.cli.catalog as catalog_mod

        config_store_mod.save_profiles(
            {"conflict": {"skip_plus": True, "only_plus": True}}
        )
        monkeypatch.setattr(
            catalog_mod,
            "_get_client",
            lambda locale: pytest.fail("constructed client with invalid settings"),
        )
        result = CliRunner().invoke(cli, ["find", "--profile", "conflict"])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_search_profile_genre_is_resolved_after_settings(
        self, mock_client, tmp_config
    ):
        config_store_mod.save_profiles({"genre-only": {"genre": "fantasy"}})
        mock_client.resolve_genre.return_value = ("cat1", "Fantasy")
        mock_client.search_pages.return_value = iter([([make_product()], 1, 1)])
        result = CliRunner().invoke(
            cli,
            ["search", "--profile", "genre-only", "--all-languages", "--quiet"],
        )
        assert result.exit_code == 0, result.output
        mock_client.resolve_genre.assert_called_once_with("fantasy")

    def test_search_profile_genre_dry_run_needs_no_client(
        self, tmp_config, monkeypatch
    ):
        import audible_deals.cli.catalog as catalog_mod

        config_store_mod.save_profiles({"genre-only": {"genre": "fantasy"}})
        monkeypatch.setattr(
            catalog_mod,
            "_get_client",
            lambda locale: pytest.fail("dry run constructed a client"),
        )
        result = CliRunner().invoke(
            cli, ["search", "--profile", "genre-only", "--dry-run"]
        )
        assert result.exit_code == 0, result.output
        assert "Category: fantasy" in result.output

    def test_config_max_price_per_hour_alias_round_trips(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "max-price-per-hour", "0.5"])
        assert result.exit_code == 0, result.output
        assert config_store_mod.load_config()["max_pph"] == 0.5
        help_result = runner.invoke(cli, ["config", "set", "--help"])
        assert "max-price-per-hour" in help_result.output


class TestCLIHelp:
    def test_main_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "find" in result.output
        assert "search" in result.output
        assert "compare" in result.output
        assert "wishlist" in result.output
        assert "watch" in result.output
        assert "history" in result.output

    def test_find_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--help"])
        assert result.exit_code == 0
        assert "--limit" in result.output
        assert "--quiet" in result.output
        assert "--exclude-genre" in result.output
        assert "price-per-hour" in result.output

    def test_search_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "--help"])
        assert result.exit_code == 0
        assert "--limit" in result.output
        assert "--quiet" in result.output
        assert "--exclude-genre" in result.output


class TestCompletionsCommand:
    def test_completions_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["completions", "--help"])
        assert result.exit_code == 0
        assert "bash" in result.output

    def test_completions_no_shell_invocation(self, monkeypatch):
        """Verify subprocess.run is called directly, not via /bin/sh -c."""
        import subprocess as sp

        calls = []

        def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return sp.CompletedProcess(args[0], 0, stdout="# completion", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("shutil.which", lambda _: None)

        runner = CliRunner()
        result = runner.invoke(cli, ["completions", "bash"])
        assert result.exit_code == 0
        assert len(calls) == 1
        cmd = calls[0][0][0]
        assert "/bin/sh" not in cmd
        assert "env" in calls[0][1]
        assert "_DEALS_COMPLETE" in calls[0][1]["env"]


class TestAsinValidationInCommands:
    def test_detail_rejects_path_traversal(self, tmp_config, mock_client):
        runner = CliRunner()
        result = runner.invoke(cli, ["detail", "../../../etc/passwd"])
        assert result.exit_code != 0
        assert "Invalid ASIN" in result.output

    def test_detail_accepts_valid_asin(self, mock_client, tmp_config):
        mock_client.get_product.return_value = make_product(asin="B00VALID")
        runner = CliRunner()
        result = runner.invoke(cli, ["detail", "B00VALID"])
        assert result.exit_code == 0

    def test_open_rejects_path_traversal(self, tmp_config, mock_client):
        runner = CliRunner()
        result = runner.invoke(cli, ["open", "../../etc/passwd"])
        assert result.exit_code != 0
        assert "Invalid ASIN" in result.output

    def test_compare_rejects_bad_asin(self, tmp_config, mock_client):
        runner = CliRunner()
        result = runner.invoke(cli, ["compare", "B00GOOD", "../bad"])
        assert result.exit_code != 0
        assert "Invalid ASIN" in result.output


class TestWebhookValidation:
    def test_rejects_non_http_scheme(self):
        with pytest.raises(click.BadParameter, match="http://"):
            _validate_webhook_url("ftp://example.com/hook")

    def test_rejects_no_host(self):
        with pytest.raises(click.BadParameter, match="host"):
            _validate_webhook_url("http://")

    def test_rejects_localhost(self):
        with pytest.raises(click.BadParameter, match="non-public"):
            _validate_webhook_url("http://localhost/hook")

    def test_rejects_127_0_0_1(self):
        with pytest.raises(click.BadParameter, match="non-public"):
            _validate_webhook_url("http://127.0.0.1/hook")

    def test_rejects_private_ip(self, monkeypatch):
        import socket

        monkeypatch.setattr(
            "audible_deals.validation.socket.getaddrinfo",
            lambda host, port: [(socket.AF_INET, 0, 0, "", ("10.0.0.1", 0))],
        )
        with pytest.raises(click.BadParameter, match="non-public"):
            _validate_webhook_url("https://internal.corp/hook")

    def test_rejects_link_local(self, monkeypatch):
        import socket

        monkeypatch.setattr(
            "audible_deals.validation.socket.getaddrinfo",
            lambda host, port: [(socket.AF_INET, 0, 0, "", ("169.254.169.254", 0))],
        )
        with pytest.raises(click.BadParameter, match="non-public"):
            _validate_webhook_url("https://metadata.internal/hook")

    def test_accepts_public_ip(self, monkeypatch):
        import socket

        monkeypatch.setattr(
            "audible_deals.validation.socket.getaddrinfo",
            lambda host, port: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
        )
        _validate_webhook_url("https://example.com/hook")  # should not raise

    def test_rejects_unresolvable_host(self, monkeypatch):
        import socket

        monkeypatch.setattr(
            "audible_deals.validation.socket.getaddrinfo",
            lambda host, port: (_ for _ in ()).throw(
                socket.gaierror("Name not resolved")
            ),
        )
        with pytest.raises(click.BadParameter, match="Cannot resolve"):
            _validate_webhook_url("https://nonexistent.invalid/hook")


class TestProfileSaveNewFlags:
    def test_skip_owned_in_profile(self, tmp_config):
        """profile save accepts --skip-owned and persists it."""
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "save", "myprofile", "--skip-owned"])
        assert result.exit_code == 0, result.output

        profiles = config_store_mod.load_profiles()
        assert profiles["myprofile"]["skip_owned"] is True

    def test_language_in_profile(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["profile", "save", "langprofile", "--language", "french"]
        )
        assert result.exit_code == 0, result.output

        profiles = config_store_mod.load_profiles()
        assert profiles["langprofile"]["language"] == "french"

    def test_interactive_in_profile(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "save", "iprofile", "--interactive"])
        assert result.exit_code == 0, result.output

        profiles = config_store_mod.load_profiles()
        assert profiles["iprofile"]["interactive"] is True


class TestFindProfileSkipOwned:
    def test_find_profile_skip_owned(self, mock_client, tmp_config):
        """find --profile loads skip_owned from profile and calls get_library_asins."""

        config_store_mod.save_profiles({"myp": {"skip_owned": True}})
        mock_client.search_pages.return_value = iter([([], 1, 0)])
        mock_client.get_library_asins.return_value = set()

        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--profile", "myp", "--pages", "1"])
        assert result.exit_code == 0, result.output
        mock_client.get_library_asins.assert_called_once()

    def test_find_backward_compat(self, mock_client, tmp_config):
        """Old profiles without new keys still work fine."""

        config_store_mod.save_profiles({"oldp": {"max_price": 5.0}})
        mock_client.search_pages.return_value = iter([([], 1, 0)])

        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--profile", "oldp", "--pages", "1"])
        assert result.exit_code == 0, result.output


class TestSearchWithProfile:
    def test_search_profile_applies_settings(self, mock_client, tmp_config):
        """search --profile X applies profile settings."""

        config_store_mod.save_profiles({"stest": {"min_rating": 4.5}})
        products = [
            make_product(asin="SP1", price=5.0, rating=4.8),
            make_product(asin="SP2", price=5.0, rating=3.0),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "search_profile.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "test",
                "--profile",
                "stest",
                "--pages",
                "1",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "SP1" in asins
        assert "SP2" not in asins

    def test_search_profile_not_found(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "test", "--profile", "noexist"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_search_profile_skip_owned(self, mock_client, tmp_config):
        """search --profile with skip_owned calls get_library_asins."""

        config_store_mod.save_profiles({"owned_profile": {"skip_owned": True}})
        mock_client.search_pages.return_value = iter([([], 1, 0)])
        mock_client.get_library_asins.return_value = set()

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "test",
                "--profile",
                "owned_profile",
                "--pages",
                "1",
            ],
        )
        assert result.exit_code == 0, result.output
        mock_client.get_library_asins.assert_called_once()


class TestProfileSaveMissingFlags:
    def test_skip_plus_persisted(self, tmp_config):
        """profile save --skip-plus stores skip_plus=True."""
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "save", "plustest", "--skip-plus"])
        assert result.exit_code == 0, result.output

        profiles = config_store_mod.load_profiles()
        assert profiles["plustest"]["skip_plus"] is True

    def test_exclude_keyword_persisted(self, tmp_config):
        """profile save --exclude-keyword stores the keyword in exclude_keywords."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["profile", "save", "kwtest", "--exclude-keyword", "abridged"],
        )
        assert result.exit_code == 0, result.output

        profiles = config_store_mod.load_profiles()
        assert "abridged" in profiles["kwtest"]["exclude_keywords"]

    def test_skip_plus_and_exclude_keyword_round_trip(self, tmp_config):
        """profile save --skip-plus --exclude-keyword abridged persists both keys."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "profile",
                "save",
                "combo",
                "--skip-plus",
                "--exclude-keyword",
                "abridged",
            ],
        )
        assert result.exit_code == 0, result.output

        profiles = config_store_mod.load_profiles()
        assert profiles["combo"]["skip_plus"] is True
        assert "abridged" in profiles["combo"]["exclude_keywords"]

    def test_find_profile_applies_skip_plus(self, mock_client, tmp_config):
        """find --profile applies skip_plus, excluding plus-catalog items."""

        config_store_mod.save_profiles({"skipplus": {"skip_plus": True}})
        products = [
            make_product(asin="PL1", price=5.0, in_plus_catalog=True),
            make_product(asin="PL2", price=5.0, in_plus_catalog=False),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "skip_plus_profile.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--profile",
                "skipplus",
                "--pages",
                "1",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "PL2" in asins
        assert "PL1" not in asins

    def test_search_profile_applies_exclude_keyword(self, mock_client, tmp_config):
        """search --profile applies exclude_keywords, removing matching titles."""

        config_store_mod.save_profiles(
            {"kwprofile": {"exclude_keywords": ["abridged"]}}
        )
        products = [
            make_product(asin="KW1", title="Great Book Abridged Edition", price=5.0),
            make_product(asin="KW2", title="Great Book Full", price=5.0),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "kw_profile.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "test",
                "--profile",
                "kwprofile",
                "--pages",
                "1",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "KW2" in asins
        assert "KW1" not in asins


class TestConfigCommands:
    def test_set_and_get(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "max-price", "5.0"])
        assert result.exit_code == 0, result.output
        assert "max_price" in result.output

        result = runner.invoke(cli, ["config", "get", "max-price"])
        assert result.exit_code == 0, result.output
        assert "5.0" in result.output

    def test_set_bool(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "skip-owned", "true"])
        assert result.exit_code == 0, result.output

        cfg = config_store_mod.load_config()
        assert cfg["skip_owned"] is True

    def test_set_invalid_bool(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "skip-owned", "maybe"])
        assert result.exit_code != 0
        assert "Invalid boolean" in result.output

    def test_set_invalid_key(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "nonexistent-key", "val"])
        assert result.exit_code != 0
        assert "Unknown config key" in result.output

    def test_list_empty(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "list"])
        assert result.exit_code == 0, result.output
        assert "No global defaults" in result.output

    def test_list_with_values(self, tmp_config):

        config_store_mod.save_config({"max_price": 5.0, "skip_owned": True})
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "list"])
        assert result.exit_code == 0, result.output
        assert "max_price" in result.output
        assert "skip_owned" in result.output

    def test_reset_key(self, tmp_config):

        config_store_mod.save_config({"max_price": 5.0, "min_rating": 4.0})
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "reset", "max-price"])
        assert result.exit_code == 0, result.output
        cfg = config_store_mod.load_config()
        assert "max_price" not in cfg
        assert "min_rating" in cfg

    def test_reset_all(self, tmp_config):

        config_store_mod.save_config({"max_price": 5.0, "min_rating": 4.0})
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "reset"], input="y\n")
        assert result.exit_code == 0, result.output
        cfg = config_store_mod.load_config()
        assert cfg == {}

    def test_reset_invalid_key(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "reset", "bad-key"])
        assert result.exit_code != 0
        assert "Unknown config key" in result.output

    def test_type_coercion_int(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "pages", "5"])
        assert result.exit_code == 0, result.output

        cfg = config_store_mod.load_config()
        assert cfg["pages"] == 5
        assert isinstance(cfg["pages"], int)

    def test_type_coercion_float(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "min-rating", "4.5"])
        assert result.exit_code == 0, result.output

        cfg = config_store_mod.load_config()
        assert cfg["min_rating"] == 4.5


class TestConfigAppliedToFind:
    def test_config_max_price_applies(self, mock_client, tmp_config):
        """Config max_price is applied when not passed on CLI."""

        config_store_mod.save_config({"max_price": 3.0})
        products = [
            make_product(asin="CF1", price=2.0, series_name="", series_position=""),
            make_product(asin="CF2", price=6.0, series_name="", series_position=""),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "cfg_find.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--pages",
                "1",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "CF1" in asins
        assert "CF2" not in asins

    def test_cli_flag_overrides_config(self, mock_client, tmp_config):
        """CLI --max-price overrides config max_price."""

        config_store_mod.save_config({"max_price": 2.0})
        products = [
            make_product(asin="CO1", price=4.0, series_name="", series_position=""),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        out_file = tmp_config / "cfg_override.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        # With config max_price=2, CO1 at 4.0 would be excluded. CLI overrides to 10, so included.
        asins = [d["asin"] for d in data]
        assert "CO1" in asins

    def test_profile_overrides_config(self, mock_client, tmp_config):
        """Profile min_rating overrides config min_rating."""

        config_store_mod.save_config({"min_rating": 4.0})
        config_store_mod.save_profiles({"p": {"min_rating": 3.0}})
        products = [
            make_product(
                asin="PO1", price=3.0, rating=3.5, series_name="", series_position=""
            ),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        out_file = tmp_config / "cfg_prof.json"
        runner = CliRunner()
        # With config only, PO1 (3.5) would be excluded. Profile sets 3.0, so included.
        result = runner.invoke(
            cli,
            [
                "find",
                "--profile",
                "p",
                "--pages",
                "1",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "PO1" in asins


class TestConfigBooleanOverride:
    def test_config_bool_not_overridden_when_cli_explicit(self):
        """Config booleans must not override when the user explicitly passed the flag."""
        from unittest.mock import MagicMock

        from audible_deals.cli.helpers import _CL

        ctx = MagicMock()
        ctx.get_parameter_source.return_value = _CL  # Simulate CLI explicit
        s = _resolve_settings(
            ctx,
            config={"on_sale": True, "deep": True},
            profile=None,
            cli_flags={"on_sale": False, "deep": False},
        )
        assert s.on_sale is False
        assert s.deep is False

    def test_config_bool_applied_when_not_cli(self):
        """Config booleans should apply when user did NOT pass the flag."""
        from unittest.mock import MagicMock

        import click

        ctx = MagicMock()
        ctx.get_parameter_source.return_value = click.core.ParameterSource.DEFAULT
        s = _resolve_settings(
            ctx,
            config={"on_sale": True, "deep": True},
            profile=None,
            cli_flags={"on_sale": False, "deep": False},
        )
        assert s.on_sale is True
        assert s.deep is True


class TestLanguagePrecedence:
    def test_cli_all_languages_overrides_config_language(self):
        s = resolve_settings(
            SettingsResolutionRequest(
                config={"language": "french"},
                profile=None,
                cli_flags={"language": "", "all_languages": True},
                explicit_options={"all_languages"},
            )
        )

        assert s.language == ""
        assert s.all_languages is True

    def test_cli_language_overrides_config_all_languages(self):
        s = resolve_settings(
            SettingsResolutionRequest(
                config={"all_languages": True},
                profile=None,
                cli_flags={"language": "spanish", "all_languages": False},
                explicit_options={"language"},
            )
        )

        assert s.language == "spanish"
        assert s.all_languages is False

    def test_profile_language_overrides_config_all_languages(self):
        s = resolve_settings(
            SettingsResolutionRequest(
                config={"all_languages": True},
                profile={"language": "french"},
                cli_flags={"language": "", "all_languages": False},
            )
        )

        assert s.language == "french"
        assert s.all_languages is False

    def test_same_source_language_conflict_is_rejected(self):
        with pytest.raises(ValueError, match="cannot both be enabled"):
            resolve_settings(
                SettingsResolutionRequest(
                    config={"language": "french", "all_languages": True},
                    profile=None,
                    cli_flags={"language": "", "all_languages": False},
                )
            )

    def test_config_bool_false_applied_when_not_cli(self):
        """Config with explicit False should set ns to False when source is DEFAULT."""
        from unittest.mock import MagicMock

        import click

        ctx = MagicMock()
        ctx.get_parameter_source.return_value = click.core.ParameterSource.DEFAULT
        s = _resolve_settings(
            ctx,
            config={"on_sale": False, "deep": False},
            profile=None,
            cli_flags={"on_sale": True, "deep": True},
        )
        assert s.on_sale is False
        assert s.deep is False

    def test_profile_bool_not_overridden_when_cli_explicit(self):
        """Profile booleans must not override when the user explicitly passed the flag."""
        from unittest.mock import MagicMock

        from audible_deals.cli.helpers import _CL

        ctx = MagicMock()
        ctx.get_parameter_source.return_value = _CL
        s = _resolve_settings(
            ctx,
            config={},
            profile={"on_sale": True, "deep": True},
            cli_flags={"on_sale": False, "deep": False},
        )
        assert s.on_sale is False
        assert s.deep is False

    def test_profile_bool_applied_when_not_cli(self):
        """Profile booleans should apply when user did NOT pass the flag."""
        from unittest.mock import MagicMock

        import click

        ctx = MagicMock()
        ctx.get_parameter_source.return_value = click.core.ParameterSource.DEFAULT
        s = _resolve_settings(
            ctx,
            config={},
            profile={"on_sale": True, "deep": True},
            cli_flags={"on_sale": False, "deep": False},
        )
        assert s.on_sale is True
        assert s.deep is True


class TestVersionFlag:
    def test_version_flag(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "deals" in result.output
        # version string should be present (e.g. "deals, version X.Y.Z")
        assert "version" in result.output.lower()


class TestBareInvocation:
    def test_bare_invocation_exits_zero(self):
        runner = CliRunner()
        result = runner.invoke(cli, [])
        assert result.exit_code == 0

    def test_bare_invocation_shows_dashboard(self):
        runner = CliRunner()
        result = runner.invoke(cli, [])
        assert "marketplace:" in result.output

    def test_bare_invocation_shows_reference_hint(self):
        runner = CliRunner()
        result = runner.invoke(cli, [])
        assert "deals --help" in result.output


class TestProfileShow:
    def test_profile_show_displays_flags(self, tmp_config):

        config_store_mod.save_profiles(
            {
                "myprofile": {
                    "genre": "sci-fi",
                    "max_price": 5.0,
                    "min_rating": 4.0,
                    "on_sale": True,
                }
            }
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "show", "myprofile"])
        assert result.exit_code == 0, result.output
        assert "myprofile" in result.output
        assert "sci-fi" in result.output
        assert "5.0" in result.output

    def test_profile_show_not_found(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "show", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_profile_show_list_values(self, tmp_config):

        config_store_mod.save_profiles(
            {
                "multi": {
                    "exclude_authors": ["Andy Weir", "Brandon Sanderson"],
                }
            }
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "show", "multi"])
        assert result.exit_code == 0, result.output
        assert "Andy Weir" in result.output
        assert "Brandon Sanderson" in result.output

    def test_profile_show_bool_true_displayed(self, tmp_config):
        """Boolean True values should show as --flag."""

        config_store_mod.save_profiles(
            {
                "booltest": {
                    "deep": True,
                }
            }
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "show", "booltest"])
        assert result.exit_code == 0, result.output
        assert "deep" in result.output

    def test_profile_show_bool_false_displayed_as_no_flag(self, tmp_config):
        """Boolean False values should display as --no-flag."""

        config_store_mod.save_profiles(
            {
                "falsetest": {
                    "deep": False,
                    "on_sale": False,
                }
            }
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "show", "falsetest"])
        assert result.exit_code == 0, result.output
        assert "False" not in result.output
        assert "--no-deep" in result.output
        assert "--no-on-sale" in result.output


class TestProfileShowSingularFlags:
    def test_exclude_authors_shows_as_exclude_author(self, tmp_config):
        """profile show renders exclude_authors as --exclude-author (singular)."""

        config_store_mod.save_profiles(
            {"myp": {"exclude_authors": ["Andy Weir", "Terry Brooks"]}}
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "show", "myp"])
        assert result.exit_code == 0, result.output
        assert "--exclude-author" in result.output
        assert "--exclude-authors" not in result.output

    def test_exclude_narrators_shows_as_exclude_narrator(self, tmp_config):
        """profile show renders exclude_narrators as --exclude-narrator (singular)."""

        config_store_mod.save_profiles({"myp2": {"exclude_narrators": ["R.C. Bray"]}})
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "show", "myp2"])
        assert result.exit_code == 0, result.output
        assert "--exclude-narrator" in result.output
        assert "--exclude-narrators" not in result.output

    def test_other_keys_still_hyphenated(self, tmp_config):
        """profile show still hyphenates other underscore keys correctly."""

        config_store_mod.save_profiles(
            {"myp3": {"min_rating": 4.0, "first_in_series": True}}
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["profile", "show", "myp3"])
        assert result.exit_code == 0, result.output
        assert "--min-rating" in result.output
        assert "--first-in-series" in result.output


class TestConfigResetConfirmation:
    def test_reset_all_confirmed(self, tmp_config):
        """config reset with no key clears config when user confirms."""

        config_store_mod.save_config({"max_price": 5.0, "min_rating": 4.0})
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "reset"], input="y\n")
        assert result.exit_code == 0, result.output
        assert "All global defaults cleared" in result.output
        assert config_store_mod.load_config() == {}

    def test_reset_all_cancelled(self, tmp_config):
        """config reset with no key leaves config intact when user cancels."""

        config_store_mod.save_config({"max_price": 5.0})
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "reset"], input="n\n")
        assert result.exit_code == 0, result.output
        assert "Cancelled" in result.output
        assert config_store_mod.load_config() == {"max_price": 5.0}

    def test_reset_key_no_confirmation_needed(self, tmp_config):
        """config reset KEY skips the confirmation prompt."""

        config_store_mod.save_config({"max_price": 5.0, "min_rating": 4.0})
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "reset", "max-price"])
        assert result.exit_code == 0, result.output
        cfg = config_store_mod.load_config()
        assert "max_price" not in cfg
        assert "min_rating" in cfg


class TestProfileSaveFalsy:
    def test_profile_save_preserves_zero_max_price(self, tmp_config, monkeypatch):
        """profile save preserves max_price=0.0 (falsy but valid)."""

        runner = CliRunner()
        result = runner.invoke(
            cli, ["profile", "save", "zeroprofile", "--max-price", "0"]
        )
        assert result.exit_code == 0, result.output
        profiles = config_store_mod.load_profiles()
        assert "max_price" in profiles["zeroprofile"]
        assert profiles["zeroprofile"]["max_price"] == 0.0

    def test_profile_save_drops_false_flags(self, tmp_config, monkeypatch):
        """profile save drops False boolean flags (they are always defaults, not explicit choices)."""

        runner = CliRunner()
        result = runner.invoke(
            cli, ["profile", "save", "falseprofile", "--genre", "sci-fi"]
        )
        assert result.exit_code == 0, result.output
        profiles = config_store_mod.load_profiles()
        # on_sale=False is NOT stored — profile save's is_flag options only capture True
        assert "on_sale" not in profiles["falseprofile"]

    def test_profile_save_drops_empty(self, tmp_config, monkeypatch):
        """profile save drops empty strings and empty tuples but not zero."""

        runner = CliRunner()
        result = runner.invoke(
            cli, ["profile", "save", "emptyprofile", "--max-price", "0"]
        )
        assert result.exit_code == 0, result.output
        profiles = config_store_mod.load_profiles()
        # Empty string fields like genre, author etc. should not be saved
        assert "genre" not in profiles["emptyprofile"]
        assert "author" not in profiles["emptyprofile"]
        assert "narrator" not in profiles["emptyprofile"]
        # But max_price=0.0 should be saved
        assert profiles["emptyprofile"]["max_price"] == 0.0


class TestProfileSaveZeroDefaults:
    def test_profile_save_omits_zero_defaults(self, tmp_config):
        """profile save --genre sci-fi must NOT save min_rating=0.0 etc."""
        from audible_deals.config_store import load_profiles as _load_profiles

        runner = CliRunner()
        result = runner.invoke(
            cli, ["profile", "save", "zerotest", "--genre", "sci-fi"]
        )
        assert result.exit_code == 0, result.output
        profiles = _load_profiles()
        assert "genre" in profiles["zerotest"]
        assert profiles["zerotest"]["genre"] == "sci-fi"
        assert "min_rating" not in profiles["zerotest"]
        assert "min_ratings" not in profiles["zerotest"]
        assert "min_hours" not in profiles["zerotest"]

    def test_profile_save_preserves_explicit_zero(self, tmp_config):
        """profile save --max-price 0 must keep max_price=0.0."""
        from audible_deals.config_store import load_profiles as _load_profiles

        runner = CliRunner()
        result = runner.invoke(
            cli, ["profile", "save", "zeroexplicit", "--max-price", "0"]
        )
        assert result.exit_code == 0, result.output
        profiles = _load_profiles()
        assert profiles["zeroexplicit"]["max_price"] == 0.0


class TestProfileSaveNewOptions:
    def test_profile_save_min_discount(self, tmp_config):
        """profile save --min-discount should persist."""
        from audible_deals.config_store import load_profiles as _load_profiles

        runner = CliRunner()
        result = runner.invoke(
            cli, ["profile", "save", "disctest", "--min-discount", "50"]
        )
        assert result.exit_code == 0, result.output
        profiles = _load_profiles()
        assert profiles["disctest"]["min_discount"] == 50

    def test_profile_save_max_pph(self, tmp_config):
        """profile save --max-price-per-hour should persist."""
        from audible_deals.config_store import load_profiles as _load_profiles

        runner = CliRunner()
        result = runner.invoke(
            cli, ["profile", "save", "pphtest", "--max-price-per-hour", "0.5"]
        )
        assert result.exit_code == 0, result.output
        profiles = _load_profiles()
        assert profiles["pphtest"]["max_pph"] == 0.5

    def test_profile_save_publisher(self, tmp_config):
        """profile save --publisher should persist."""
        from audible_deals.config_store import load_profiles as _load_profiles

        runner = CliRunner()
        result = runner.invoke(
            cli, ["profile", "save", "pubtest", "--publisher", "Penguin"]
        )
        assert result.exit_code == 0, result.output
        profiles = _load_profiles()
        assert profiles["pubtest"]["publisher"] == "Penguin"


class TestStringKeyPrecedence:
    """Profile string keys must override config string keys (CLI > Profile > Config)."""

    def test_profile_string_overrides_config(self):
        """When config set a string, profile must override it."""
        from unittest.mock import MagicMock

        import click

        ctx = MagicMock()
        ctx.get_parameter_source.return_value = click.core.ParameterSource.DEFAULT
        cfg = {"language": "english", "narrator": "Alice"}
        profile = {"language": "french", "narrator": "Bob"}
        cli_flags = {
            "language": "",
            "narrator": "",
            "author": "",
            "series": "",
            "publisher": "",
        }

        s = _resolve_settings(ctx, config=cfg, profile=None, cli_flags=cli_flags)
        assert s.language == "english"
        assert s.narrator == "Alice"

        s2 = _resolve_settings(ctx, config=cfg, profile=profile, cli_flags=cli_flags)
        assert s2.language == "french"
        assert s2.narrator == "Bob"

    def test_config_string_applied_when_no_profile(self):
        """Config string fills ns when CLI absent and no profile override."""
        from unittest.mock import MagicMock

        import click

        ctx = MagicMock()
        ctx.get_parameter_source.return_value = click.core.ParameterSource.DEFAULT
        cfg = {"language": "french", "narrator": "Alice"}
        s = _resolve_settings(
            ctx, config=cfg, profile=None, cli_flags={"language": "", "narrator": ""}
        )
        assert s.language == "french"
        assert s.narrator == "Alice"

    def test_cli_string_overrides_both(self):
        """CLI-supplied string must not be overridden by config or profile."""
        from unittest.mock import MagicMock

        from audible_deals.cli.helpers import _CL

        ctx = MagicMock()
        ctx.get_parameter_source.return_value = _CL
        cfg = {"language": "english", "narrator": "Alice"}
        profile = {"language": "french", "narrator": "Bob"}
        cli_flags = {"language": "spanish", "narrator": "Carlos"}

        s = _resolve_settings(ctx, config=cfg, profile=profile, cli_flags=cli_flags)
        assert s.language == "spanish"
        assert s.narrator == "Carlos"

    def test_profile_only_keys_applied(self):
        """Profile-only string keys (genre, keywords) are applied when CLI absent."""
        from unittest.mock import MagicMock

        import click

        ctx = MagicMock()
        ctx.get_parameter_source.return_value = click.core.ParameterSource.DEFAULT
        profile = {"genre": "mystery", "keywords": "thriller"}
        cli_flags = {
            "genre": "",
            "keywords": "",
            "exclude_genre": (),
            "exclude_authors": (),
        }
        s = _resolve_settings(ctx, config={}, profile=profile, cli_flags=cli_flags)
        assert s.genre == "mystery"
        assert s.keywords == "thriller"


class TestDoctorCommand:
    def _patch_auth(self, monkeypatch, tmp_config):
        """Redirect AUTH_FILE in cli module to tmp_config."""

        monkeypatch.setattr(constants_mod, "AUTH_FILE", tmp_config / "auth.json")
        monkeypatch.setattr(constants_mod, "CONFIG_DIR", tmp_config)

    def test_auth_file_missing_fails(self, tmp_config, mock_client, monkeypatch):
        self._patch_auth(monkeypatch, tmp_config)
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        assert "FAIL" in result.output
        assert result.exit_code == 1

    def test_auth_file_expired_refreshes(self, tmp_config, mock_client, monkeypatch):
        import time

        self._patch_auth(monkeypatch, tmp_config)
        auth_file = tmp_config / "auth.json"
        auth_file.write_text(
            json.dumps(
                {
                    "access_token": "at",
                    "refresh_token": "rt",
                    "locale_code": "us",
                    "expires": time.time() - 3600,
                }
            )
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        assert "WARN" in result.output
        assert "automatic refresh" in result.output
        assert result.exit_code == 0
        mock_client.check_connection.assert_called_once_with()

    def test_auth_file_expires_soon_warns(self, tmp_config, mock_client, monkeypatch):
        import time

        self._patch_auth(monkeypatch, tmp_config)
        auth_file = tmp_config / "auth.json"
        auth_file.write_text(
            json.dumps(
                {
                    "access_token": "at",
                    "refresh_token": "rt",
                    "locale_code": "us",
                    "expires": time.time() + 3600,
                }
            )
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        assert "WARN" in result.output

    def test_all_checks_pass(self, tmp_config, mock_client, monkeypatch):
        import time

        self._patch_auth(monkeypatch, tmp_config)
        auth_file = tmp_config / "auth.json"
        auth_file.write_text(
            json.dumps(
                {
                    "access_token": "at",
                    "refresh_token": "rt",
                    "locale_code": "us",
                    "expires": time.time() + 86400 * 30,
                }
            )
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0
        assert "PASS" in result.output

    def test_marketplace_check_skipped_when_auth_fails(
        self, tmp_config, mock_client, monkeypatch
    ):
        self._patch_auth(monkeypatch, tmp_config)
        runner = CliRunner()
        runner.invoke(cli, ["doctor"])
        mock_client.check_connection.assert_not_called()


class TestDoctorUnknownKeys:
    def _patch_auth(self, monkeypatch, tmp_config):

        monkeypatch.setattr(constants_mod, "AUTH_FILE", tmp_config / "auth.json")
        monkeypatch.setattr(constants_mod, "CONFIG_DIR", tmp_config)

    def test_unknown_config_key_warns(self, tmp_config, mock_client, monkeypatch):
        """A config.json key absent from _CONFIG_SCHEMA triggers a WARN row."""

        self._patch_auth(monkeypatch, tmp_config)
        constants_mod.CONFIG_FILE.write_text(
            json.dumps({"max_price": 5.0, "typo_key": True, "another_bad": 1})
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        assert "WARN" in result.output
        assert "Unknown config keys" in result.output
        assert "typo_key" in result.output
        assert "another_bad" in result.output

    def test_no_unknown_config_keys_passes(self, tmp_config, mock_client, monkeypatch):
        """All known keys → PASS for unknown-config-keys check."""

        self._patch_auth(monkeypatch, tmp_config)
        constants_mod.CONFIG_FILE.write_text(json.dumps({"max_price": 5.0}))
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        assert "Unknown config keys" in result.output
        assert result.output.count("FAIL") == 1  # only auth FAIL

    def test_no_config_file_skips_unknown_key_check(
        self, tmp_config, mock_client, monkeypatch
    ):
        """No config.json → unknown-config-key row is absent."""
        self._patch_auth(monkeypatch, tmp_config)
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        assert "Unknown config keys" not in result.output

    def test_unknown_profile_key_warns(self, tmp_config, mock_client, monkeypatch):
        """A profile with a key outside the combined allowed set triggers WARN."""

        self._patch_auth(monkeypatch, tmp_config)
        constants_mod.PROFILES_FILE.write_text(
            json.dumps({"myprofile": {"max_price": 5.0, "ghost_key": "x"}})
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        assert "Unknown profile keys" in result.output
        assert "WARN" in result.output
        assert "ghost_key" in result.output

    def test_no_profiles_file_skips_profile_key_check(
        self, tmp_config, mock_client, monkeypatch
    ):
        """No profiles.json → unknown-profile-key row is absent entirely."""
        self._patch_auth(monkeypatch, tmp_config)
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        assert "Unknown profile keys" not in result.output

    def test_notify_state_malformed_entry_warns(
        self, tmp_config, mock_client, monkeypatch
    ):
        """A notify_state.json with a non-dict entry triggers WARN."""

        self._patch_auth(monkeypatch, tmp_config)
        constants_mod.NOTIFY_STATE_FILE.write_text(
            json.dumps(
                {"B001": "not-a-dict", "B002": {"date": "2025-01-01", "price": 3.99}}
            )
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        assert "Notify-state health" in result.output
        assert "WARN" in result.output
        assert "malformed" in result.output

    def test_notify_state_stale_entry_warns(self, tmp_config, mock_client, monkeypatch):
        """A notify_state.json entry with a date >365 days old triggers WARN."""

        self._patch_auth(monkeypatch, tmp_config)
        constants_mod.NOTIFY_STATE_FILE.write_text(
            json.dumps({"B003": {"date": "2000-01-01", "price": 1.99}})
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        assert "Notify-state health" in result.output
        assert "WARN" in result.output
        assert "stale" in result.output

    def test_notify_state_healthy_passes(self, tmp_config, mock_client, monkeypatch):
        """A notify_state.json with a recent valid entry shows PASS."""
        import datetime as dt

        self._patch_auth(monkeypatch, tmp_config)
        recent = dt.date.today().isoformat()
        constants_mod.NOTIFY_STATE_FILE.write_text(
            json.dumps({"B004": {"date": recent, "price": 2.99}})
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        assert "Notify-state health" in result.output
        assert "1 suppressed ASIN(s) tracked" in result.output

    def test_notify_state_empty_passes(self, tmp_config, mock_client, monkeypatch):
        """An empty notify_state.json shows PASS with 'No entries'."""

        self._patch_auth(monkeypatch, tmp_config)
        constants_mod.NOTIFY_STATE_FILE.write_text(json.dumps({}))
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        assert "Notify-state health" in result.output
        assert "No entries" in result.output

    def test_corrupt_config_json_fails(self, tmp_config, mock_client, monkeypatch):
        """doctor reports FAIL for Config file valid when config.json is corrupt."""

        self._patch_auth(monkeypatch, tmp_config)
        constants_mod.CONFIG_FILE.write_text("not valid json {{{")
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        assert "Config file valid" in result.output
        assert "FAIL" in result.output

    def test_non_dict_config_json_fails(self, tmp_config, mock_client, monkeypatch):
        """doctor reports FAIL for Config file valid when config.json is not a JSON object."""

        self._patch_auth(monkeypatch, tmp_config)
        constants_mod.CONFIG_FILE.write_text(json.dumps([1, 2, 3]))
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        assert "Config file valid" in result.output
        assert "FAIL" in result.output


class TestCreditPriceConfig:
    def test_set_and_get(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "credit-price", "11.25"])
        assert result.exit_code == 0, result.output
        cfg = config_store_mod.load_config()
        assert cfg["credit_price"] == 11.25
        assert isinstance(cfg["credit_price"], float)

    def test_invalid_value_rejected(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "credit-price", "cheap"])
        assert result.exit_code != 0


def test_corrupt_numeric_config_allows_doctor_and_recovery(tmp_config, mock_client):
    constants_mod.CONFIG_FILE.write_text('{"max_price": NaN}')
    runner = CliRunner()

    doctor = runner.invoke(cli, ["doctor"])
    assert doctor.exit_code == 1
    assert "Config file valid" in doctor.output
    assert "max_price" in doctor.output
    assert "finite" in doctor.output

    listed = runner.invoke(cli, ["config", "list"])
    assert listed.exit_code == 0, listed.output

    reset = runner.invoke(cli, ["config", "reset", "max-price"])
    assert reset.exit_code == 0, reset.output
    assert config_store_mod.load_config() == {}


def test_corrupt_numeric_config_blocks_discovery_before_client(tmp_config, monkeypatch):
    constants_mod.CONFIG_FILE.write_text('{"min_hours": Infinity}')
    import audible_deals.cli.catalog as catalog_mod

    monkeypatch.setattr(
        catalog_mod,
        "_get_client",
        lambda locale: pytest.fail("invalid config constructed a client"),
    )

    result = CliRunner().invoke(cli, ["find", "--dry-run"])
    assert result.exit_code != 0
    assert "min_hours" in result.output
    assert "finite" in result.output


def test_corrupt_credit_price_blocks_catalog_before_client(tmp_config, monkeypatch):
    constants_mod.CONFIG_FILE.write_text('{"credit_price": NaN}')
    import audible_deals.cli.catalog as catalog_mod

    monkeypatch.setattr(
        catalog_mod,
        "_get_client",
        lambda locale: pytest.fail("invalid credit price constructed a client"),
    )

    result = CliRunner().invoke(cli, ["find", "--pages", "1"])
    assert result.exit_code != 0
    assert "credit_price" in result.output
    assert "finite" in result.output


class TestRoutesRootGroup:
    def test_help(self, tmp_config):
        result = _routes_run(CliRunner(), ["--help"])
        assert result.exit_code == 0
        assert "Audible deal finder" in result.output

    def test_version(self, tmp_config):
        result = _routes_run(CliRunner(), ["--version"])
        assert result.exit_code == 0

    def test_no_subcommand_shows_dashboard(self, tmp_config):
        result = _routes_run(CliRunner(), [])
        assert result.exit_code == 0
        assert "Authentication is not set up" in result.output
        assert "deals login" in result.output


class TestRoutesAuthCommands:
    def test_login_help(self, tmp_config):
        result = _routes_run(CliRunner(), ["login", "--help"])
        assert result.exit_code == 0
        assert "--external" in result.output

    def test_import_auth_help(self, tmp_config):
        result = _routes_run(CliRunner(), ["import-auth", "--help"])
        assert result.exit_code == 0
        assert "audible-cli" in result.output


class TestRoutesOpenCommand:
    def test_open_by_asin(self, tmp_config, mock_client):
        with patch("audible_deals.cli.misc.click.launch") as mock_launch:
            result = _routes_run(CliRunner(), ["open", "B00OPEN01"])
            assert result.exit_code == 0
            mock_launch.assert_called_once()
            assert "audible.com" in mock_launch.call_args[0][0]

    def test_open_by_last_ref(self, tmp_config, mock_client):
        products = [make_product(asin="B00OPEN02")]
        _routes_seed_last_results(tmp_config, products)
        with patch("audible_deals.cli.misc.click.launch") as mock_launch:
            result = _routes_run(CliRunner(), ["open", "--last", "1"])
            assert result.exit_code == 0
            mock_launch.assert_called_once()


class TestRoutesProfileCommands:
    def test_profile_save(self, tmp_config, mock_client):
        result = _routes_run(
            CliRunner(),
            [
                "profile",
                "save",
                "my-scifi",
                "--genre",
                "sci-fi",
                "--max-price",
                "5",
                "--first-in-series",
            ],
        )
        assert result.exit_code == 0
        assert "my-scifi" in result.output

    def test_profile_list(self, tmp_config, mock_client):
        profiles = {"test-prof": {"genre": "sci-fi", "max_price": 5.0}}
        (tmp_config / "profiles.json").write_text(json.dumps(profiles))
        result = _routes_run(CliRunner(), ["profile", "list"])
        assert result.exit_code == 0
        assert "test-prof" in result.output

    def test_profile_show(self, tmp_config, mock_client):
        profiles = {"show-prof": {"genre": "mystery", "on_sale": True}}
        (tmp_config / "profiles.json").write_text(json.dumps(profiles))
        result = _routes_run(CliRunner(), ["profile", "show", "show-prof"])
        assert result.exit_code == 0
        assert "mystery" in result.output

    def test_profile_delete(self, tmp_config, mock_client):
        profiles = {"del-prof": {"genre": "romance"}}
        (tmp_config / "profiles.json").write_text(json.dumps(profiles))
        result = _routes_run(CliRunner(), ["profile", "delete", "del-prof"])
        assert result.exit_code == 0
        assert "deleted" in result.output.lower()

    def test_profile_list_empty(self, tmp_config, mock_client):
        result = _routes_run(CliRunner(), ["profile", "list"])
        assert result.exit_code == 0

    def test_profile_delete_nonexistent(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["profile", "delete", "nope"])
        assert result.exit_code != 0


class TestRoutesConfigCommands:
    def test_config_set(self, tmp_config, mock_client):
        result = _routes_run(CliRunner(), ["config", "set", "max-price", "5"])
        assert result.exit_code == 0
        assert "max_price" in result.output

    def test_config_get(self, tmp_config, mock_client):
        cfg = {"max_price": 5.0}
        (tmp_config / "config.json").write_text(json.dumps(cfg))
        result = _routes_run(CliRunner(), ["config", "get", "max-price"])
        assert result.exit_code == 0
        assert "5.0" in result.output

    def test_config_get_unset(self, tmp_config, mock_client):
        result = _routes_run(CliRunner(), ["config", "get", "max-price"])
        assert result.exit_code == 0
        assert "not set" in result.output

    def test_config_list(self, tmp_config, mock_client):
        cfg = {"max_price": 5.0, "skip_owned": True}
        (tmp_config / "config.json").write_text(json.dumps(cfg))
        result = _routes_run(CliRunner(), ["config", "list"])
        assert result.exit_code == 0
        assert "max_price" in result.output

    def test_config_list_empty(self, tmp_config, mock_client):
        result = _routes_run(CliRunner(), ["config", "list"])
        assert result.exit_code == 0

    def test_config_reset_key(self, tmp_config, mock_client):
        cfg = {"max_price": 5.0}
        (tmp_config / "config.json").write_text(json.dumps(cfg))
        result = _routes_run(CliRunner(), ["config", "reset", "max-price"])
        assert result.exit_code == 0
        assert "removed" in result.output.lower()

    def test_config_set_bool(self, tmp_config, mock_client):
        result = _routes_run(CliRunner(), ["config", "set", "skip-owned", "true"])
        assert result.exit_code == 0

    def test_config_set_invalid_key(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["config", "set", "bad-key", "5"])
        assert result.exit_code != 0


class TestRoutesCompletionsCommand:
    def test_completions_bash(self, tmp_config):
        result = _routes_run(CliRunner(), ["completions", "bash"])
        assert result.exit_code == 0

    def test_completions_zsh(self, tmp_config):
        result = _routes_run(CliRunner(), ["completions", "zsh"])
        assert result.exit_code == 0

    def test_completions_fish(self, tmp_config):
        result = _routes_run(CliRunner(), ["completions", "fish"])
        assert result.exit_code == 0


class TestRoutesLocaleSupport:
    def test_locale_uk(self, tmp_config, mock_client):
        products = [make_product(asin="UK01", price=3.99)]
        _routes_setup_search_mock(mock_client, products)
        result = _routes_run(CliRunner(), ["--locale", "uk", "search", "test"])
        assert result.exit_code == 0

    def test_locale_de(self, tmp_config, mock_client):
        products = [make_product(asin="DE01", price=3.99)]
        _routes_setup_search_mock(mock_client, products)
        result = _routes_run(CliRunner(), ["--locale", "de", "search", "test"])
        assert result.exit_code == 0


class TestRoutesErrorPaths:
    def test_search_genre_and_category_conflict(self, tmp_config, mock_client):
        result = CliRunner().invoke(
            cli, ["search", "test", "--genre", "sci-fi", "--category", "cat1"]
        )
        assert result.exit_code != 0

    def test_find_genre_and_category_conflict(self, tmp_config, mock_client):
        result = CliRunner().invoke(
            cli, ["find", "--genre", "sci-fi", "--category", "cat1"]
        )
        assert result.exit_code != 0

    def test_invalid_asin_in_detail(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["detail", "../../../etc/passwd"])
        assert result.exit_code != 0

    def test_invalid_asin_in_history(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["history", "../../bad"])
        assert result.exit_code != 0

    def test_wishlist_sync_update_without_max_price(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["wishlist", "sync", "--update"])
        assert result.exit_code != 0

    def test_wishlist_add_no_asins(self, tmp_config, mock_client):
        """wishlist add with no ASINs or --last should error."""
        result = CliRunner().invoke(cli, ["wishlist", "add"])
        assert result.exit_code != 0


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
        ("min_rating", "6"),
        ("min_rating", "inf"),
        ("max_price", "nan"),
        ("credit_price", "nan"),
    ],
)
def test_config_coercion_rejects_out_of_range(key, value):
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
def test_config_coercion_accepts_in_range(key, value, expected):
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


@pytest.mark.parametrize(
    "args",
    [
        ["find", "--dry-run", "--max-price", "nan"],
        ["find", "--dry-run", "--min-hours", "inf"],
        ["find", "--dry-run", "--min-rating", "6"],
        ["find", "--dry-run", "--min-ratings", "-1"],
    ],
)
def test_discovery_numeric_options_reject_invalid_values(tmp_config, args):
    result = CliRunner().invoke(cli, args)
    assert result.exit_code != 0


def test_config_set_rejects_zero_pages(tmp_config, mock_client):
    result = CliRunner().invoke(cli, ["config", "set", "pages", "0"])
    assert result.exit_code != 0


def test_config_set_accepts_valid_values(tmp_config, mock_client):
    result = _routes_run(CliRunner(), ["config", "set", "min-discount", "70"])
    assert result.exit_code == 0
    assert json.loads((tmp_config / "config.json").read_text())["min_discount"] == 70


class TestCompletionsHelpTextLeak:
    def _fake_help_run(self, *args, **kwargs):
        """Mimic the python -m fallback emitting plain help text on stdout."""
        return sp.CompletedProcess(
            args[0],
            0,
            stdout=(
                "Usage: python -m audible_deals [OPTIONS] COMMAND [ARGS]...\n\n"
                "Options:\n  --help  Show this message and exit.\n"
            ),
            stderr="",
        )

    def test_fallback_does_not_echo_help_text(self, monkeypatch):
        """When the spawned process returns help text, completions must fail
        rather than echoing the help banner into the shell config."""
        monkeypatch.setattr("subprocess.run", self._fake_help_run)
        monkeypatch.setattr("shutil.which", lambda _: None)

        result = CliRunner().invoke(cli, ["completions", "bash"])

        assert result.exit_code != 0
        assert "Usage:" not in result.output

    def test_fallback_uses_fixed_prog_name(self, monkeypatch):
        """The fallback must spawn with a stable prog_name so Click derives the
        _DEALS_COMPLETE var, not the python -m form."""
        calls = []

        def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return sp.CompletedProcess(
                args[0], 0, stdout="_deals_completion() { :; }\n", stderr=""
            )

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("shutil.which", lambda _: None)

        result = CliRunner().invoke(cli, ["completions", "bash"])

        assert result.exit_code == 0
        cmd = calls[0][0][0]
        assert "-m" not in cmd
        joined = " ".join(cmd)
        assert "prog_name='deals'" in joined

    def test_valid_completion_script_is_echoed(self, monkeypatch):
        """A genuine completion script (no Usage banner) is still emitted."""

        def fake_run(*args, **kwargs):
            return sp.CompletedProcess(
                args[0], 0, stdout="_deals_completion() { :; }\n", stderr=""
            )

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("shutil.which", lambda _: None)

        result = CliRunner().invoke(cli, ["completions", "bash"])

        assert result.exit_code == 0
        assert "_deals_completion" in result.output


class TestCompletionsExitCode:
    def test_nonzero_subprocess_surfaces_failure(self, monkeypatch):
        def fake_run(*args, **kwargs):
            return sp.CompletedProcess(args[0], 1, stdout="", stderr="boom\n")

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/deals")

        result = CliRunner().invoke(cli, ["completions", "bash"])

        assert result.exit_code != 0
        assert "boom" in result.output

    def test_empty_output_surfaces_failure(self, monkeypatch):
        def fake_run(*args, **kwargs):
            return sp.CompletedProcess(args[0], 0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/deals")

        result = CliRunner().invoke(cli, ["completions", "bash"])

        assert result.exit_code != 0


class TestWebhookCgnatSsrf:
    def test_rejects_cgnat_shared_space(self, monkeypatch):
        import socket

        monkeypatch.setattr(
            "audible_deals.validation.socket.getaddrinfo",
            lambda host, port: [(socket.AF_INET, 0, 0, "", ("100.64.0.1", 0))],
        )
        with pytest.raises(click.BadParameter, match="non-public"):
            validate_webhook_url("https://shared.nat/hook")

    def test_still_accepts_public_ip(self, monkeypatch):
        import socket

        monkeypatch.setattr(
            "audible_deals.validation.socket.getaddrinfo",
            lambda host, port: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
        )
        validate_webhook_url("https://example.com/hook")  # should not raise


def test_doctor_reports_indexed_wishlist_semantic_issues(tmp_config, mock_client):
    constants_mod.AUTH_FILE.write_text(
        json.dumps(
            {
                "access_token": "at",
                "refresh_token": "rt",
                "locale_code": "us",
                "expires": time.time() + 86400,
            }
        )
    )
    wishlist_mod.save_wishlist([{"asin": f"BAD{i}", "max_price": -1} for i in range(6)])

    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "Wishlist health" in result.output
    assert "WARN" in result.output
    assert "[0]" in result.output
    assert "+1 more" in result.output
    assert "wishlist repair --dry-run" in result.output


def test_doctor_fails_for_non_list_wishlist(tmp_config, mock_client):
    constants_mod.AUTH_FILE.write_text(
        json.dumps(
            {
                "access_token": "at",
                "refresh_token": "rt",
                "locale_code": "us",
                "expires": time.time() + 86400,
            }
        )
    )
    constants_mod.WISHLIST_FILE.write_text("{}")

    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 1
    assert "Wishlist health" in result.output
    assert "Expected a list" in result.output
