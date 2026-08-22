"""Tests for the catalog find command."""

from __future__ import annotations

import datetime
import json

import pytest
from click.testing import CliRunner

import audible_deals.config_store as config_store_mod
import audible_deals.constants as constants_mod
from audible_deals.cli import cli
from tests.conftest import make_product


def _seed_price_history(asin: str, prices: list[float]) -> None:
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


def _capture_history_context(monkeypatch):
    """Patch display_products to record the hist_context it receives."""
    import audible_deals.presentation.result_output as result_output_mod

    captured: dict[str, dict] = {}

    def fake_display_products(filtered, **kwargs):
        captured["hist_context"] = kwargs.get("hist_context")

    monkeypatch.setattr(result_output_mod, "display_products", fake_display_products)
    return captured


class TestFindCommand:
    def test_find_basic(self, mock_client, tmp_config):
        products = [
            make_product(asin=f"F{i}", price=float(i), list_price=20.0)
            for i in range(1, 6)
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 5)])
        mock_client.resolve_genre.return_value = ("cat1", "Fiction")

        runner = CliRunner()
        result = runner.invoke(
            cli, ["find", "--genre", "fiction", "--max-price", "10", "--pages", "1"]
        )
        assert result.exit_code == 0, result.output
        assert "Deals under $10.00" in result.output

    def test_find_json_output(self, mock_client, tmp_config):
        products = [make_product(asin="J1", price=3.0)]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])

        out_file = tmp_config / "out.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 1
        assert data[0]["asin"] == "J1"

    def test_find_limit(self, mock_client, tmp_config):
        products = [
            make_product(
                asin=f"L{i}", price=float(i), series_name="", series_position=""
            )
            for i in range(1, 11)
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 10)])

        out_file = tmp_config / "limit.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "20",
                "--pages",
                "1",
                "--limit",
                "3",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 3

    def test_find_quiet(self, mock_client, tmp_config):
        products = [make_product(price=3.0)]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "-q",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Deals under" not in result.output

    def test_genre_category_conflict(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--genre", "sci-fi", "--category", "123"])
        assert result.exit_code != 0
        assert "not both" in result.output

    def test_output_implies_quiet(self, mock_client, tmp_config):
        """When -o is set without -q, quiet should be implied (no table in stdout)."""
        products = [make_product(price=3.0, series_name="", series_position="")]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        out_file = tmp_config / "implied.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        # Table header should NOT appear in console output
        assert "Deals under" not in result.output

    def test_output_explicit_no_quiet_override(self, mock_client, tmp_config):
        """Explicitly passing --no-quiet (or just not passing -q) with -o does imply quiet."""
        products = [make_product(price=3.0, series_name="", series_position="")]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        out_file = tmp_config / "noquiet.json"
        runner = CliRunner()
        # Passing -q explicitly should still suppress table
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--output",
                str(out_file),
                "-q",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Deals under" not in result.output


class TestFindTitleIncludesGenre:
    def test_find_title_with_genre(self, mock_client, tmp_config):
        """find --genre shows category name in the table title."""
        products = [
            make_product(asin="GT1", price=3.0, series_name="", series_position="")
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        mock_client.resolve_genre.return_value = ("cat42", "Science Fiction")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--genre",
                "sci-fi",
                "--max-price",
                "10",
                "--pages",
                "1",
                "--all-languages",
                "-n",
                "0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Science Fiction" in result.output

    def test_find_title_without_genre(self, mock_client, tmp_config):
        """find without --genre does not include a category in title."""
        products = [
            make_product(asin="NT1", price=3.0, series_name="", series_position="")
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])

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
                "-n",
                "0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Deals under $10.00" in result.output


class TestFindDefaultLimit:
    def test_find_default_limit_25(self, mock_client, tmp_config):
        """find without --limit defaults to 25 results."""
        products = [
            make_product(
                asin=f"DL{i:02d}",
                price=float(i),
                series_name="",
                series_position="",
                num_ratings=10,
            )
            for i in range(1, 36)
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 35)])
        out_file = tmp_config / "default_limit.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "100",
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
        assert len(data) == 25

    def test_find_limit_zero_means_unlimited(self, mock_client, tmp_config):
        """find -n 0 shows all results (unlimited)."""
        products = [
            make_product(
                asin=f"UL{i:02d}",
                price=float(i),
                series_name="",
                series_position="",
                num_ratings=10,
            )
            for i in range(1, 36)
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 35)])
        out_file = tmp_config / "unlimited.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "100",
                "--pages",
                "1",
                "-n",
                "0",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 35

    def test_search_default_limit_25(self, mock_client, tmp_config):
        """search defaults to limit=25 (same as find)."""
        products = [
            make_product(
                asin=f"SL{i:02d}", price=float(i), series_name="", series_position=""
            )
            for i in range(1, 36)
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 35)])
        out_file = tmp_config / "search_default_limit.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "test",
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
        assert len(data) == 25

    def test_search_limit_zero_means_unlimited(self, mock_client, tmp_config):
        """search -n 0 shows all results (unlimited)."""
        products = [
            make_product(
                asin=f"SL{i:02d}", price=float(i), series_name="", series_position=""
            )
            for i in range(1, 36)
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 35)])
        out_file = tmp_config / "search_unlimited.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "search",
                "test",
                "--pages",
                "1",
                "-n",
                "0",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        assert len(data) == 35


