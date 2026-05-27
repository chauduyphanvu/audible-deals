"""Tests for the logging setup and instrumentation."""

from __future__ import annotations

import logging

import pytest
from click.testing import CliRunner

from audible_deals.cli import cli
from audible_deals.logging_setup import configure_logging


@pytest.fixture(autouse=True)
def reset_logging(monkeypatch):
    """Reset the package logger between tests so configure_logging is idempotent."""
    monkeypatch.delenv("DEALS_LOG_FILE", raising=False)
    yield
    root = logging.getLogger("audible_deals")
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    root.setLevel(logging.WARNING)
    root.propagate = True


def _level(name: str = "audible_deals") -> int:
    return logging.getLogger(name).getEffectiveLevel()


# ===================================================================
# configure_logging
# ===================================================================


class TestConfigureLogging:
    def test_default_is_warning(self):
        configure_logging(0)
        assert _level() == logging.WARNING

    def test_v_enables_info(self):
        configure_logging(1)
        assert _level() == logging.INFO

    def test_vv_enables_debug(self):
        configure_logging(2)
        assert _level() == logging.DEBUG

    def test_env_var_forces_debug(self, monkeypatch):
        monkeypatch.setenv("DEALS_DEBUG", "1")
        configure_logging(0)
        assert _level() == logging.DEBUG

    def test_repeated_calls_replace_handler(self):
        configure_logging(2)
        configure_logging(1)
        root = logging.getLogger("audible_deals")
        assert len(root.handlers) == 1
        assert root.level == logging.INFO

    def test_noisy_loggers_quiet_at_warning(self):
        configure_logging(0)
        assert logging.getLogger("audible").getEffectiveLevel() >= logging.WARNING
        assert logging.getLogger("urllib3").getEffectiveLevel() >= logging.WARNING

    def test_noisy_loggers_debug_at_vv(self):
        configure_logging(2)
        assert logging.getLogger("audible").getEffectiveLevel() == logging.DEBUG


# ===================================================================
# CLI --verbose flag
# ===================================================================


class TestVerboseFlag:
    def test_flag_present_in_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "--verbose" in result.output
        assert "-v" in result.output

    def test_vv_sets_debug_level(self, tmp_config, monkeypatch):
        runner = CliRunner()
        # `config list` doesn't hit the network — safe smoke test.
        result = runner.invoke(cli, ["-vv", "config", "list"])
        assert result.exit_code == 0
        assert logging.getLogger("audible_deals").getEffectiveLevel() == logging.DEBUG


# ===================================================================
# WARNING records on corrupted state files
# ===================================================================


class TestStateCorruptionWarnings:
    def test_load_wishlist_warns_on_corrupt(self, tmp_config, caplog):
        import audible_deals.state as state_mod

        state_mod.WISHLIST_FILE.write_text("not json")
        with caplog.at_level(logging.WARNING, logger="audible_deals.state"):
            data = state_mod.load_wishlist()
        assert data == []
        assert any("corrupt" in r.message for r in caplog.records)

    def test_load_config_warns_on_corrupt(self, tmp_config, caplog):
        import audible_deals.state as state_mod

        state_mod.CONFIG_FILE.write_text("not json")
        with caplog.at_level(logging.WARNING, logger="audible_deals.state"):
            data = state_mod.load_config()
        assert data == {}
        assert any("corrupt" in r.message for r in caplog.records)

    def test_load_seen_asins_warns_on_corrupt(self, tmp_config, caplog):
        import audible_deals.state as state_mod

        state_mod.SEEN_ASINS_FILE.write_text("not json")
        with caplog.at_level(logging.WARNING, logger="audible_deals.state"):
            data = state_mod.load_seen_asins()
        assert data == set()
        assert any("corrupt" in r.message for r in caplog.records)


# ===================================================================
# DEBUG records on API calls
# ===================================================================


class TestApiDebugLogging:
    def test_search_catalog_emits_debug(self, api, caplog):
        from audible_deals.client import DealsClient

        api.get_mock.return_value = {"products": [], "total_results": 0}
        dc = DealsClient(auth_file=api.tmp_path / "auth.json", locale="us")

        with caplog.at_level(logging.DEBUG, logger="audible_deals.client"):
            dc.search_catalog(keywords="dune", page=1)

        msgs = [r.message for r in caplog.records]
        assert any("API GET" in m and "1.0/catalog/products" in m for m in msgs)
        # Both the request and the completion line should fire.
        assert sum("API GET" in m for m in msgs) >= 2

    def test_get_products_batch_emits_debug(self, api, caplog):
        from audible_deals.client import DealsClient

        api.get_mock.return_value = {"products": []}
        dc = DealsClient(auth_file=api.tmp_path / "auth.json", locale="us")

        with caplog.at_level(logging.DEBUG, logger="audible_deals.client"):
            dc.get_products_batch(["B00TEST0001", "B00TEST0002"])

        msgs = [r.message for r in caplog.records]
        assert any("get_products_batch in=2" in m for m in msgs)


# ===================================================================
# DEALS_LOG_FILE rotating file handler
# ===================================================================


class TestDealsLogFile:
    def test_file_handler_added(self, tmp_path, monkeypatch):
        log_path = tmp_path / "test.log"
        monkeypatch.setenv("DEALS_LOG_FILE", str(log_path))
        configure_logging(0)
        root = logging.getLogger("audible_deals")
        file_handlers = [h for h in root.handlers if hasattr(h, "baseFilename")]
        assert len(file_handlers) == 1
        assert file_handlers[0].level == logging.DEBUG

    def test_file_handler_writes(self, tmp_path, monkeypatch):
        log_path = tmp_path / "test.log"
        monkeypatch.setenv("DEALS_LOG_FILE", str(log_path))
        configure_logging(0)
        logging.getLogger("audible_deals.test").debug("hello from test")
        assert log_path.exists()
        content = log_path.read_text()
        assert "hello from test" in content

    def test_creates_parent_dirs(self, tmp_path, monkeypatch):
        log_path = tmp_path / "nested" / "subdir" / "test.log"
        monkeypatch.setenv("DEALS_LOG_FILE", str(log_path))
        configure_logging(0)
        assert log_path.parent.exists()

    def test_no_file_handler_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("DEALS_LOG_FILE", raising=False)
        configure_logging(0)
        root = logging.getLogger("audible_deals")
        file_handlers = [h for h in root.handlers if hasattr(h, "baseFilename")]
        assert file_handlers == []

    def test_idempotent_replaces_handlers(self, tmp_path, monkeypatch):
        log_path = tmp_path / "test.log"
        monkeypatch.setenv("DEALS_LOG_FILE", str(log_path))
        configure_logging(0)
        configure_logging(1)
        root = logging.getLogger("audible_deals")
        assert len(root.handlers) == 2  # one stderr + one file
