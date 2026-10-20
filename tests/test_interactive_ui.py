"""Which browser ``-i`` opens, and what happens when the full-screen one cannot.

The full-screen browser itself is covered in test_tui.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import click
import pytest
from click.testing import CliRunner

from audible_deals import wishlist as wishlist_mod
from audible_deals.cli import cli
from audible_deals.cli import interactive as interactive_mod
from audible_deals.cli.interactive import choose_ui
from audible_deals.config_store import load_config
from tests.conftest import bare_env, make_product, make_raw


class Stream:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


TTY, PIPE = Stream(True), Stream(False)


class TestChoosingTheBrowser:
    def test_a_terminal_with_textual_gets_the_full_screen_browser(self, monkeypatch):
        monkeypatch.setenv("TERM", "xterm-256color")

        assert choose_ui("auto", stdin=TTY, stdout=TTY) == "tui"

    def test_interactive_ui_prompt_forces_the_prompt_loop(self, monkeypatch):
        monkeypatch.setenv("TERM", "xterm-256color")

        assert choose_ui("prompt", stdin=TTY, stdout=TTY) == "prompt"

    def test_without_textual_it_is_the_prompt_loop(self, monkeypatch):
        import importlib.util

        real = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util,
            "find_spec",
            lambda name, *a: None if name == "textual" else real(name, *a),
        )
        monkeypatch.setenv("TERM", "xterm-256color")

        assert choose_ui("auto", stdin=TTY, stdout=TTY) == "prompt"

    def test_a_blocked_import_counts_as_not_installed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "textual", None)

        assert choose_ui("auto", stdin=TTY, stdout=TTY) == "prompt"

    @pytest.mark.parametrize(
        ("stdin", "stdout"), [(PIPE, TTY), (TTY, PIPE), (PIPE, PIPE)]
    )
    def test_a_pipe_on_either_end_is_the_prompt_loop(self, stdin, stdout, monkeypatch):
        monkeypatch.setenv("TERM", "xterm-256color")

        assert choose_ui("auto", stdin=stdin, stdout=stdout) == "prompt"

    def test_a_dumb_terminal_is_the_prompt_loop(self, monkeypatch):
        monkeypatch.setenv("TERM", "dumb")

        assert choose_ui("auto", stdin=TTY, stdout=TTY) == "prompt"

    def test_streams_without_isatty_are_the_prompt_loop(self, monkeypatch):
        monkeypatch.setenv("TERM", "xterm-256color")

        assert choose_ui("auto", stdin=object(), stdout=object()) == "prompt"

    def test_the_setting_comes_from_the_running_commands_config(self, monkeypatch):
        monkeypatch.setenv("TERM", "xterm-256color")
        context = click.Context(cli, obj={"config": {"interactive_ui": "prompt"}})

        with context:
            assert interactive_mod.interactive_ui_setting() == "prompt"
            assert choose_ui(stdin=TTY, stdout=TTY) == "prompt"
        with click.Context(cli, obj={"config": {}}):
            assert interactive_mod.interactive_ui_setting() == "auto"
            assert choose_ui(stdin=TTY, stdout=TTY) == "tui"

    def test_a_setting_nobody_recognizes_means_auto(self):
        with click.Context(cli, obj={"config": {"interactive_ui": "gui"}}):
            assert interactive_mod.interactive_ui_setting() == "auto"


class TestTheSetting:
    def test_it_can_be_set_and_is_validated(self, tmp_config):
        runner = CliRunner()

        ok = runner.invoke(cli, ["config", "set", "interactive-ui", "prompt"])
        bad = runner.invoke(cli, ["config", "set", "interactive-ui", "gui"])

        assert ok.exit_code == 0, ok.output
        assert load_config()["interactive_ui"] == "prompt"
        assert bad.exit_code == 1 and "Valid: auto, prompt" in bad.stderr


def batch(count=3):
    return [
        make_product(asin=f"B0UI{index:06d}", title=f"Book {index}", price=3.0 + index)
        for index in range(count)
    ]


@click.command("probe")
def probe():
    interactive_mod._interactive_browse(batch(), table_shown=False)


class TestRouting:
    @pytest.fixture
    def stub(self, monkeypatch):
        """Pretend the full-screen browser is chosen, and record what it is given."""
        calls = []

        def fake_run(products, currency, credit_price, **kwargs):
            calls.append((products, currency, credit_price, kwargs))
            return ["Added 1 title(s) to the wishlist"]

        monkeypatch.setattr(interactive_mod, "choose_ui", lambda *a, **k: "tui")
        import audible_deals.tui.app as tui_app

        monkeypatch.setattr(tui_app, "run_results_app", fake_run)
        return calls

    def test_the_full_screen_browser_replaces_the_prompt(self, tmp_config, stub):
        result = CliRunner().invoke(probe, input="")  # no prompt input is needed

        assert result.exit_code == 0, result.output
        assert len(stub) == 1
        products, currency, _credit, kwargs = stub[0]
        assert [p.asin for p in products] == [p.asin for p in batch()]
        assert kwargs["title"] == "Results"
        assert "Added 1 title(s) to the wishlist" in result.output  # kept in scrollback
        assert "Enter # or @#" not in result.output  # the prompt loop did not run

    def test_the_saved_sort_is_handed_to_the_browser(
        self, tmp_config, stub, monkeypatch
    ):
        monkeypatch.setattr(interactive_mod, "_saved_sort", lambda: "rating")

        CliRunner().invoke(probe, input="")

        assert stub[0][3]["sort_key"] == "rating"

    def test_a_browser_that_cannot_start_falls_back_to_the_prompt(
        self, tmp_config, monkeypatch
    ):
        monkeypatch.setattr(interactive_mod, "choose_ui", lambda *a, **k: "tui")
        import audible_deals.tui.app as tui_app

        def broken(*args, **kwargs):
            raise RuntimeError("no terminal capabilities")

        monkeypatch.setattr(tui_app, "run_results_app", broken)

        result = CliRunner().invoke(probe, input="q\n")

        assert result.exit_code == 0, result.output
        assert "Warning: The full-screen browser could not run" in result.stderr
        assert "no terminal capabilities" in result.stderr
        assert "interactive-ui prompt" in result.stderr
        assert "Book 0" in result.stdout  # the table was printed for the fallback
        assert "Enter # or @#" in result.stdout  # and the prompt loop ran

    def test_with_the_prompt_chosen_the_table_is_not_printed_twice(self, tmp_config):
        @click.command("probe-shown")
        def shown():
            interactive_mod._interactive_browse(batch(), table_shown=True)

        result = CliRunner().invoke(shown, input="q\n")

        assert "Book 0" not in result.stdout  # the caller already printed it

    def test_vv_lets_the_error_through(self, tmp_config, monkeypatch):
        monkeypatch.setattr(interactive_mod, "choose_ui", lambda *a, **k: "tui")
        import audible_deals.tui.app as tui_app

        monkeypatch.setattr(
            tui_app,
            "run_results_app",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        @click.command("probe-vv")
        @click.pass_context
        def vv(ctx):
            ctx.obj = {"verbose": 2}
            interactive_mod._interactive_browse(batch(), table_shown=False)

        result = CliRunner().invoke(vv, input="q\n")

        assert isinstance(result.exception, RuntimeError)


class TestThroughFind:
    @pytest.fixture
    def catalog(self, api):
        raws = [
            make_raw(
                f"B0FIND{n:05d}",
                title=f"Find Title {n}",
                price={"lowest_price": {"base": 2.0 + n}, "list_price": {"base": 20.0}},
                runtime_length_min=600,
                language="english",
                series=[],
            )
            for n in range(3)
        ]
        api.get_mock.side_effect = lambda endpoint, **p: (
            {"categories": []}
            if endpoint == "1.0/catalog/categories"
            else {"products": raws, "total_results": len(raws)}
        )

    FIND = ("find", "--pages", "1", "--max-price", "30", "-i")

    def test_with_the_full_screen_browser_the_table_is_not_printed_first(
        self, tmp_config, catalog, monkeypatch
    ):
        monkeypatch.setattr(interactive_mod, "choose_ui", lambda *a, **k: "tui")
        import audible_deals.tui.app as tui_app

        launched = []
        monkeypatch.setattr(
            tui_app,
            "run_results_app",
            lambda products, *a, **k: launched.append(products) or [],
        )

        result = CliRunner().invoke(cli, list(self.FIND))

        assert result.exit_code == 0, result.output
        assert len(launched) == 1 and len(launched[0]) == 3
        assert "Find Title" not in result.stdout  # no table before the full-screen app
        # The summary line still prints, and the saved session is ready for @N.
        assert "3 deals found under $30.00" in result.stdout
        assert json.loads(CliRunner().invoke(cli, ["last", "--json"]).stdout)

    def test_without_it_the_table_and_the_prompt_are_as_before(
        self, tmp_config, catalog
    ):
        result = CliRunner().invoke(cli, list(self.FIND), input="q\n")

        assert result.exit_code == 0, result.output
        assert "Find Title 0" in result.stdout
        assert "Enter # or @#" in result.stdout

    def test_the_prompt_loop_still_works_end_to_end(self, tmp_config, catalog):
        result = CliRunner().invoke(cli, list(self.FIND), input="w 1\n3.5\nq\n")

        assert result.exit_code == 0, result.output
        (item,) = wishlist_mod.load_wishlist()
        assert item["max_price"] == 3.5

    def test_json_never_opens_a_browser(self, tmp_config, catalog, monkeypatch):
        monkeypatch.setattr(interactive_mod, "choose_ui", lambda *a, **k: "tui")
        import audible_deals.tui.app as tui_app

        monkeypatch.setattr(
            tui_app, "run_results_app", lambda *a, **k: pytest.fail("launched")
        )

        result = CliRunner().invoke(cli, [*self.FIND, "--json"])

        assert result.exit_code == 0
        assert len(json.loads(result.stdout)) == 3


class TestImports:
    @pytest.mark.parametrize(
        "args",
        [
            ["--version"],
            ["--help"],
            ["find", "--help"],
            ["config", "list"],
            ["wishlist", "list"],
        ],
    )
    def test_textual_is_not_imported_by_other_commands(self, args, tmp_path):
        code = textwrap.dedent(
            f"""
            import sys
            from audible_deals.cli import cli
            try:
                cli.main(args={args!r}, prog_name="deals", standalone_mode=False)
            except SystemExit:
                pass
            bad = [m for m in sys.modules if m == "textual" or m.startswith("textual.")
                   or m == "audible_deals.tui" or m.startswith("audible_deals.tui.")]
            print(bad)
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=bare_env(HOME=str(tmp_path), DEALS_HOME=str(tmp_path / "d")),
        )

        assert result.stdout.strip().endswith("[]"), result.stdout + result.stderr

    def test_the_prompt_path_does_not_import_textual_either(
        self, tmp_config, api, monkeypatch
    ):
        code = textwrap.dedent(
            """
            import sys
            from audible_deals.cli.interactive import choose_ui
            print(choose_ui("auto", stdin=open("/dev/null"), stdout=open("/dev/null", "w")))
            print("textual" in sys.modules)
            """
        )
        if sys.platform == "win32":
            pytest.skip("needs /dev/null")
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )

        assert result.stdout.split() == ["prompt", "False"], result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pseudo-terminal")