class TestFindDefaults:
    def test_find_default_sort_price_per_hour(self, mock_client, tmp_config):
        """find without --sort uses price-per-hour ordering."""
        products = [
            # A: $10 / 2hrs = $5/hr
            make_product(
                asin="PPH_A",
                price=10.0,
                length_minutes=120,
                series_name="",
                series_position="",
                num_ratings=10,
            ),
            # B: $3 / 10hrs = $0.30/hr (better value)
            make_product(
                asin="PPH_B",
                price=3.0,
                length_minutes=600,
                series_name="",
                series_position="",
                num_ratings=10,
            ),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "pph_sort.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "100",
                "--pages",
                "1",
                "-n",
                "0",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        # PPH_B has lower price-per-hour and should appear first
        assert data[0]["asin"] == "PPH_B"
        assert data[1]["asin"] == "PPH_A"

    def test_find_default_min_ratings_filters_unreviewed(self, mock_client, tmp_config):
        """find with default min-ratings=1 filters out items with 0 ratings."""
        products = [
            make_product(
                asin="MR1", price=3.0, num_ratings=0, series_name="", series_position=""
            ),
            make_product(
                asin="MR2", price=3.0, num_ratings=5, series_name="", series_position=""
            ),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 2)])
        out_file = tmp_config / "min_ratings.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--max-price",
                "10",
                "--pages",
                "1",
                "-n",
                "0",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert "MR1" not in asins
        assert "MR2" in asins


