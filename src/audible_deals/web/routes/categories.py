"""Categories blueprint — browse Audible categories."""

from __future__ import annotations

from flask import Blueprint, current_app, render_template, request

from audible_deals.client import DealsClient

from . import get_locale

bp = Blueprint("categories", __name__)


@bp.get("/categories")
def categories_page():
    locale = get_locale()
    categories = []
    error = None
    try:
        with DealsClient(locale=locale) as dc:
            categories = dc.get_categories()
    except Exception as exc:
        error = str(exc)
    return render_template(
        "categories.html",
        categories=categories,
        error=error,
        active_page="categories",
    )


@bp.get("/hx/categories")
def categories_list():
    """HTMX endpoint: fetch subcategories for a given parent."""
    locale = get_locale()
    parent = request.args.get("parent", "").strip()
    categories = []
    error = None
    try:
        with DealsClient(locale=locale) as dc:
            categories = dc.get_categories(root=parent)
    except Exception as exc:
        error = str(exc)
    return render_template(
        "partials/_categories_list.html",
        categories=categories,
        parent=parent,
        error=error,
    )
