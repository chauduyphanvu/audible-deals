"""State files are UTF-8 whatever the platform's locale encoding is."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

SCRIPT = textwrap.dedent(
    """
    import json, locale, sys
    from audible_deals import paths
    from audible_deals.config_store import load_profiles, save_config, save_profiles
    from audible_deals.wishlist import load_wishlist, save_wishlist
    from audible_deals.results_cache import save_last_results, load_last_results
    from audible_deals.price_history import record_prices, load_price_history
    from audible_deals.product import Product
    from audible_deals.state_store import save_install_record, load_install_record

    assert locale.getpreferredencoding(False).lower() in ("ascii", "ansi_x3.4-1968", "us-ascii"), (
        locale.getpreferredencoding(False)
    )
    text = "Caf\\u00e9 \\u2013 \\u65e5\\u672c\\u8a9e \\U0001F3A7"
    save_profiles({"p": {"genre": text}})
    save_config({"webhook": "https://example.test/" + text})
    save_wishlist([{"asin": "B00UTF8001", "title": text, "max_price": 3.0, "added": ""}])
    save_last_results(text, [{"asin": "B00UTF8001", "title": text}])
    record_prices([Product(asin="B00UTF8001", title=text, price=2.5)])
    save_install_record({"method": text})

    assert load_profiles()["p"]["genre"] == text
    assert load_wishlist()[0]["title"] == text
    assert load_last_results()[0] == text
    assert load_price_history("B00UTF8001")[0]["title"] == text
    assert load_install_record()["method"] == text
    print("utf8-ok")
    """
)


def test_state_round_trips_under_an_ascii_locale(tmp_path):
    env = {
        **os.environ,
        "DEALS_HOME": str(tmp_path),
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONIOENCODING": "utf-8",
    }
    script = tmp_path / "probe.py"
    script.write_text(SCRIPT, encoding="ascii")
    result = subprocess.run(
        [sys.executable, str(script)], env=env, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    assert "utf8-ok" in result.stdout
    profiles = json.loads((tmp_path / "profiles.json").read_text(encoding="utf-8"))
    assert profiles["p"]["genre"] == "Café – 日本語 🎧"


def test_the_written_bytes_are_utf8_not_the_locale_encoding(tmp_config):
    from audible_deals.config_store import save_profiles

    save_profiles({"p": {"genre": "Café"}})

    raw = (tmp_config / "profiles.json").read_bytes()
    assert "Café".encode() in raw
    assert b"\xe9" not in raw.replace("é".encode(), b"")  # no latin-1 byte


def test_files_written_in_the_old_locale_encoding_still_load(tmp_config, monkeypatch):
    monkeypatch.setattr(
        "audible_deals.storage.locale.getpreferredencoding", lambda do: "cp1252"
    )
    (tmp_config / "profiles.json").write_bytes(
        '{"p": {"genre": "Café"}}'.encode("cp1252")
    )

    from audible_deals.config_store import load_profiles

    assert load_profiles() == {"p": {"genre": "Café"}}


def test_a_hand_edited_file_with_a_bom_loads(tmp_config):
    (tmp_config / "profiles.json").write_bytes(
        b"\xef\xbb\xbf" + b'{"p": {"genre": "x"}}'
    )

    from audible_deals.config_store import load_profiles

    assert load_profiles() == {"p": {"genre": "x"}}


def test_the_track_log_is_read_as_utf8(tmp_config):
    from click.testing import CliRunner

    from audible_deals.cli import cli

    (tmp_config / "track.log").write_bytes("Café – ok\n".encode())

    result = CliRunner().invoke(cli, ["track", "log"])

    assert "Café – ok" in result.output
