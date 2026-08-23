"""Product serialization, export, and atomic file write utilities."""

from __future__ import annotations

import csv
import dataclasses
import json as json_mod
import logging
from dataclasses import asdict
from pathlib import Path

import click

from audible_deals.product import Product
from audible_deals.metrics import price_per_hour

logger = logging.getLogger(__name__)


def sanitize_csv_cell(value):
    """Neutralize text that spreadsheet applications may execute as a formula."""
    if not isinstance(value, str) or not value:
        return value
    stripped = value.lstrip()
    if value[0] in "\t\r\n" or (stripped and stripped[0] in "=+-@"):
        return "'" + value
    return value


def serialize_product(p: Product) -> dict:
    """Convert a Product to a plain dict for export."""
    d = asdict(p)
    if d["price"] is not None:
        d["price"] = round(d["price"], 2)
    if d["list_price"] is not None:
        d["list_price"] = round(d["list_price"], 2)
    d["full_title"] = p.full_title
    d["hours"] = p.hours
    d["discount_pct"] = p.discount_pct
    pph = price_per_hour(p)
    d["price_per_hour"] = round(pph, 2) if pph != float("inf") else None
    d["url"] = p.url
    return d


PRODUCT_FIELDS: frozenset[str] = frozenset(f.name for f in dataclasses.fields(Product))


def validate_export_path(path: Path | None) -> None:
    """Reject unsupported export formats before command side effects."""
    if path is not None and path.suffix.lower() not in {".json", ".csv"}:
        raise click.BadParameter(
            f"Unsupported extension '{path.suffix.lower()}'. Use .json or .csv.",
            param_hint="--output",
        )


def deserialize_product(d: dict) -> Product | None:
    """Reconstruct a Product from a serialized dict, ignoring computed fields."""
    try:
        return Product(**{k: v for k, v in d.items() if k in PRODUCT_FIELDS})
    except TypeError:
        logger.warning(
            "deserialize_product failed for asin=%r", d.get("asin"), exc_info=True
        )
        return None


def export_products(products: list[Product], path: Path) -> None:
    """Export products to file, detecting format from extension."""
    validate_export_path(path)
    suffix = path.suffix.lower()
    rows = [serialize_product(p) for p in products]
    logger.debug("export_products format=%s count=%d path=%s", suffix, len(rows), path)

    if suffix == ".json":
        path.write_text(
            json_mod.dumps(rows, indent=2, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
    elif suffix == ".csv":
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        for row in rows:
            for key in ("authors", "narrators", "categories", "category_ids"):
                if isinstance(row[key], list):
                    row[key] = "; ".join(str(v) for v in row[key])
            for key, value in row.items():
                row[key] = sanitize_csv_cell(value)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
