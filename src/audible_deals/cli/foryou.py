"""Personalized deal discovery from the user's library taste profile."""

from __future__ import annotations

import logging
import time
from inspect import cleandoc
from pathlib import Path

import click

from audible_deals import taste
from audible_deals.cli.helpers import (
    _credit_price,
    _currency,
    _get_client,
    _resolve_output_quiet,
)
from audible_deals.cli.pipeline import _record_and_emit
from audible_deals.client import DealsClient
from audible_deals.constants import DEFAULT_LIMIT, LOCALE_LANGUAGES
from audible_deals.display import console, create_scan_progress
from audible_deals.filtering import dedupe_editions, filter_products, sort_local
from audible_deals.price_history import (
    history_key,
    load_price_history,
    price_history_context,
)
from audible_deals.product import Product
from audible_deals.serialization import validate_export_path
from audible_deals.wishlist import load_wishlist, partition_wishlist

logger = logging.getLogger(__name__)

_AUTHOR_SCANS = 3
_GENRE_SCANS = 2
_PAGES_PER_SCAN = 2


def _scan_plan(profile: dict) -> tuple[list[str], list[dict], list[dict]]:
    """Authors, genres, and series the scan will cover, from the profile."""
    authors = [a["name"] for a in profile.get("authors", [])][:_AUTHOR_SCANS]
    genres = profile.get("genres", [])[:_GENRE_SCANS]
    series = [s for s in profile.get("series", []) if s.get("series_asin")]
    return authors, genres, series


def _print_for_me_plan(
    profile: dict, authors: list[str], genres: list[dict], series: list[dict]
) -> None:
    console.print("\n[bold]Dry run[/bold] — would scan, based on your library:")
    console.print(
        f"  Profile: {profile.get('library_size', 0)} books (built {profile.get('built_at', '?')})"
    )
    if series:
        console.print(f"  Series in progress: {', '.join(s['name'] for s in series)}")
    if authors:
        console.print(f"  Authors: {', '.join(authors)}")
    if genres:
        console.print(f"  Genres: {', '.join(g['name'] for g in genres)}")
    api_calls = len(series) + (len(authors) + len(genres)) * _PAGES_PER_SCAN
    console.print(f"  API calls: ~{api_calls}")


def _fetch_candidates(
    dc: DealsClient,
    authors: list[str],
    genres: list[dict],
    series: list[dict],
    owned: set[str],
) -> tuple[list[Product], dict[str, str]]:
    """Fetch series gaps, author works, and genre bestsellers, deduped vs owned."""
    candidates: dict[str, Product] = {}
    series_of: dict[str, str] = {}
    segments = len(series) + len(authors) + len(genres)

    def add(p: Product, series_name: str | None = None) -> None:
        if p.asin in owned or p.asin in candidates:
            return
        candidates[p.asin] = p
        if series_name:
            series_of[p.asin] = series_name

    with create_scan_progress() as progress:
        task = progress.add_task("Scanning your taste", total=segments, items=0)
        done = 0

        for s in series:
            for p in dc.get_series_products(s["series_asin"]):
                add(p, series_name=s["name"])
            done += 1
            progress.update(task, completed=done, items=len(candidates))
            time.sleep(0.3)  # rate limit between series lookups

        for author in authors:
            for page_products, _, _ in dc.search_pages(
                keywords=author, sort_by="Relevance", max_pages=_PAGES_PER_SCAN
            ):
                for p in page_products:
                    if any(author.lower() in a.lower() for a in p.authors):
                        add(p)
            done += 1
            progress.update(task, completed=done, items=len(candidates))

        for genre in genres:
            for page_products, _, _ in dc.search_pages(
                category_id=genre["id"],
                sort_by="BestSellers",
                max_pages=_PAGES_PER_SCAN,
            ):
                for p in page_products:
                    add(p)
            done += 1
            progress.update(task, completed=done, items=len(candidates))

    return list(candidates.values()), series_of


