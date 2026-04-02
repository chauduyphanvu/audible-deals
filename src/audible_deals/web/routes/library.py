"""Library blueprint — browse and filter the user's Audible library."""

from __future__ import annotations

import json

from flask import Blueprint, Response, render_template, request, stream_with_context

from audible_deals.cli import (
    _filter_products,
    _sort_local,
)
from audible_deals.client import DealsClient

from . import get_locale

bp = Blueprint("library", __name__)


def _safe_float(key: str, default: float = 0.0) -> float:
    try:
        return float(request.args.get(key, default) or default)
    except (ValueError, TypeError):
        return default


def _safe_int(key: str, default: int = 0) -> int:
    try:
        return int(request.args.get(key, default) or default)
    except (ValueError, TypeError):
        return default


def _get_library_params() -> dict:
    """Parse and validate library filter params from request.args."""
    return {
        "author": request.args.get("author", "").strip(),
        "narrator": request.args.get("narrator", "").strip(),
        "genre": request.args.get("genre", "").strip(),
        "min_rating": _safe_float("min_rating"),
        "min_ratings": _safe_int("min_ratings"),
        "min_hours": _safe_float("min_hours"),
        "sort": request.args.get("sort", "date"),
        "limit": _safe_int("limit"),
    }


def _filter_and_render(all_products, params, locale):
    """Apply filters, sort, limit, and render the library table partial."""
    filtered, _ = _filter_products(
        all_products,
        author=params["author"],
        narrator=params["narrator"],
        genre=params["genre"],
        min_rating=params["min_rating"],
        min_ratings=params["min_ratings"],
        min_hours=params["min_hours"],
    )
    filtered = _sort_local(filtered, params["sort"])
    total = len(all_products)
    total_before_limit = len(filtered)

    if params["limit"] > 0:
        filtered = filtered[:params["limit"]]

    return render_template(
        "partials/_library_table.html",
        products=filtered,
        total=total,
        total_before_limit=total_before_limit,
        error=None,
        locale=locale,
    )


@bp.get("/library")
def library_page():
    return render_template("library.html", active_page="library")


@bp.get("/hx/library/results")
def library_results():
    """HTMX endpoint: fetch & filter the library, return the results partial."""
    locale = get_locale()
    params = _get_library_params()

    try:
        with DealsClient(locale=locale) as dc:
            all_products = dc.get_library()
    except Exception as exc:
        return render_template(
            "partials/_library_table.html",
            products=[],
            total=0,
            error=str(exc),
        )

    return _filter_and_render(all_products, params, locale)


@bp.get("/library/stream")
def library_stream():
    """SSE endpoint: stream library loading progress, then emit the results partial."""
    locale = get_locale()
    params = _get_library_params()

    def _generate():
        all_products = []
        error = None

        try:
            with DealsClient(locale=locale) as dc:
                for page_products, page_num in dc.get_library_pages():
                    all_products.extend(page_products)
                    progress_data = json.dumps(
                        {"page": page_num, "count": len(all_products)}
                    )
                    yield f"event: progress\ndata: {progress_data}\n\n"

        except Exception as exc:
            error = str(exc)

        if error:
            html = render_template(
                "partials/_library_table.html",
                products=[],
                total=0,
                total_before_limit=0,
                error=error,
                locale=locale,
            )
        else:
            html = _filter_and_render(all_products, params, locale)

        escaped = html.replace("\n", " ")
        yield f"event: results\ndata: {escaped}\n\n"

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
