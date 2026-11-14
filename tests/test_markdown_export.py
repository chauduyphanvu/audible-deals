"""Markdown table rendering and file export."""

from __future__ import annotations

import click
import pytest

from audible_deals.presentation.markdown import render_markdown
from audible_deals.serialization import export_products, validate_export_path
from tests.conftest import make_product

HEADER = [
    "| # | Book | Price | Hours | Rating |",
    "|--:|:-----|------:|------:|:-------|",
]


def _rows(markdown: str) -> list[str]:
    """Table body lines, without the title or header rows."""
    lines = markdown.splitlines()
    return lines[lines.index(HEADER[1]) + 1 :]


def _row(product) -> str:
    (row,) = _rows(render_markdown([product]))
    return row


def _plain(**overrides):
    defaults = dict(
        asin="B0EXAMPLE1",
        title="Red Rising",
        subtitle="",
        authors=["Pierce Brown"],
        price=4.99,
        list_price=27.0,
        length_minutes=972,
        rating=4.7,
        num_ratings=98123,
        series_name="",
        series_position="",
    )
    defaults.update(overrides)
    return make_product(**defaults)


class TestRenderMarkdown:
    def test_discounted_row_matches_documented_format(self):
        assert _row(_plain()) == (
            "| 1 | [Red Rising](https://www.audible.com/pd/B0EXAMPLE1) by Pierce Brown"
            " | $4.99 ~~$27.00~~ (-82%) | 16.2 | 4.7 (98,123) |"
        )

    def test_header_and_trailing_newline(self):
        output = render_markdown([_plain()])
        assert output.splitlines()[:2] == HEADER
        assert output.endswith("|\n")
        assert not output.endswith("\n\n")

    def test_undiscounted_row_has_no_list_price(self):
        row = _row(_plain(price=5.0, list_price=5.0))
        assert "~~" not in row
        assert "$5.00" in row

    def test_list_price_below_price_is_not_shown(self):
        assert "~~" not in _row(_plain(price=6.0, list_price=5.0))

    def test_missing_list_price_is_not_shown(self):
        assert "~~" not in _row(_plain(list_price=None))

    def test_plus_title_is_marked_without_discount(self):
        row = _row(
            _plain(
                asin="B0EXAMPLE2",
                title="Some Plus Title",
                authors=["A. Writer"],
                price=3.5,
                list_price=None,
                length_minutes=540,
                rating=4.2,
                num_ratings=311,
                in_plus_catalog=True,
            )
        )
        assert row == (
            "| 1 | [Some Plus Title](https://www.audible.com/pd/B0EXAMPLE2)"
            " by A. Writer | $3.50 · Plus | 9.0 | 4.2 (311) |"
        )

    def test_plus_marker_follows_the_discount(self):
        assert "(-82%) · Plus |" in _row(_plain(in_plus_catalog=True))

    def test_series_with_position(self):
        row = _row(_plain(series_name="Red Rising Saga", series_position="1"))
        assert "by Pierce Brown · Red Rising Saga #1 |" in row

    def test_series_without_position_omits_hash(self):
        row = _row(_plain(series_name="Red Rising Saga", series_position=""))
        assert "by Pierce Brown · Red Rising Saga |" in row
        assert "#" not in row

    def test_no_authors_omits_by(self):
        row = _row(_plain(authors=[]))
        assert " by " not in row
        assert row.startswith(
            "| 1 | [Red Rising](https://www.audible.com/pd/B0EXAMPLE1) |"
        )

    def test_subtitle_is_part_of_the_link_text(self):
        assert "[Red Rising: Book One](" in _row(_plain(subtitle="Book One"))

    def test_missing_price_shows_n_a(self):
        row = _row(_plain(price=None, list_price=None))
        assert " | n/a | " in row
        assert "~~" not in row

    def test_no_ratings_leaves_rating_cell_empty(self):
        row = _row(_plain(rating=0.0, num_ratings=0))
        assert row.endswith("| 16.2 |  |")

    def test_zero_hours_leaves_hours_cell_empty(self):
        row = _row(_plain(length_minutes=0))
        assert "| $4.99 ~~$27.00~~ (-82%) |  | 4.7 (98,123) |" in row

    def test_non_usd_currency_symbol_and_domain(self):
        row = _row(_plain(locale="uk", price=3.0, list_price=12.0))
        assert "https://www.audible.co.uk/pd/B0EXAMPLE1" in row
        assert "£3.00 ~~£12.00~~ (-75%)" in row

    def test_yen_matches_the_table_view_price_format(self):
        from audible_deals.presentation.common import price_str

        product = _plain(locale="jp", price=500.0, list_price=2000.0)
        assert price_str(product.price, product.currency) in _row(product)
        assert "¥500.00 ~~¥2000.00~~ (-75%)" in _row(product)

    def test_rows_are_numbered_in_input_order(self):
        products = [_plain(asin=f"B0N{i}", title=f"Book {i}") for i in range(3)]
        rows = _rows(render_markdown(products))
        assert [row.split(" | ")[0] for row in rows] == ["| 1", "| 2", "| 3"]
        assert "[Book 0]" in rows[0] and "[Book 2]" in rows[2]

    def test_pipe_is_escaped_in_every_text_cell(self):
        row = _row(
            _plain(
                title="A|B", authors=["C|D"], series_name="E|F", series_position="1|2"
            )
        )
        assert "[A\\|B]" in row
        assert "by C\\|D" in row
        assert "E\\|F #1\\|2" in row
        # The row edges and the four column separators are the only bare pipes.
        assert row.replace("\\|", "").count("|") == 6

    def test_square_brackets_are_escaped_inside_link_text_only(self):
        row = _row(_plain(title="Vol [1]", authors=["Ed [Ed]"], series_name="S [x]"))
        assert "[Vol \\[1\\]](" in row
        assert "by Ed [Ed]" in row
        assert "· S [x]" in row

    @pytest.mark.parametrize("char", ["*", "_", "~", "`"])
    def test_emphasis_characters_are_escaped_in_title_author_and_series(self, char):
        row = _row(
            _plain(
                title=f"a{char}b",
                authors=[f"c{char}d"],
                series_name=f"e{char}f",
            )
        )
        assert f"[a\\{char}b]" in row
        assert f"by c\\{char}d" in row
        assert f"e\\{char}f" in row

    def test_backslash_is_escaped_so_it_cannot_cancel_other_escapes(self):
        assert "[a\\\\\\*b](" in _row(_plain(title="a\\*b"))

    def test_newlines_become_spaces(self):
        row = _row(_plain(title="Line one\nLine two\r\nLine three", authors=["A\nB"]))
        assert "\n" not in row and "\r" not in row
        assert "[Line one Line two Line three](" in row
        assert "by A B" in row

    def test_title_line_has_count_and_blank_line(self):
        output = render_markdown(
            [_plain(), _plain(asin="B0EXAMPLE2")], title="Sci-fi under $5"
        )
        assert output.startswith("**Sci-fi under $5** (2 books)\n\n| # | Book |")

    def test_title_line_uses_singular_for_one_book(self):
        assert render_markdown([_plain()], title="T").startswith("**T** (1 book)\n\n")

    def test_title_is_escaped(self):
        output = render_markdown([_plain()], title="Hot_deals *now*")
        assert output.startswith("**Hot\\_deals \\*now\\*** (1 book)")

    @pytest.mark.parametrize("title", [None, ""])
    def test_no_title_line_without_a_title(self, title):
        assert render_markdown([_plain()], title=title).startswith(HEADER[0])

    def test_empty_list_renders_header_rows_only(self):
        assert render_markdown([]) == "\n".join(HEADER) + "\n"

    def test_empty_list_with_title(self):
        assert render_markdown([], title="Nothing") == (
            "**Nothing** (0 books)\n\n" + "\n".join(HEADER) + "\n"
        )


