"""Search and Find Deals routes for the web UI."""

from __future__ import annotations

import csv
import io
import json
import math
from typing import Generator

from flask import (
    Blueprint,
    Response,
    current_app,
    render_template,
    request,
    stream_with_context,
)
from markupsafe import escape as _html_escape

from audible_deals.cli import (
    DEEP_SORT_ORDERS,
    LAST_RESULTS_FILE,
    SORT_OPTIONS,
    _atomic_write,
    _dedupe_editions,
    _deserialize_product,
    _filter_products,
    _first_in_series,
    _load_config,
    _load_last_results,
    _load_profiles,
    _record_prices,
    _serialize_product,
    _sort_local,
)
from audible_deals.client import LOCALE_CURRENCY, DealsClient

bp = Blueprint("search", __name__)

_NEWLINE = "\n"

# Sort choices available in the UI
_SORT_CHOICES = [
    ("price", "Price (low to high)"),
    ("-price", "Price (high to low)"),
    ("price-per-hour", "$/hr (best value)"),
    ("discount", "Discount %"),
    ("rating", "Rating"),
    ("length", "Length"),
    ("date", "Release date"),
    ("title", "Title"),
    ("bestsellers", "Bestsellers"),
    ("relevance", "Relevance"),
]


from . import currency as _currency, get_locale as _locale


def _error_html(message: str) -> str:
    safe = _html_escape(message)
    return (
        f'<div class="rounded-lg bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-700">'
        f'<strong>Error:</strong> {safe}</div>'
    )


def _run_pipeline(
    products: list,
    *,
    max_price: float | None,
    min_rating: float,
    min_hours: float,
    narrator: str,
    author: str,
    on_sale: bool,
    first_in_series_flag: bool,
    sort: str,
    skip_asins: set | None = None,
) -> tuple[list, dict, int, int, int]:
    """Filter, dedupe, collapse, sort. Returns (products, breakdown, total_before_limit, editions_removed, series_collapsed)."""
    filtered, breakdown = _filter_products(
        products,
        max_price=max_price,
        min_rating=min_rating,
        min_hours=min_hours,
        narrator=narrator,
        author=author,
        on_sale=on_sale,
        skip_asins=skip_asins,
    )
    filtered, editions_removed = _dedupe_editions(filtered)
    series_collapsed = 0
    if first_in_series_flag:
        filtered, series_collapsed = _first_in_series(filtered)
    filtered = _sort_local(filtered, sort)
    return filtered, breakdown, len(filtered), editions_removed, series_collapsed


def _get_form_params() -> dict:
    """Extract and coerce all filter params from request.args."""
    args = request.args

    def _float(key: str, default: float | None) -> float | None:
        val = args.get(key, "").strip()
        if not val:
            return default
        try:
            return float(val)
        except ValueError:
            return default

    def _int(key: str, default: int) -> int:
        val = args.get(key, "").strip()
        if not val:
            return default
        try:
            return int(val)
        except ValueError:
            return default

    return {
        "query": args.get("query", "").strip(),
        "genre": args.get("genre", "").strip(),
        "max_price": _float("max_price", None),
        "min_rating": _float("min_rating", 0.0) or 0.0,
        "min_hours": _float("min_hours", 0.0) or 0.0,
        "narrator": args.get("narrator", "").strip(),
        "author": args.get("author", "").strip(),
        "sort": args.get("sort", "price-per-hour").strip(),
        "pages": min(_int("pages", 10), 100),
        "on_sale": args.get("on_sale") in ("1", "true", "on"),
        "deep": args.get("deep") in ("1", "true", "on"),
        "first_in_series": args.get("first_in_series") in ("1", "true", "on"),
        "skip_owned": args.get("skip_owned") in ("1", "true", "on"),
    }


def _build_result_title(
    mode: str,
    currency: str,
    max_price: float | None,
    category_name: str,
    query: str,
) -> str:
    """Build the human-readable title for a search/find result set."""
    if mode == "find":
        parts = [f"Deals under {currency}{max_price:.2f}" if max_price is not None else "Deals"]
        if category_name:
            parts.append(f"in {category_name}")
        return " ".join(parts)
    # search mode
    if query:
        title = f'Search: "{query}"'
        if category_name:
            title += f" in {category_name}"
        return title
    return f"Search: {category_name}" if category_name else "Search Results"


def _cache_results(title: str, products: list) -> None:
    """Write serialized products to the last-results cache file."""
    cache_obj = {"title": title, "results": [_serialize_product(p) for p in products]}
    try:
        _atomic_write(LAST_RESULTS_FILE, json.dumps(cache_obj, ensure_ascii=False))
    except Exception:
        pass


