"""Product serialization, export, and atomic file write utilities."""

from __future__ import annotations

import csv
import dataclasses
import json as json_mod
from dataclasses import asdict
from pathlib import Path

import click

from audible_deals.client import Product
from audible_deals.filtering import price_per_hour


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


def deserialize_product(d: dict) -> Product | None:
    """Reconstruct a Product from a serialized dict, ignoring computed fields."""
    try:
        return Product(**{k: v for k, v in d.items() if k in PRODUCT_FIELDS})
    except TypeError:
        return None


def export_products(products: list[Product], path: Path) -> None:
    """Export products to file, detecting format from extension."""
    suffix = path.suffix.lower()
    rows = [serialize_product(p) for p in products]

    if suffix == ".json":
        path.write_text(json_mod.dumps(rows, indent=2, ensure_ascii=False))
    elif suffix == ".csv":
        if not rows:
            path.write_text("")
            return
        for row in rows:
            for key in ("authors", "narrators", "categories", "category_ids"):
                if isinstance(row[key], list):
                    row[key] = "; ".join(str(v) for v in row[key])
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    else:
        raise click.BadParameter(
            f"Unsupported extension '{suffix}'. Use .json or .csv.",
            param_hint="--output",
        )
