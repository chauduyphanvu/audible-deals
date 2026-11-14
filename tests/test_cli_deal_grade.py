"""History grades on the history, detail, watch, result-table, and JSON surfaces."""

from __future__ import annotations

import datetime
import json
from io import StringIO

from click.testing import CliRunner
from rich.console import Console

import audible_deals.constants as constants_mod
import audible_deals.wishlist as wishlist_mod
from audible_deals.cli import cli
from audible_deals.price_history import load_price_history
from audible_deals.result_models import ResultSession
from audible_deals.result_processing import result_recipe
from audible_deals.results_cache import save_result_session
from audible_deals.serialization import serialize_product
from tests.conftest import STANDARD_WITH_DIPS, daily_history, make_product

GOOD_HEADLINE = "GOOD: second-lowest price in 102 earlier checks since"


def _seed(asin: str, entries: list[dict], locale: str = "us") -> None:
    constants_mod.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    (constants_mod.HISTORY_DIR / f"{asin}.json").write_text(
        json.dumps({"marketplaces": {locale: entries}})
    )


def _until_yesterday(*segments: tuple[int, float]) -> list[dict]:
    days = sum(count for count, _ in segments)
    start = datetime.date.today() - datetime.timedelta(days=days)
    return daily_history(*segments, start=start)