def _render_table(
    products: list,
    breakdown: dict,
    currency: str,
    title: str,
    total_before_limit: int,
    editions_removed: int,
    series_collapsed: int,
    max_price: float | None,
) -> str:
    return render_template(
        "partials/_product_table.html",
        products=products,
        breakdown=breakdown,
        currency=currency,
        title=title,
        total_before_limit=total_before_limit,
        editions_removed=editions_removed,
        series_collapsed=series_collapsed,
        max_price=max_price,
    )


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    """Dashboard landing page."""
    last_results = []
    last_title = ""
    try:
        last_title, data = _load_last_results()
        last_results = [_deserialize_product(d) for d in data[:5]]
    except Exception:
        pass

    return render_template(
        "index.html",
        active_page="home",
        last_results=last_results,
        last_title=last_title,
        currency=_currency(),
    )


@bp.route("/search")
def search_page():
    """Search catalog page."""
    cfg = _load_config()
    profiles = _load_profiles()
    return render_template(
        "search.html",
        active_page="search",
        mode="search",
        sort_choices=_SORT_CHOICES,
        profiles=profiles,
        config=cfg,
        defaults={
            "max_price": cfg.get("max_price", ""),
            "sort": cfg.get("sort", "relevance"),
            "pages": cfg.get("pages", 3),
            "min_rating": cfg.get("min_rating", ""),
            "min_hours": cfg.get("min_hours", ""),
        },
        currency=_currency(),
    )


@bp.route("/find")
def find_page():
    """Find deals page."""
    cfg = _load_config()
    profiles = _load_profiles()
    return render_template(
        "search.html",
        active_page="find",
        mode="find",
        sort_choices=_SORT_CHOICES,
        profiles=profiles,
        config=cfg,
        defaults={
            "max_price": cfg.get("max_price", 5.00),
            "sort": cfg.get("sort", "price-per-hour"),
            "pages": cfg.get("pages", 10),
            "min_rating": cfg.get("min_rating", ""),
            "min_hours": cfg.get("min_hours", ""),
        },
        currency=_currency(),
    )


# ---------------------------------------------------------------------------
# HTMX result endpoints
# ---------------------------------------------------------------------------

def _fetch_and_render(mode: str) -> str:
    """Shared fetch-filter-render for /hx/search/results and /hx/find/results."""
    params = _get_form_params()
    locale = _locale()
    currency = _currency()

    try:
        dc = DealsClient(locale=locale)
        with dc:
            skip_asins: set | None = None
            if params["skip_owned"]:
                skip_asins = dc.get_library_asins()

            category_id = ""
            category_name = ""
            if params["genre"]:
                try:
                    category_id, category_name = dc.resolve_genre(params["genre"])
                except ValueError as e:
                    return _error_html(str(e))

            server_sort = SORT_OPTIONS.get(params["sort"], "BestSellers" if mode == "find" else "Relevance")
            sort_orders = DEEP_SORT_ORDERS if params["deep"] else [server_sort]

            all_products = []
            seen_asins: set[str] = set()

            for sort_order in sort_orders:
                for products, _page_num, _total in dc.search_pages(
                    keywords=params["query"] if mode == "search" else "",
                    category_id=category_id,
                    sort_by=sort_order,
                    max_pages=params["pages"],
                ):
                    new_products = [p for p in products if p.asin not in seen_asins]
                    seen_asins.update(p.asin for p in new_products)
                    all_products.extend(new_products)

        filtered, breakdown, total_before_limit, editions_removed, series_collapsed = _run_pipeline(
            all_products,
            max_price=params["max_price"],
            min_rating=params["min_rating"],
            min_hours=params["min_hours"],
            narrator=params["narrator"],
            author=params["author"],
            on_sale=params["on_sale"],
            first_in_series_flag=params["first_in_series"],
            sort=params["sort"],
            skip_asins=skip_asins,
        )
        _record_prices(filtered)

        result_title = _build_result_title(mode, currency, params["max_price"], category_name, params["query"])
        _cache_results(result_title, filtered)

        return _render_table(
            filtered, breakdown, currency, result_title,
            total_before_limit, editions_removed, series_collapsed, params["max_price"],
        )

    except Exception as exc:
        return _error_html(str(exc))


@bp.route("/hx/search/results")
def hx_search_results():
    return _fetch_and_render("search")


@bp.route("/hx/find/results")
def hx_find_results():
    return _fetch_and_render("find")


# ---------------------------------------------------------------------------
# SSE stream endpoints
# ---------------------------------------------------------------------------

