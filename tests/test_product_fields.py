"""The response groups requested from Audible cover exactly what the parser reads."""

from __future__ import annotations

import ast
from pathlib import Path

from audible_deals import constants, product

#: Top-level API fields parse_product reads, and the response group that
#: carries them (see the note above CATALOG_RESPONSE_GROUPS).
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


def test_the_unused_heavy_groups_are_not_requested():
    requested = constants.CATALOG_RESPONSE_GROUPS.split(",")
    assert "product_desc" not in requested
    assert "media" not in requested
    assert len(requested) == len(set(requested))


def test_every_requested_group_is_used_by_a_field():
    # product_plan_details accompanies product_plans (Audible Plus detection).
    needed = set(FIELD_GROUPS.values()) | {"product_plan_details"}
    assert set(constants.CATALOG_RESPONSE_GROUPS.split(",")) == needed