class TestHistoryCommand:
    def test_verdict_rests_on_checks_before_the_latest(self, tmp_config):
        today = datetime.date.today().isoformat()
        _seed(
            "B00HIST001",
            [
                *_until_yesterday(*STANDARD_WITH_DIPS),
                {"date": today, "price": 9.99, "title": "Book"},
            ],
        )

        result = CliRunner().invoke(cli, ["history", "B00HIST001"])

        assert result.exit_code == 0, result.output
        assert GOOD_HEADLINE in result.output
        assert "Lowest $7.49 on" in result.output
        assert "usual price $24.50" in result.output

    def test_thin_history_is_new(self, tmp_config):
        _seed("B00HIST002", _until_yesterday((3, 12.00)))

        result = CliRunner().invoke(cli, ["history", "B00HIST002"])

        assert result.exit_code == 0, result.output
        assert "NEW: not enough history yet (2 earlier checks over 2 days)" in (
            result.output
        )
        assert "deals track install builds them." in result.output
        assert all(len(line) <= 80 for line in result.output.splitlines())

    def test_json_still_emits_raw_entries(self, tmp_config):
        entries = _until_yesterday(*STANDARD_WITH_DIPS)
        _seed("B00HIST003", entries)

        result = CliRunner().invoke(cli, ["history", "B00HIST003", "--json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == entries


class TestDetailCommand:
    def test_detail_records_today_and_shows_the_verdict(self, tmp_config, mock_client):
        _seed("B00DETL001", _until_yesterday(*STANDARD_WITH_DIPS))
        mock_client.get_product.return_value = make_product(
            asin="B00DETL001", title="Detail Grade", price=9.99
        )

        result = CliRunner().invoke(cli, ["detail", "B00DETL001"])

        assert result.exit_code == 0, result.output
        assert "History:" in result.output
        assert GOOD_HEADLINE in result.output
        history = load_price_history("B00DETL001")
        assert history[-1]["date"] == datetime.date.today().isoformat()
        assert history[-1]["price"] == 9.99

    def test_first_look_records_without_a_verdict(self, tmp_config, mock_client):
        mock_client.get_product.return_value = make_product(
            asin="B00DETL002", price=12.50
        )

        result = CliRunner().invoke(cli, ["detail", "B00DETL002"])

        assert result.exit_code == 0, result.output
        assert "History:" not in result.output
        assert [entry["price"] for entry in load_price_history("B00DETL002")] == [12.50]

    def test_repeat_look_on_the_same_day_is_not_double_counted(
        self, tmp_config, mock_client
    ):
        _seed("B00DETL003", _until_yesterday((3, 12.00)))
        mock_client.get_product.return_value = make_product(
            asin="B00DETL003", price=12.00
        )

        first = CliRunner().invoke(cli, ["detail", "B00DETL003"])
        second = CliRunner().invoke(cli, ["detail", "B00DETL003"])

        for result in (first, second):
            assert result.exit_code == 0, result.output
            assert "NEW: not enough history yet (3 earlier checks" in result.output
        assert len(load_price_history("B00DETL003")) == 4

    def test_unavailable_product_records_nothing(self, tmp_config, mock_client):
        mock_client.get_product.return_value = make_product(
            asin="B00DETL004", price=None
        )

        result = CliRunner().invoke(cli, ["detail", "B00DETL004"])

        assert result.exit_code == 0, result.output
        assert "History:" not in result.output
        assert load_price_history("B00DETL004") == []


class TestWatchCommand:
    def test_watch_rows_carry_the_grade(self, tmp_config, mock_client):
        wishlist_mod.save_wishlist(
            [
                {"asin": "B00WTCH001", "title": "Graded", "max_price": 5.0},
                {"asin": "B00WTCH002", "title": "Fresh", "max_price": 5.0},
            ]
        )
        _seed("B00WTCH001", _until_yesterday(*STANDARD_WITH_DIPS))
        mock_client.get_products_batch.return_value = [
            make_product(asin="B00WTCH001", title="Graded", price=27.99),
            make_product(asin="B00WTCH002", title="Fresh", price=4.00),
        ]

        result = CliRunner().invoke(cli, ["watch"])

        assert result.exit_code == 0, result.output
        assert "high" in result.output
        assert "BUY" in result.output
        assert "new" not in result.output.lower()
        assert load_price_history("B00WTCH001")[-1]["price"] == 27.99


class TestResultSurfaces:
    def test_find_table_grades_against_prior_days(self, tmp_config, mock_client):
        _seed("B00FIND001", _until_yesterday(*STANDARD_WITH_DIPS))
        mock_client.search_pages.return_value = iter(
            [([make_product(asin="B00FIND001", price=4.99, series_name="")], 1, 1)]
        )

        result = CliRunner().invoke(cli, ["find", "--pages", "1"])

        assert result.exit_code == 0, result.output
        assert "History lowest" in result.output
        assert "★" not in result.output
        assert "vs hist" not in result.output

    def test_json_output_keeps_its_fields_and_adds_no_grade(
        self, tmp_config, mock_client
    ):
        product = make_product(asin="B00JSON001", price=4.99, series_name="")
        _seed(product.asin, _until_yesterday(*STANDARD_WITH_DIPS))
        mock_client.search_pages.return_value = iter([([product], 1, 1)])

        found = CliRunner().invoke(cli, ["find", "--pages", "1", "--json"])
        cached = CliRunner().invoke(cli, ["last", "--json"])

        for result in (found, cached):
            assert result.exit_code == 0, result.output
            assert json.loads(result.stdout) == [serialize_product(product)]

    def test_last_grades_cached_prices_at_their_session_date(self, tmp_config):
        product = make_product(asin="B00LAST001", price=9.99, series_name="")
        entries = daily_history((37, 24.50), start=datetime.date(2026, 2, 1))
        entries += [
            {"date": "2026-03-10", "price": 9.99, "title": "Book"},
            {"date": "2026-03-12", "price": 5.99, "title": "Book"},
        ]
        _seed(product.asin, entries)
        recipe = result_recipe(limit=0)
        session = ResultSession(
            producer="find",
            locale="us",
            title="Cached deals",
            source={"command": "deals find --pages 1", "pages": 1},
            candidates=[serialize_product(product)],
            baseline_recipe=recipe,
            current_recipe=recipe,
            visible_asins=[product.asin],
            constraints={"drop_zero_length": True},
            timestamp="2026-03-10T12:00:00+00:00",
        )
        save_result_session(session)

        result = CliRunner().invoke(cli, ["last"])

        assert result.exit_code == 0, result.output
        # Graded against the 37 checks before 10 Mar: the later 5.99 refresh
        # and the session's own backfilled entry do not count.
        assert "History lowest" in result.output


class TestInteractiveHistory:
    def test_h_shows_the_verdict(self, tmp_config, monkeypatch):
        import audible_deals.cli.interactive as interactive_mod
        from audible_deals.presentation import terminal

        today = datetime.date.today().isoformat()
        product = make_product(asin="B00INTR001", price=9.99)
        _seed(
            product.asin,
            [
                *_until_yesterday(*STANDARD_WITH_DIPS),
                {"date": today, "price": 9.99, "title": "Book"},
            ],
        )
        output = StringIO()
        monkeypatch.setattr(
            terminal, "console", Console(file=output, width=100, force_terminal=False)
        )
        browser = interactive_mod.InteractiveBrowser([product])

        assert browser.dispatch(interactive_mod.InteractiveCommand("history", (0,)))

        assert GOOD_HEADLINE in output.getvalue()