class TestInARealTerminal:
    """`-i` in a pseudo-terminal: the config setting decides which browser opens."""

    SCRIPT = textwrap.dedent(
        """
        import click
        from audible_deals.cli import cli
        from audible_deals.cli.interactive import _interactive_browse
        from audible_deals.product import Product

        @cli.command("probe")
        def probe():
            _interactive_browse([
                Product(asin="B0PTY00001", title="Pty Book", authors=["A"], price=3.0,
                        list_price=10.0, length_minutes=600, rating=4.0, num_ratings=10)
            ], table_shown=False)

        cli.main(args=["probe"], prog_name="deals", standalone_mode=False)
        print("DONE")
        """
    )

    def _run(self, tmp_path, config, keys, wait_for):
        import pty
        import select
        import time

        (tmp_path / "d").mkdir()
        (tmp_path / "d" / "config.json").write_text(json.dumps(config))
        script = tmp_path / "probe.py"
        script.write_text(self.SCRIPT)
        master, slave = pty.openpty()
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={
                **os.environ,
                "TERM": "xterm-256color",
                "HOME": str(tmp_path),
                "DEALS_HOME": str(tmp_path / "d"),
            },
            close_fds=True,
        )
        os.close(slave)
        output, sent = b"", False
        deadline = time.time() + 20
        while time.time() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
            if not sent and wait_for in output:
                os.write(master, keys)
                sent = True
            if proc.poll() is not None and not ready:
                break
        code = proc.wait(timeout=10)
        os.close(master)
        return code, output, sent

    def test_auto_opens_the_full_screen_browser(self, tmp_path):
        code, output, sent = self._run(tmp_path, {}, b"q", b"Pty Book")

        assert sent and code == 0 and b"DONE" in output
        assert b"\x1b[?1049h" in output and b"\x1b[?1049l" in output
        assert b"Enter # or @#" not in output

    def test_interactive_ui_prompt_keeps_the_old_loop_in_a_terminal(self, tmp_path):
        code, output, sent = self._run(
            tmp_path, {"interactive_ui": "prompt"}, b"q\r", b"Enter # or @#"
        )

        assert sent and code == 0 and b"DONE" in output
        assert b"\x1b[?1049h" not in output  # no alternate screen
        assert b"Pty Book" in output  # the fallback printed the table itself