class TestExportPath:
    @pytest.mark.parametrize("name", ["out.md", "out.markdown", "OUT.MD", "x.Markdown"])
    def test_markdown_suffixes_are_accepted(self, tmp_path, name):
        validate_export_path(tmp_path / name)

    def test_unsupported_suffix_lists_every_format(self, tmp_path):
        with pytest.raises(click.BadParameter) as raised:
            validate_export_path(tmp_path / "out.txt")
        message = str(raised.value.message)
        for suffix in (".json", ".csv", ".md", ".markdown"):
            assert suffix in message

    @pytest.mark.parametrize("name", ["out.md", "out.markdown"])
    def test_export_products_writes_utf8_markdown(self, tmp_path, name):
        path = tmp_path / name
        export_products([_plain(title="Café 東京", authors=["Zoë"])], path, title="Été")
        text = path.read_bytes().decode("utf-8")
        assert text == render_markdown(
            [_plain(title="Café 東京", authors=["Zoë"])], title="Été"
        )
        assert "Café 東京" in text and "Zoë" in text

    def test_export_products_without_title_has_no_title_line(self, tmp_path):
        path = tmp_path / "out.md"
        export_products([_plain()], path)
        assert path.read_text(encoding="utf-8").startswith(HEADER[0])

    def test_export_empty_list_writes_header_only(self, tmp_path):
        path = tmp_path / "empty.md"
        export_products([], path)
        assert path.read_text(encoding="utf-8") == "\n".join(HEADER) + "\n"