class TestDryRunFind:
    def test_find_dry_run_shows_summary(self, mock_client, tmp_config):
        """find --dry-run prints scan summary and does not call search_pages."""
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--dry-run", "--pages", "5"])
        assert result.exit_code == 0, result.output
        assert "Dry run" in result.output
        assert "Sort orders" in result.output
        assert "Pages per sort" in result.output
        assert "API calls" in result.output
        mock_client.search_pages.assert_not_called()

    def test_find_dry_run_shows_category(self, mock_client, tmp_config):
        """find --dry-run with genre resolved shows category name."""
        mock_client._categories_cache = [
            {"id": "cat1", "name": "Mystery, Thriller & Suspense"}
        ]
        mock_client.resolve_genre.return_value = (
            "cat1",
            "Mystery, Thriller & Suspense",
        )
        mock_client.__enter__ = lambda s: s
        mock_client.__exit__ = lambda s, *a: False

        # Bypass real genre resolution by using --category
        runner = CliRunner()
        result = runner.invoke(
            cli, ["find", "--dry-run", "--pages", "2", "--category", "cat1"]
        )
        assert result.exit_code == 0, result.output
        assert "Dry run" in result.output
        mock_client.search_pages.assert_not_called()

    def test_find_dry_run_with_category_never_constructs_client(
        self, tmp_config, monkeypatch
    ):
        """Dry runs do not resolve categories through the authenticated client."""
        import audible_deals.cli.catalog as catalog_mod

        monkeypatch.setattr(
            catalog_mod,
            "_get_client",
            lambda locale: pytest.fail("dry run constructed a client"),
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--category", "cat1", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "resolved during scan" in result.output

    def test_dry_run_shows_effective_settings_and_filters(self, tmp_config):
        config_store_mod.save_config(
            {"max_price": 9.0, "sort": "rating", "limit": 40, "skip_owned": True}
        )
        config_store_mod.save_profiles(
            {
                "strict": {
                    "max_price": 7.0,
                    "sort": "title",
                    "limit": 10,
                    "min_rating": 4.2,
                    "exclude_authors": ["Blocked Author"],
                }
            }
        )

        result = CliRunner().invoke(
            cli,
            [
                "find",
                "--profile",
                "strict",
                "--max-price",
                "5",
                "--sort",
                "discount",
                "--limit",
                "3",
                "--on-sale",
                "--released-after",
                "2025-01-01",
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Result sort: discount" in result.output
        assert "Limit: 3" in result.output
        assert "Profile: strict" in result.output
        assert "max-price=5.0" in result.output
        assert "min-rating=4.2" in result.output
        assert "on-sale=yes" in result.output
        assert "skip-owned=yes" in result.output
        assert "exclude-authors=Blocked Author" in result.output
        assert "released-after=2025-01-01" in result.output
        filters = result.output.split("Filters: ", 1)[1].splitlines()[0]
        assert "; " in filters


class TestFindSubcategories:
    def _make_search_side_effect(self, products_by_call: list[list]):
        """Return a side_effect that yields successive product lists."""
        call_idx = 0

        def _side_effect(**kwargs):
            nonlocal call_idx
            batch = products_by_call[call_idx % len(products_by_call)]
            call_idx += 1
            yield batch, 1, len(batch)

        return _side_effect

    def test_subcategories_scans_each_child(self, mock_client, tmp_config):
        """--subcategories calls get_categories and scans each child id."""
        child1 = make_product(
            asin="SUB1", price=2.0, series_name="", series_position=""
        )
        child2 = make_product(
            asin="SUB2", price=3.0, series_name="", series_position=""
        )

        mock_client.resolve_genre.return_value = ("parent1", "Sci-Fi")
        mock_client.get_categories.return_value = [
            {"id": "child1", "name": "Space Opera"},
            {"id": "child2", "name": "Cyberpunk"},
        ]

        call_order: list[str] = []

        def fake_search_pages(**kwargs):
            call_order.append(kwargs["category_id"])
            batch = [child1] if kwargs["category_id"] == "child1" else [child2]
            yield batch, 1, 1

        mock_client.search_pages.side_effect = fake_search_pages

        out_file = tmp_config / "sub.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--genre",
                "sci-fi",
                "--subcategories",
                "--pages",
                "1",
                "--max-price",
                "10",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        mock_client.get_categories.assert_called_once_with(root="parent1")
        assert set(call_order) == {"child1", "child2"}
        data = json.loads(out_file.read_text())
        asins = {d["asin"] for d in data}
        assert asins == {"SUB1", "SUB2"}

    def test_subcategories_no_children_falls_back(self, mock_client, tmp_config):
        """No children → scans the parent and prints the notice."""
        products = [
            make_product(asin="FB1", price=2.0, series_name="", series_position="")
        ]

        mock_client.resolve_genre.return_value = ("parent2", "Mystery")
        mock_client.get_categories.return_value = []
        mock_client.search_pages.return_value = iter([(products, 1, 1)])

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--genre",
                "mystery",
                "--subcategories",
                "--pages",
                "1",
                "--max-price",
                "10",
                "--all-languages",
                "-q",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "No subcategories found" in result.output
        called_ids = [
            c.kwargs["category_id"] for c in mock_client.search_pages.call_args_list
        ]
        assert called_ids == ["parent2"]

    def test_subcategories_without_genre_raises(self, mock_client, tmp_config):
        """--subcategories without --genre/--category raises UsageError."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--subcategories",
                "--pages",
                "1",
                "--max-price",
                "10",
            ],
        )
        assert result.exit_code != 0
        assert "--subcategories requires --genre or --category" in result.output

    def test_subcategories_dedup_across_children(self, mock_client, tmp_config):
        """Same ASIN in two subcategories appears only once."""
        shared = make_product(
            asin="DEDUP", price=2.0, series_name="", series_position=""
        )
        unique = make_product(
            asin="UNIQ", price=3.0, series_name="", series_position=""
        )

        mock_client.resolve_genre.return_value = ("parent3", "Fantasy")
        mock_client.get_categories.return_value = [
            {"id": "c1", "name": "Epic Fantasy"},
            {"id": "c2", "name": "Urban Fantasy"},
        ]

        def fake_search_pages(**kwargs):
            if kwargs["category_id"] == "c1":
                yield [shared, unique], 1, 2
            else:
                yield [shared], 1, 1

        mock_client.search_pages.side_effect = fake_search_pages

        out_file = tmp_config / "dedup.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--genre",
                "fantasy",
                "--subcategories",
                "--pages",
                "1",
                "--max-price",
                "10",
                "--all-languages",
                "-q",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out_file.read_text())
        asins = [d["asin"] for d in data]
        assert asins.count("DEDUP") == 1
        assert "UNIQ" in asins

    def test_subcategories_dry_run_marks_live_counts_unknown(
        self, mock_client, tmp_config
    ):
        """Dry run with subcategories avoids fetching the live category tree."""
        mock_client.resolve_genre.return_value = ("parent4", "Romance")
        mock_client.get_categories.return_value = [
            {"id": "r1", "name": "Contemporary"},
            {"id": "r2", "name": "Historical"},
            {"id": "r3", "name": "Paranormal"},
        ]

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--genre",
                "romance",
                "--subcategories",
                "--pages",
                "2",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Subcategories: unknown" in result.output
        assert "API calls: unknown" in result.output
        mock_client.get_categories.assert_not_called()


class TestRequireHistoryCLI:
    def test_require_history_without_hist_filter_raises_usage_error_find(
        self, tmp_config
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--require-history"])
        assert result.exit_code == 2
        assert "--require-history requires" in result.output

    def test_require_history_without_hist_filter_raises_usage_error_search(
        self, tmp_config
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "test", "--require-history"])
        assert result.exit_code == 2
        assert "--require-history requires" in result.output

    def test_require_history_with_hist_below_accepted(self, mock_client, tmp_config):
        mock_client.search_pages.return_value = iter([([], 1, 0)])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--require-history",
                "--hist-below",
                "50",
                "--pages",
                "1",
                "-q",
            ],
        )
        assert result.exit_code == 0, result.output

    def test_require_history_with_min_price_drop_accepted(
        self, mock_client, tmp_config
    ):
        mock_client.search_pages.return_value = iter([([], 1, 0)])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--require-history",
                "--min-price-drop",
                "10",
                "--pages",
                "1",
                "-q",
            ],
        )
        assert result.exit_code == 0, result.output


class TestReleasedDateCLI:
    def test_invalid_released_after_raises_usage_error(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--released-after", "not-a-date"])
        assert result.exit_code == 2
        assert "invalid date" in result.output

    def test_invalid_released_before_raises_usage_error(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--released-before", "2024/01/01"])
        assert result.exit_code == 2
        assert "invalid date" in result.output

    def test_valid_released_after_accepted(self, mock_client, tmp_config):
        mock_client.search_pages.return_value = iter([([], 1, 0)])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["find", "--released-after", "2024-01-01", "--pages", "1", "-q"],
        )
        assert result.exit_code == 0, result.output

    def test_valid_released_before_accepted(self, mock_client, tmp_config):
        mock_client.search_pages.return_value = iter([([], 1, 0)])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["find", "--released-before", "2024-12-31", "--pages", "1", "-q"],
        )
        assert result.exit_code == 0, result.output

    def test_invalid_released_after_search_raises_usage_error(self, tmp_config):
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "test", "--released-after", "bad"])
        assert result.exit_code == 2
        assert "invalid date" in result.output

    @pytest.mark.parametrize("command", [["find"], ["search", "test"]])
    def test_inverted_release_window_is_rejected_before_client_creation(
        self, command, tmp_config, monkeypatch
    ):
        import audible_deals.cli.catalog as catalog_mod

        monkeypatch.setattr(
            catalog_mod,
            "_get_client",
            lambda locale: pytest.fail("inverted dates constructed a client"),
        )
        result = CliRunner().invoke(
            cli,
            [
                *command,
                "--released-after",
                "2025-01-02",
                "--released-before",
                "2025-01-01",
                "--dry-run",
            ],
        )

        assert result.exit_code == 2
        assert "cannot be later" in result.output

    @pytest.mark.parametrize("command", [["find"], ["search", "test"]])
    def test_equal_release_bounds_are_valid(self, command, tmp_config):
        result = CliRunner().invoke(
            cli,
            [
                *command,
                "--released-after",
                "2025-01-01",
                "--released-before",
                "2025-01-01",
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output


class TestCreditAdviceInFind:
    def test_buy_column_with_config(self, mock_client, tmp_config):
        config_store_mod.save_config({"credit_price": 11.25})
        products = [
            make_product(asin="CR1", price=24.99, series_name="", series_position=""),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        runner = CliRunner()
        result = runner.invoke(
            cli, ["find", "--pages", "1", "--all-languages", "--max-price", "30"]
        )
        assert result.exit_code == 0, result.output
        assert "Buy" in result.output
        assert "credit" in result.output

    def test_no_buy_column_without_config(self, mock_client, tmp_config):
        products = [
            make_product(asin="CR2", price=3.99, series_name="", series_position=""),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "--pages", "1", "--all-languages"])
        assert result.exit_code == 0, result.output
        assert "Buy" not in result.output

    def test_max_effective_price_filter(self, mock_client, tmp_config):
        config_store_mod.save_config({"credit_price": 11.25})
        products = [
            make_product(asin="CR3", price=24.99, series_name="", series_position=""),
            make_product(asin="CR4", price=14.99, series_name="", series_position=""),
        ]
        mock_client.search_pages.return_value = iter([(products, 1, 1)])
        out_file = tmp_config / "eff.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "find",
                "--pages",
                "1",
                "--all-languages",
                "--max-price",
                "30",
                "--max-effective-price",
                "12",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0, result.output
        asins = [d["asin"] for d in json.loads(out_file.read_text())]
        # Both cost one credit (11.25 effective), so both pass the 12 cap
        assert asins == ["CR3", "CR4"] or set(asins) == {"CR3", "CR4"}


class TestHistoricalMedianBadgeFlagIndependence:
    def test_vs_median_independent_of_hist_below_flag(
        self, mock_client, tmp_config, monkeypatch
    ):
        # Exactly 2 prior on-disk entries. The 'vs median' badge requires >=3
        # entries; today's just-recorded price must be excluded (matching ATL),
        # so the badge must be absent in BOTH runs regardless of --hist-below.
        product = make_product(asin="F1", price=5.0, series_name="", series_position="")

        def reset_and_run(args):
            _seed_price_history("F1", [9.0, 8.0])
            mock_client.search_pages.return_value = iter([([product], 1, 1)])
            captured = _capture_history_context(monkeypatch)
            runner = CliRunner()
            result = runner.invoke(cli, args)
            assert result.exit_code == 0, result.output
            return captured["hist_context"]

        plain = reset_and_run(["find", "--pages", "1"])
        with_flag = reset_and_run(["find", "--pages", "1", "--hist-below", "100"])

        assert plain == with_flag
        assert "F1" not in plain