@click.command("for-me")
@click.option(
    "--refresh",
    is_flag=True,
    default=False,
    help="Rebuild the taste profile (refetches your library)",
)
@click.option(
    "--max-price", type=click.FloatRange(min=0), default=None, help="Max price filter"
)
@click.option(
    "--max-effective-price",
    "max_effective_price",
    type=click.FloatRange(min=0),
    default=None,
    help="Max effective price — the cheaper of cash price and one credit",
)
@click.option("--min-rating", type=float, default=0.0, help="Minimum rating")
@click.option("--min-ratings", type=int, default=0, help="Minimum number of ratings")
@click.option("--min-hours", type=float, default=0.0, help="Minimum length in hours")
@click.option(
    "--on-sale/--no-on-sale", default=False, help="Only show discounted items"
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(min=0),
    default=DEFAULT_LIMIT,
    help="Show only the top N results (0 for unlimited, default: 25)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Export results to file (.json or .csv)",
)
@click.option(
    "--json",
    "json_flag",
    is_flag=True,
    default=False,
    help="Output results as JSON to stdout",
)
@click.option(
    "--quiet", "-q", is_flag=True, default=False, help="Suppress table output"
)
@click.option(
    "--show-url", is_flag=True, default=False, help="Show Audible URL for each item"
)
@click.option(
    "--interactive/--no-interactive",
    "-i",
    default=False,
    help="Browse results interactively",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be scanned without making API calls",
)
@click.option(
    "--sort",
    type=click.Choice(
        [
            "price",
            "-price",
            "discount",
            "price-per-hour",
            "value",
            "rating",
            "length",
            "date",
            "title",
            "author",
            "asin",
            "bestsellers",
        ]
    ),
    default=None,
    help="Re-sort results after fit ranking (default: fit rank order)",
)
@click.option(
    "--narrator",
    default="",
    help="Filter by narrator name (substring match, client-side)",
)
@click.option(
    "--exclude-author",
    "exclude_authors",
    multiple=True,
    help="Exclude author (substring match, repeatable)",
)
@click.option(
    "--exclude-narrator",
    "exclude_narrators",
    multiple=True,
    help="Exclude narrator (substring match, repeatable)",
)
@click.option(
    "--skip-plus/--no-skip-plus",
    default=False,
    help="Exclude Audible Plus catalog titles",
)
@click.option(
    "--only-plus/--no-only-plus",
    default=False,
    help="Show only Audible Plus catalog titles",
)
@click.pass_context
def for_me(
    ctx,
    refresh,
    max_price,
    max_effective_price,
    min_rating,
    min_ratings,
    min_hours,
    on_sale,
    limit,
    output,
    json_flag,
    quiet,
    show_url,
    interactive,
    dry_run,
    sort,
    narrator,
    exclude_authors,
    exclude_narrators,
    skip_plus,
    only_plus,
):
    """Personalized deals from your own library's taste profile.

    Builds a local profile from the books you own (top authors, narrators,
    genres, and series in progress — cached for 24h), scans the catalog from
    those angles, and ranks results by how well they match. The Match column
    says why each book is there. Owned books are always excluded.

    \b
    Examples:
        deals for-me
        deals for-me --max-price 5 --on-sale
        deals for-me --refresh          # rebuild the profile from your library
        deals for-me --dry-run          # show the scan plan
    """
    logger.info(
        "for-me refresh=%s max_price=%s dry_run=%s", refresh, max_price, dry_run
    )
    if skip_plus and only_plus:
        raise click.UsageError("--skip-plus and --only-plus are mutually exclusive")
    validate_export_path(output)
    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)
    dc = _get_client(ctx.obj["locale"])

    profile = None if refresh else taste.load_cached_profile()
    if dry_run:
        if profile is None:
            raise click.ClickException(
                "No cached taste profile — run `deals for-me` (without --dry-run) "
                "to build it first."
            )
        authors, genres, series = _scan_plan(profile)
        if not (authors or genres or series):
            raise click.ClickException(
                "Could not derive a taste profile from your library."
            )
        _print_for_me_plan(profile, authors, genres, series)
        return

    with dc:
        if profile is None:
            from audible_deals.cli.library import fetch_library_with_progress

            lib_products = fetch_library_with_progress(dc)
            if not lib_products:
                raise click.ClickException(
                    "Your library is empty — for-me learns your taste from books you own."
                )
            profile = taste.build_profile(lib_products)
            taste.save_profile(profile)
            console.print(
                f"[dim]Taste profile built from {len(lib_products)} books "
                "(cached for 24h; --refresh to rebuild).[/dim]"
            )

        authors, genres, series = _scan_plan(profile)
        if not (authors or genres or series):
            raise click.ClickException(
                "Could not derive a taste profile from your library."
            )

        owned = set(profile.get("owned_asins", []))
        candidates, series_of = _fetch_candidates(dc, authors, genres, series, owned)

    credit_price = _credit_price(ctx)
    language = LOCALE_LANGUAGES.get(ctx.obj["locale"], "")
    filtered, filter_breakdown = filter_products(
        candidates,
        drop_zero_length=True,
        max_price=max_price,
        max_effective_price=max_effective_price,
        credit_price=credit_price,
        min_rating=min_rating,
        min_ratings=min_ratings,
        min_hours=min_hours,
        on_sale=on_sale,
        language=language,
        narrator=narrator,
        exclude_authors=exclude_authors,
        exclude_narrators=exclude_narrators,
        skip_plus=skip_plus,
        only_plus=only_plus,
    )
    filtered, editions_removed = dedupe_editions(filtered)
    histories = {
        history_key(p.asin, p.locale): load_price_history(p.asin, p.locale)
        for p in filtered
        if p.price is not None
    }
    atl_asins, hist_context = price_history_context(filtered, histories=histories)
    ranked, match_context = taste.rank_by_fit(
        filtered,
        profile,
        series_of,
        atl_asins=atl_asins,
        hist_context=hist_context,
    )
    if sort:
        ranked = sort_local(ranked, sort)

    wishlist_asins = {i["asin"] for i in partition_wishlist(load_wishlist())[0]}
    for asin in match_context:
        if asin in wishlist_asins:
            match_context[asin] += " · wishlisted"

    _record_and_emit(
        ranked,
        filter_breakdown,
        editions_removed,
        0,
        title="For you",
        limit=limit,
        output=output,
        json_flag=json_flag,
        quiet=quiet,
        max_price=max_price,
        currency=_currency(ctx),
        interactive=interactive,
        show_url=show_url,
        credit_price=credit_price,
        match_context=match_context,
        histories=histories,
        atl_asins=atl_asins,
        hist_context=hist_context,
    )


@click.pass_context
def _deprecated_for_you(ctx, **kwargs):
    click.echo("Warning: `deals for-you` is deprecated; use `deals for-me`.", err=True)
    return for_me.callback(**kwargs)


for_you = click.Command(
    name="for-you",
    callback=_deprecated_for_you,
    params=for_me.params,
    help=(
        "Deprecated: use `deals for-me` instead. This alias will be removed in "
        "a future release.\n\n"
        f"{cleandoc(for_me.help or '')}"
    ),
    short_help="Deprecated alias for `deals for-me`.",
    hidden=True,
)