def _stream_results(mode: str) -> Generator[str, None, None]:
    """Generator yielding SSE events for search progress, then final result."""
    params = _get_form_params()
    locale = _locale()
    currency = _currency()

    try:
        dc = DealsClient(locale=locale)
        with dc:
            skip_asins: set | None = None
            if params["skip_owned"]:
                skip_asins = dc.get_library_asins()

            category_id = ""
            category_name = ""
            if params["genre"]:
                try:
                    category_id, category_name = dc.resolve_genre(params["genre"])
                except ValueError as e:
                    yield f"event: error\ndata: {_error_html(str(e))}\n\n"
                    return

            server_sort = SORT_OPTIONS.get(params["sort"], "BestSellers" if mode == "find" else "Relevance")
            sort_orders = DEEP_SORT_ORDERS if params["deep"] else [server_sort]
            total_pages = params["pages"] * len(sort_orders)

            all_products = []
            seen_asins: set[str] = set()
            page_count = 0

            for sort_idx, sort_order in enumerate(sort_orders):
                for products, page_num, total in dc.search_pages(
                    keywords=params["query"] if mode == "search" else "",
                    category_id=category_id,
                    sort_by=sort_order,
                    max_pages=params["pages"],
                ):
                    new_products = [p for p in products if p.asin not in seen_asins]
                    seen_asins.update(p.asin for p in new_products)
                    all_products.extend(new_products)
                    page_count += 1

                    if page_num == 1 and total:
                        actual = min(params["pages"], math.ceil(total / 50))
                        remaining_sorts = len(sort_orders) - sort_idx - 1
                        total_pages = page_count + actual - 1 + remaining_sorts * params["pages"]

                    progress_html = render_template(
                        "partials/_progress.html",
                        page=page_count,
                        total_pages=total_pages,
                        items=len(all_products),
                    )
                    yield f"event: progress\ndata: {progress_html.replace(_NEWLINE, ' ')}\n\n"

        filtered, breakdown, total_before_limit, editions_removed, series_collapsed = _run_pipeline(
            all_products,
            max_price=params["max_price"],
            min_rating=params["min_rating"],
            min_hours=params["min_hours"],
            narrator=params["narrator"],
            author=params["author"],
            on_sale=params["on_sale"],
            first_in_series_flag=params["first_in_series"],
            sort=params["sort"],
            skip_asins=skip_asins,
        )
        _record_prices(filtered)

        result_title = _build_result_title(mode, currency, params["max_price"], category_name, params["query"])
        _cache_results(result_title, filtered)

        result_html = _render_table(
            filtered, breakdown, currency, result_title,
            total_before_limit, editions_removed, series_collapsed, params["max_price"],
        )
        yield f"event: done\ndata: {result_html.replace(_NEWLINE, ' ')}\n\n"

    except Exception as exc:
        err_html = _error_html(str(exc))
        yield f"event: error\ndata: {err_html.replace(_NEWLINE, ' ')}\n\n"


@bp.route("/search/stream")
def search_stream():
    return Response(
        stream_with_context(_stream_results("search")),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.route("/find/stream")
def find_stream():
    return Response(
        stream_with_context(_stream_results("find")),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Re-filter last results
# ---------------------------------------------------------------------------

@bp.route("/hx/last")
def hx_last():
    """Re-filter/re-sort the last cached results and return the product table."""
    params = _get_form_params()
    currency = _currency()

    try:
        cached_title, data = _load_last_results()
        products = [_deserialize_product(d) for d in data]
    except Exception as exc:
        return _error_html(str(exc))

    filtered, breakdown, total_before_limit, editions_removed, series_collapsed = _run_pipeline(
        products,
        max_price=params["max_price"],
        min_rating=params["min_rating"],
        min_hours=params["min_hours"],
        narrator=params["narrator"],
        author=params["author"],
        on_sale=params["on_sale"],
        first_in_series_flag=params["first_in_series"],
        sort=params["sort"] or "price",
    )

    return _render_table(
        filtered, breakdown, currency, cached_title,
        total_before_limit, editions_removed, series_collapsed, params["max_price"],
    )


# ---------------------------------------------------------------------------
# Export endpoint
# ---------------------------------------------------------------------------

@bp.route("/export")
def export():
    """Download last results as JSON or CSV."""
    fmt = request.args.get("format", "json").lower()
    params = _get_form_params()

    # The cache stores already-serialized dicts; re-deserialize only to run
    # the pipeline filters, then re-serialize for export.
    try:
        _cached_title, data = _load_last_results()
        products = [_deserialize_product(d) for d in data]
    except Exception as exc:
        return _error_html(str(exc)), 400

    filtered, _, _, _, _ = _run_pipeline(
        products,
        max_price=params["max_price"],
        min_rating=params["min_rating"],
        min_hours=params["min_hours"],
        narrator=params["narrator"],
        author=params["author"],
        on_sale=params["on_sale"],
        first_in_series_flag=params["first_in_series"],
        sort=params["sort"] or "price",
    )

    rows = [_serialize_product(p) for p in filtered]

    if fmt == "csv":
        output = io.StringIO()
        if rows:
            for row in rows:
                for key in ("authors", "narrators", "categories", "category_ids"):
                    if isinstance(row.get(key), list):
                        row[key] = "; ".join(str(v) for v in row[key])
            writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=audible-deals.csv"},
        )
    else:
        return Response(
            json.dumps(rows, indent=2, ensure_ascii=False),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=audible-deals.json"},
        )
