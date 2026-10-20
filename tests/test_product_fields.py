"""The response groups requested from Audible cover what the parser reads."""

from __future__ import annotations

import ast
from pathlib import Path

from audible_deals import constants, product

#: Top-level API fields parse_product reads, and the response group that
#: carries them. The list requested in CATALOG_RESPONSE_GROUPS is a superset:
#: product_desc and media are asked for too although nothing here reads them
#: (dropping them would shrink responses, but the mapping below has not been
#: checked against a live account; see PROGRESS.md and the changelog).
FIELD_GROUPS = {
    "asin": "product_attrs",
    "title": "product_attrs",
    "subtitle": "product_attrs",
    "publisher_name": "product_attrs",
    "runtime_length_min": "product_attrs",
    "language": "product_attrs",
    "release_date": "product_attrs",
    "authors": "contributors",
    "narrators": "contributors",
    "rating": "rating",
    "category_ladders": "category_ladders",
    "series": "series",
    "plans": "product_plans",
    "price": "price",
    "list_price": "price",
}


def fields_read_by_the_parser() -> set[str]:
    tree = ast.parse(Path(product.__file__).read_text(encoding="utf-8"))
    fields = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "raw"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            fields.add(node.args[0].value)
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "raw"
            and isinstance(node.slice, ast.Constant)
        ):
            fields.add(node.slice.value)
    return fields


def test_the_parser_reads_only_fields_whose_group_is_requested():
    requested = set(constants.CATALOG_RESPONSE_GROUPS.split(","))
    read = fields_read_by_the_parser()

    unknown = read - FIELD_GROUPS.keys()
    assert not unknown, (
        f"parse_product now reads {sorted(unknown)}: find the response group that "
        "carries them, add it to CATALOG_RESPONSE_GROUPS and to FIELD_GROUPS here"
    )
    assert {FIELD_GROUPS[field] for field in read} <= requested
