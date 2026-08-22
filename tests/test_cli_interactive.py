"""Interactive browser CLI behavior."""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

import audible_deals.constants as constants_mod
import audible_deals.wishlist as wishlist_mod
from audible_deals.cli import cli
from audible_deals.result_models import (
    DiscoveryResult,
)
from audible_deals.results_cache import (
    load_dismissed_asins,
    load_result_session,
    save_last_results,
)
from tests.conftest import make_product


def _run_browse(products, user_input, tmp_config):
    """Drive _interactive_browse via a thin Click wrapper with simulated input."""
    from audible_deals.cli.interactive import _interactive_browse

    @click.command()
    def _cmd():
        _interactive_browse(products)

    runner = CliRunner()
    return runner.invoke(_cmd, input=user_input, catch_exceptions=False)


class TestInteractiveBrowse:
    """Drive _interactive_browse via a thin Click wrapper."""

    def test_bulk_range_adds_two(self, tmp_config):
        """'w 1-2' adds both products with a single price prompt."""

        products = [
            make_product(asin="BR1", title="Book One"),
            make_product(asin="BR2", title="Book Two"),
            make_product(asin="BR3", title="Book Three"),
        ]
        result = _run_browse(products, "w 1-2\n3.99\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        items = wishlist_mod.load_wishlist()
        asins = [i["asin"] for i in items]
        assert "BR1" in asins
        assert "BR2" in asins
        assert "BR3" not in asins
        assert items[0]["max_price"] == 3.99
        assert items[1]["max_price"] == 3.99

    def test_bulk_list_adds_two(self, tmp_config):
        """'w 1,3' adds first and third products."""

        products = [
            make_product(asin="BL1", title="Book One"),
            make_product(asin="BL2", title="Book Two"),
            make_product(asin="BL3", title="Book Three"),
        ]
        result = _run_browse(products, "w 1,3\n\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        items = wishlist_mod.load_wishlist()
        asins = [i["asin"] for i in items]
        assert "BL1" in asins
        assert "BL3" in asins
        assert "BL2" not in asins

    def test_bulk_dedup_same_index_twice(self, tmp_config):
        """'w 1,1' adds the item only once."""

        products = [make_product(asin="BD1", title="Dedup Book")]
        result = _run_browse(products, "w 1,1\n\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        items = wishlist_mod.load_wishlist()
        assert len([i for i in items if i.get("asin") == "BD1"]) == 1

    def test_out_of_range_rejected_without_crash(self, tmp_config):
        """An out-of-range index in a range prints the bounds message and continues."""

        products = [make_product(asin="OR1", title="Only Book")]
        result = _run_browse(products, "w 5\n\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        assert "1-1" in result.output
        assert wishlist_mod.load_wishlist() == []

    def test_single_index_still_works(self, tmp_config):
        """'w 1' still adds a single item as before."""

        products = [make_product(asin="SI1", title="Single Book")]
        result = _run_browse(products, "w 1\n\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        items = wishlist_mod.load_wishlist()
        assert any(i.get("asin") == "SI1" for i in items)

    def test_already_on_wishlist_shows_note(self, tmp_config):
        """Skips with a dim note if ASIN is already on the wishlist."""

        products = [make_product(asin="AW1", title="Already Book")]
        constants_mod.WISHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        constants_mod.WISHLIST_FILE.write_text(
            json.dumps(
                [
                    {
                        "asin": "AW1",
                        "title": "Already Book",
                        "max_price": None,
                        "added": "",
                    }
                ]
            )
        )
        # Already-wishlisted item: note is shown with no prompt and no write.
        result = _run_browse(products, "w 1\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        assert "already on wishlist" in result.output

    def test_help_action_prints_hint(self, tmp_config):
        """'?' prints the hint line and result count."""
        products = [make_product(asin="HP1"), make_product(asin="HP2")]
        result = _run_browse(products, "?\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        assert "1-2" in result.output

    def test_help_keyword_prints_hint(self, tmp_config):
        """'help' also prints the hint."""
        products = [make_product(asin="HK1")]
        result = _run_browse(products, "help\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        assert "w #" in result.output

    def test_invalid_input_mentions_question_mark(self, tmp_config):
        """The invalid-input message mentions '?'."""
        products = [make_product(asin="IM1")]
        result = _run_browse(products, "xyz\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        assert "?" in result.output

    def test_sort_discount_reorders_and_rerenders(self, tmp_config):
        """'s discount' re-sorts highest-discount first and re-renders the table."""
        p1 = make_product(asin="SD1", title="FiftyPct Book", price=5.0, list_price=10.0)
        p2 = make_product(
            asin="SD2", title="NinetyPct Book", price=1.0, list_price=10.0
        )
        result = _run_browse([p1, p2], "s discount\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        # Table was re-rendered: both titles appear in correct relative order
        assert "FiftyPct Book" in result.output
        assert "NinetyPct Book" in result.output
        assert result.output.index("NinetyPct Book") < result.output.index(
            "FiftyPct Book"
        )

    def test_sort_updates_last_results_cache(self, tmp_config):
        """'s discount' reorders the last-results cache to match the new screen order."""
        from audible_deals.results_cache import load_last_results, save_last_results

        p1 = make_product(asin="CR1", title="FiftyPct", price=5.0, list_price=10.0)
        p2 = make_product(asin="CR2", title="NinetyPct", price=1.0, list_price=10.0)
        # Seed cache with p1, p2, and a tail entry that is beyond the display limit
        save_last_results(
            "test",
            [
                {"asin": "CR1", "title": "FiftyPct"},
                {"asin": "CR2", "title": "NinetyPct"},
                {"asin": "CR3", "title": "TailEntry"},
            ],
        )
        _run_browse([p1, p2], "s discount\nq\n", tmp_config)
        _, data = load_last_results()
        asins = [d["asin"] for d in data]
        assert asins == ["CR2", "CR1"]
        from audible_deals.results_cache import load_result_session

        session = load_result_session()
        assert [item["asin"] for item in session.candidates] == ["CR1", "CR2", "CR3"]

    def test_sort_bogus_key_prints_error_no_crash(self, tmp_config):
        """'s bogus' prints the valid-key error and continues without crashing."""
        products = [make_product(asin="SB1")]
        result = _run_browse(products, "s bogus\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        assert "bogus" in result.output
        assert "discount" in result.output  # valid keys listed

    def test_not_interested_single_writes_to_dismissed_and_removes_row(
        self, tmp_config
    ):
        products = [make_product(asin="NI1", title="Skip Me")]
        result = _run_browse(products, "n 1\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        assert load_dismissed_asins() == {"NI1"}
        assert "Globally dismissed" in result.output

    def test_not_interested_range_writes_both_dismissed_asins(self, tmp_config):
        products = [
            make_product(asin="NR1", title="Skip One"),
            make_product(asin="NR2", title="Skip Two"),
        ]
        result = _run_browse(products, "n 1-2\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        assert load_dismissed_asins() == {"NR1", "NR2"}


class TestInteractiveCommandParser:
    @pytest.mark.parametrize(
        ("text", "action", "positions", "sort_key"),
        [
            ("  @2  ", "detail", (1,), None),
            ("1 ignored", "detail", (0,), None),
            ("C @1 2", "compare", (0, 1), None),
            ("W @1,@3-@4,@1", "wishlist", (0, 2, 3), None),
            ("n 2,2,1", "hide", (1, 0), None),
            ("S PRICE", "sort", (), "price"),
            ("O @2", "open", (1,), None),
            ("h 1", "history", (0,), None),
            ("q", "quit", (), None),
            ("Q", "quit", (), None),
            ("?", "help", (), None),
            ("help", "help", (), None),
        ],
    )
    def test_current_grammar(self, text, action, positions, sort_key):
        from audible_deals.cli.interactive import parse_interactive_command

        command = parse_interactive_command(text)

        assert (command.action, command.positions, command.sort_key) == (
            action,
            positions,
            sort_key,
        )

    @pytest.mark.parametrize("text", ["quit", "hide 1", "HELP", "c 1", "", "w 1 2"])
    def test_rejects_unrecognized_aliases_and_arities(self, text):
        from audible_deals.cli.interactive import (
            INTERACTIVE_INVALID_INPUT,
            InteractiveCommandParseError,
            parse_interactive_command,
        )

        with pytest.raises(InteractiveCommandParseError) as exc_info:
            parse_interactive_command(text)

        assert str(exc_info.value) == INTERACTIVE_INVALID_INPUT

    def test_selection_diagnostic_is_preserved(self):
        from audible_deals.cli.interactive import parse_interactive_command

        with pytest.raises(click.ClickException) as exc_info:
            parse_interactive_command("w 3-1")

        assert (
            str(exc_info.value)
            == "Invalid selection range '3-1': start must not exceed end."
        )

    def test_command_is_frozen(self):
        from audible_deals.cli.interactive import InteractiveCommand

        command = InteractiveCommand("detail", (0,))

        with pytest.raises(AttributeError):
            command.action = "quit"


class TestInteractiveBrowserDispatcher:
    def _browser(self):
        from audible_deals.cli.interactive import InteractiveBrowser

        return InteractiveBrowser(
            [
                make_product(asin="IB1", title="First", price=5.0),
                make_product(asin="IB2", title="Second", price=2.0),
            ],
            currency="£",
            credit_price=9.99,
        )

    def test_detail_compare_open_and_quit_dispatch(self, monkeypatch):
        import audible_deals.cli.interactive as interactive_mod

        browser = self._browser()
        details = []
        comparisons = []
        launches = []
        monkeypatch.setattr(
            interactive_mod,
            "display_product_detail",
            lambda product, **kwargs: details.append((product.asin, kwargs)),
        )
        monkeypatch.setattr(
            interactive_mod,
            "display_comparison",
            lambda products, **kwargs: comparisons.append(
                ([product.asin for product in products], kwargs)
            ),
        )
        monkeypatch.setattr(click, "launch", launches.append)

        assert browser.dispatch(interactive_mod.InteractiveCommand("detail", (0,)))
        assert browser.dispatch(interactive_mod.InteractiveCommand("compare", (0, 1)))
        assert browser.dispatch(interactive_mod.InteractiveCommand("open", (1,)))
        assert not browser.dispatch(interactive_mod.InteractiveCommand("quit"))
        assert details == [("IB1", {"credit_price": 9.99})]
        assert comparisons == [(["IB1", "IB2"], {"credit_price": 9.99})]
        assert launches == [browser.products[1].url]

    def test_history_help_bounds_and_hide_dispatch(self, monkeypatch):
        import audible_deals.cli.interactive as interactive_mod

        browser = self._browser()
        output = []
        histories = []
        hidden = []
        monkeypatch.setattr(
            interactive_mod.console, "print", lambda value="": output.append(value)
        )
        monkeypatch.setattr(
            interactive_mod,
            "load_price_history",
            lambda asin, locale: [{"price": 1}],
        )
        monkeypatch.setattr(
            interactive_mod,
            "display_price_history",
            lambda entries, asin, currency: histories.append((entries, asin, currency)),
        )
        monkeypatch.setattr(interactive_mod, "save_dismissed_asins", hidden.append)
        monkeypatch.setattr(interactive_mod, "update_session_view", lambda *args: None)
        monkeypatch.setattr(
            interactive_mod, "display_products", lambda *args, **kwargs: None
        )

        assert browser.dispatch(interactive_mod.InteractiveCommand("help"))
        assert browser.dispatch(interactive_mod.InteractiveCommand("history", (0,)))
        assert browser.dispatch(interactive_mod.InteractiveCommand("hide", (0, 1)))
        assert browser.dispatch(interactive_mod.InteractiveCommand("detail", (2,)))
        assert histories == [([{"price": 1}], "IB1", "£")]
        assert hidden == [{"IB1", "IB2"}]
        assert browser.products == []
        assert any("Results: 1-2" in line for line in output)
        assert any("Number must be 1-0" in line for line in output)

    def test_empty_positions_use_bounds_diagnostic(self, monkeypatch):
        import audible_deals.cli.interactive as interactive_mod

        browser = self._browser()
        output = []
        monkeypatch.setattr(interactive_mod.console, "print", output.append)
        monkeypatch.setattr(
            interactive_mod,
            "display_product_detail",
            lambda *args, **kwargs: pytest.fail("rendered"),
        )

        assert browser.dispatch(interactive_mod.InteractiveCommand("detail"))
        assert output == ["[dim]Number must be 1-2.[/dim]"]

    def test_sort_persistence_failure_still_rerenders(self, monkeypatch):
        import audible_deals.cli.interactive as interactive_mod

        browser = self._browser()
        rendered = []
        monkeypatch.setattr(
            interactive_mod,
            "update_session_view",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read only")),
        )
        monkeypatch.setattr(
            interactive_mod,
            "display_products",
            lambda products, **kwargs: rendered.append(
                ([product.asin for product in products], kwargs)
            ),
        )

        assert browser.dispatch(
            interactive_mod.InteractiveCommand("sort", sort_key="price")
        )
        assert [product.asin for product in browser.products] == ["IB2", "IB1"]
        assert rendered[0][0] == ["IB2", "IB1"]

    def test_failed_dismissed_write_leaves_rows_and_cached_view_unchanged(
        self, tmp_config, monkeypatch
    ):
        import audible_deals.cli.interactive as interactive_mod

        browser = self._browser()
        save_last_results(
            "test",
            [
                {"asin": "IB1", "title": "First"},
                {"asin": "IB2", "title": "Second"},
            ],
        )
        before = constants_mod.LAST_RESULTS_FILE.read_text()
        rendered = []
        monkeypatch.setattr(
            interactive_mod,
            "save_dismissed_asins",
            lambda *args: (_ for _ in ()).throw(OSError("read only")),
        )
        monkeypatch.setattr(
            interactive_mod,
            "display_products",
            lambda *args, **kwargs: rendered.append(args),
        )

        assert browser.dispatch(interactive_mod.InteractiveCommand("hide", (0,)))

        assert [product.asin for product in browser.products] == ["IB1", "IB2"]
        assert constants_mod.LAST_RESULTS_FILE.read_text() == before
        assert rendered == []
        assert load_dismissed_asins() == set()

    def test_dismissed_removes_duplicate_rows_and_updates_visible_order(
        self, tmp_config, monkeypatch
    ):
        import audible_deals.cli.interactive as interactive_mod

        products = [
            make_product(asin="DUP1", title="First edition"),
            make_product(asin="KEEP", title="Keep"),
            make_product(asin="DUP1", title="Second edition"),
        ]
        save_last_results(
            "test",
            [
                {"asin": "DUP1", "title": "First edition"},
                {"asin": "KEEP", "title": "Keep"},
            ],
        )
        rendered = []
        monkeypatch.setattr(
            interactive_mod,
            "display_products",
            lambda rows, **kwargs: rendered.append([row.asin for row in rows]),
        )
        browser = interactive_mod.InteractiveBrowser(products)

        assert browser.dispatch(interactive_mod.InteractiveCommand("hide", (0,)))

        assert [product.asin for product in browser.products] == ["KEEP"]
        assert load_dismissed_asins() == {"DUP1"}
        assert load_result_session().visible_asins == ["KEEP"]
        assert rendered == [["KEEP"]]

    def test_dismissed_first_row_rerenders_remaining_as_one_with_context(
        self, tmp_config, monkeypatch
    ):
        from io import StringIO

        from rich.console import Console

        import audible_deals.cli.interactive as interactive_mod
        from audible_deals.presentation import terminal

        dismissed = make_product(asin="FIRST", title="Dismiss first", price=2.0)
        remaining = make_product(asin="SECOND", title="Now first", price=4.0)
        save_last_results(
            "Context results",
            [
                {"asin": dismissed.asin, "title": dismissed.title},
                {"asin": remaining.asin, "title": remaining.title},
            ],
        )
        browser = interactive_mod.InteractiveBrowser(
            [dismissed, remaining],
            currency="£",
            credit_price=8.5,
            title="Context results",
            max_price=6.0,
            show_url=True,
            atl_asins={remaining.asin},
            hist_context={remaining.asin: -25},
            match_context={remaining.asin: "revised match"},
        )
        rendered = []
        real_display_products = interactive_mod.display_products
        output = StringIO()
        monkeypatch.setattr(
            terminal,
            "console",
            Console(file=output, force_terminal=False, width=160),
        )

        def spy_display_products(products, **kwargs):
            rendered.append(([product.asin for product in products], kwargs))
            real_display_products(products, **kwargs)

        monkeypatch.setattr(interactive_mod, "display_products", spy_display_products)

        assert browser.dispatch(interactive_mod.InteractiveCommand("hide", (0,)))

        text = output.getvalue()
        assert "@1" in text
        assert remaining.title in text
        assert dismissed.title not in text
        assert rendered == [
            (
                [remaining.asin],
                {
                    "max_price": 6.0,
                    "title": "Context results",
                    "currency": "£",
                    "show_url": True,
                    "atl_asins": {remaining.asin},
                    "hist_context": {remaining.asin: -25},
                    "credit_price": 8.5,
                    "match_context": {remaining.asin: "revised match"},
                },
            )
        ]


class TestInteractiveBrowseWishlist:
    def test_duplicate_new_asin_reports_once_and_commits_first_row(
        self, tmp_config, monkeypatch
    ):
        import audible_deals.cli.interactive as interactive_mod

        products = [
            make_product(asin="DUPROW1", title="First Row"),
            make_product(asin="DUPROW1", title="Second Row"),
        ]
        output = []
        prompts = []
        monkeypatch.setattr(interactive_mod.console, "print", output.append)
        monkeypatch.setattr(
            click,
            "prompt",
            lambda *args, **kwargs: prompts.append((args, kwargs)) or "",
        )

        browser = interactive_mod.InteractiveBrowser(products)
        assert browser.dispatch(interactive_mod.InteractiveCommand("wishlist", (0, 1)))

        assert output == [
            "[dim]DUPROW1 already on wishlist[/dim]",
            "[green]+[/green] First Row added to wishlist",
        ]
        assert len(prompts) == 1
        assert wishlist_mod.load_wishlist()[0]["title"] == "First Row"

    def test_concurrent_add_result_is_rendered(self, monkeypatch):
        import audible_deals.cli.interactive as interactive_mod
        from audible_deals.wishlist_service import (
            WishlistAddEvent,
            WishlistAddPlan,
            WishlistAddResult,
        )

        product = make_product(asin="RACE1", title="Raced")
        output = []
        monkeypatch.setattr(
            interactive_mod,
            "plan_product_add",
            lambda asins: WishlistAddPlan(tuple(asins), (), (), 0),
        )
        monkeypatch.setattr(
            interactive_mod,
            "add_products",
            lambda products, target: WishlistAddResult(
                (),
                ("RACE1",),
                (),
                1,
                (WishlistAddEvent("raced", product),),
            ),
        )
        monkeypatch.setattr(interactive_mod.console, "print", output.append)
        monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: "")

        browser = interactive_mod.InteractiveBrowser([product])
        assert browser.dispatch(interactive_mod.InteractiveCommand("wishlist", (0,)))
        assert any("already on wishlist" in line for line in output)

    def test_all_existing_skips_prompt_lock_and_save(self, monkeypatch):
        import audible_deals.cli.interactive as interactive_mod
        from audible_deals.wishlist_service import WishlistAddPlan

        product = make_product(asin="EXISTS1")
        monkeypatch.setattr(
            interactive_mod,
            "plan_product_add",
            lambda asins: WishlistAddPlan((), tuple(asins), (), 1),
        )
        monkeypatch.setattr(
            click, "prompt", lambda *args, **kwargs: pytest.fail("prompted")
        )
        monkeypatch.setattr(
            interactive_mod,
            "add_products",
            lambda *args, **kwargs: pytest.fail("added"),
        )

        browser = interactive_mod.InteractiveBrowser([product])
        assert browser.dispatch(interactive_mod.InteractiveCommand("wishlist", (0,)))


class TestInteractiveBrowseLoop:
    @pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
    def test_prompt_interrupts_exit_cleanly(self, error, monkeypatch):
        import audible_deals.cli.interactive as interactive_mod

        monkeypatch.setattr(
            click, "prompt", lambda *args, **kwargs: (_ for _ in ()).throw(error())
        )

        interactive_mod._interactive_browse([make_product(asin="EXIT1")])

    def test_cli_integration_enters_browser_and_quits(self, mock_client, tmp_config):
        products = [make_product(asin="CLIINT1", title="Interactive Result")]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])

        result = CliRunner().invoke(
            cli,
            ["find", "--interactive", "--pages", "1", "--max-price", "10"],
            input="q\n",
        )

        assert result.exit_code == 0, result.output
        assert "Interactive Result" in result.output
        assert "Enter # or @# for details" in result.output


class TestInteractiveBrowseWishlistCheck:
    def _run(self, products, user_input, tmp_config):
        from audible_deals.cli.interactive import _interactive_browse

        @click.command()
        def _cmd():
            _interactive_browse(products)

        runner = CliRunner()
        return runner.invoke(_cmd, input=user_input, catch_exceptions=False)

    def test_already_wishlisted_no_file_write(self, tmp_config):
        """When all selected items are already on wishlist, file is not rewritten."""

        products = [make_product(asin="NW1", title="Already There")]
        constants_mod.WISHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        original = [
            {"asin": "NW1", "title": "Already There", "max_price": None, "added": ""}
        ]
        constants_mod.WISHLIST_FILE.write_text(json.dumps(original))
        mtime_before = constants_mod.WISHLIST_FILE.stat().st_mtime

        result = _run_browse(products, "w 1\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        assert "already on wishlist" in result.output
        assert constants_mod.WISHLIST_FILE.stat().st_mtime == mtime_before

    def test_mixed_some_already_wishlisted_adds_only_new(self, tmp_config):
        """Partial overlap: already-on-wishlist items get a note; new items are added."""

        products = [
            make_product(asin="MX1", title="Already"),
            make_product(asin="MX2", title="New One"),
        ]
        constants_mod.WISHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        constants_mod.WISHLIST_FILE.write_text(
            json.dumps(
                [{"asin": "MX1", "title": "Already", "max_price": None, "added": ""}]
            )
        )
        result = _run_browse(products, "w 1,2\n\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        assert "already on wishlist" in result.output
        items = wishlist_mod.load_wishlist()
        asins = [i.get("asin") for i in items]
        assert "MX1" in asins
        assert "MX2" in asins


class TestQuietInteractiveContext:
    def test_quiet_interactive_passes_context_to_browse(self, monkeypatch, tmp_config):
        """_emit_output in quiet+interactive must compute and pass atl/hist context."""
        from audible_deals.presentation.result_output import (
            ResultPresentationRequest,
            emit_results,
        )
        from audible_deals.serialization import serialize_product

        captured: dict = {}

        def fake_browse(products, **kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(
            "audible_deals.cli.interactive._interactive_browse", fake_browse
        )
        p = make_product(asin="QI1", title="Quiet Interact", price=3.0)
        emit_results(
            ResultPresentationRequest(
                result=DiscoveryResult([p]),
                serialized=(serialize_product(p),),
                title="T",
                json_flag=False,
                quiet=True,
                max_price=None,
                total_before_limit=1,
                interactive=True,
            )
        )
        assert "atl_asins" in captured
        assert "hist_context" in captured


class TestInteractiveBrowseVerbs:
    def _setup(self, tmp_config, monkeypatch):
        from io import StringIO

        from rich.console import Console

        from audible_deals.presentation import terminal as display_mod

        test_console = Console(file=StringIO(), force_terminal=False)
        monkeypatch.setattr(display_mod, "console", test_console)
        import audible_deals.cli.interactive as interactive_mod

        monkeypatch.setattr(interactive_mod, "console", test_console)
        return test_console

    def test_compare_shows_both_titles(self, tmp_config, monkeypatch):
        from audible_deals.cli.interactive import _interactive_browse

        p1 = make_product(asin="IA01", title="Alpha Book", price=5.0)
        p2 = make_product(asin="IA02", title="Beta Book", price=8.0)
        inputs = iter(["c 1 2", "q"])
        monkeypatch.setattr("click.prompt", lambda *a, **kw: next(inputs))
        console = self._setup(tmp_config, monkeypatch)
        _interactive_browse([p1, p2])
        out = console.file.getvalue()
        assert "Alpha Book" in out
        assert "Beta Book" in out

    def test_history_no_entries_prints_message(self, tmp_config, monkeypatch):
        import audible_deals.cli.interactive as interactive_mod
        from audible_deals.cli.interactive import _interactive_browse

        monkeypatch.setattr(
            interactive_mod, "load_price_history", lambda asin, locale: []
        )
        p = make_product(asin="IH01", title="History Book", price=5.0)
        inputs = iter(["h 1", "q"])
        monkeypatch.setattr("click.prompt", lambda *a, **kw: next(inputs))
        console = self._setup(tmp_config, monkeypatch)
        _interactive_browse([p])
        out = console.file.getvalue()
        assert "IH01" in out
        assert "No price history" in out

    def test_compare_missing_second_index_prints_error(self, tmp_config, monkeypatch):
        from audible_deals.cli.interactive import _interactive_browse

        p = make_product(asin="IC01", title="Only Book", price=5.0)
        inputs = iter(["c 1", "q"])
        monkeypatch.setattr("click.prompt", lambda *a, **kw: next(inputs))
        console = self._setup(tmp_config, monkeypatch)
        _interactive_browse([p])
        out = console.file.getvalue()
        assert "Invalid input" in out


class TestInteractiveTargetPrice:
    def test_typo_target_price_warns_and_sets_no_target(self, tmp_config):
        """A non-numeric typo warns instead of silently adding with no target."""
        products = [make_product(asin="TP1", title="Typo Book")]
        result = _run_browse(products, "w 1\n5o\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        assert "Invalid price" in result.output
        items = wishlist_mod.load_wishlist()
        assert len(items) == 1
        assert items[0]["asin"] == "TP1"
        assert items[0]["max_price"] is None

    def test_negative_target_price_rejected(self, tmp_config):
        """A negative target is rejected and leaves no target set."""
        products = [make_product(asin="NEG1", title="Neg Book")]
        result = _run_browse(products, "w 1\n-5\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        items = wishlist_mod.load_wishlist()
        assert len(items) == 1
        assert items[0]["asin"] == "NEG1"
        assert items[0]["max_price"] is None

    def test_zero_target_price_rejected(self, tmp_config):
        """A zero target is rejected and leaves no target set."""
        products = [make_product(asin="ZERO1", title="Zero Book")]
        result = _run_browse(products, "w 1\n0\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        items = wishlist_mod.load_wishlist()
        assert len(items) == 1
        assert items[0]["max_price"] is None

    def test_valid_target_price_still_set(self, tmp_config):
        """A valid positive target is still stored as before."""
        products = [make_product(asin="OK1", title="Ok Book")]
        result = _run_browse(products, "w 1\n3.99\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        items = wishlist_mod.load_wishlist()
        assert items[0]["max_price"] == 3.99

    def test_skip_target_price_still_works(self, tmp_config):
        """Pressing Enter to skip still adds with no target and no warning."""
        products = [make_product(asin="SK1", title="Skip Book")]
        result = _run_browse(products, "w 1\n\nq\n", tmp_config)
        assert result.exit_code == 0, result.output
        assert "Invalid price" not in result.output
        items = wishlist_mod.load_wishlist()
        assert items[0]["max_price"] is None


class TestInteractiveLocaleValidation:
    def test_invalid_locale_rejected(self, tmp_config):
        """An unknown --locale fails with a clear error instead of silent US fallback."""
        result = CliRunner().invoke(cli, ["--locale", "xx", "search", "test"])
        assert result.exit_code != 0
        assert "Invalid locale" in result.output

    def test_valid_locale_accepted(self, tmp_config, mock_client):
        """A known --locale is accepted and runs without error."""
        products = [make_product(asin="UK01", price=3.99)]
        mock_client.search_pages.return_value = iter([(products, 1, len(products))])
        result = CliRunner().invoke(
            cli, ["--locale", "uk", "search", "test"], catch_exceptions=False
        )
        assert result.exit_code == 0, result.output
