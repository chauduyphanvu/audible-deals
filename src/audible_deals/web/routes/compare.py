"""Compare and detail blueprints — product detail and side-by-side comparison."""

from __future__ import annotations

import json

from flask import Blueprint, abort, current_app, render_template, request

from audible_deals.cli import HISTORY_DIR, _validate_asin
from audible_deals.client import DealsClient

from . import get_locale

bp = Blueprint("compare", __name__)


@bp.get("/detail/<asin>")
def detail_page(asin: str):
    """Show rich detail view for a single product."""
    try:
        _validate_asin(asin)
    except Exception:
        abort(400, "Invalid ASIN")

    locale = get_locale()

    try:
        with DealsClient(locale=locale) as dc:
            product = dc.get_product(asin)
    except ValueError:
        abort(404, f"Product not found: {asin}")
    except Exception as exc:
        abort(502, str(exc))

    # Load price history if available
    history: list[dict] = []
    try:
        history = json.loads((HISTORY_DIR / f"{asin}.json").read_text())
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        pass

    return render_template(
        "detail.html",
        product=product,
        history=history,
        active_page=None,
    )


@bp.get("/compare")
def compare_page():
    """Side-by-side comparison of multiple products."""
    raw = request.args.get("asins", "")
    asin_list = [a.strip() for a in raw.split(",") if a.strip()]

    # Validate all ASINs before making any API calls
    valid_asins = []
    for asin in asin_list:
        try:
            _validate_asin(asin)
            valid_asins.append(asin)
        except Exception:
            pass  # skip invalid ASINs silently

    products = []
    error = None
    if valid_asins:
        locale = get_locale()
        try:
            with DealsClient(locale=locale) as dc:
                products = dc.get_products_batch(valid_asins)
            # Preserve the order the user supplied
            order = {a: i for i, a in enumerate(valid_asins)}
            products.sort(key=lambda p: order.get(p.asin, 999))
        except Exception as exc:
            error = str(exc)

    return render_template(
        "compare.html",
        products=products,
        asins=valid_asins,
        error=error,
        active_page=None,
    )
