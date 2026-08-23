"""Personalized deal discovery from the user's library taste profile."""

from __future__ import annotations

import dataclasses
import json
import logging
import shlex
from decimal import Decimal
from inspect import cleandoc
from pathlib import Path

import click

from audible_deals import taste
from audible_deals.cli.helpers import (
    _CL,
    _credit_price,
    _currency,
    _get_client,
    _report_partial_series_outcomes,
    _resolve_output_quiet,
)
from audible_deals.validation import NONNEGATIVE_FLOAT, NONNEGATIVE_INT, RATING_FLOAT
from audible_deals.client import CatalogSearchRequest, DealsClient
from audible_deals.constants import DEFAULT_LIMIT, LOCALE_LANGUAGES
from audible_deals.price_history import (
    history_key,
    load_price_history,
    price_history_context,
)
from audible_deals.product import Product
from audible_deals.presentation.terminal import (
    console,
    create_scan_progress,
    safe_markup,
)
from audible_deals.result_models import FilterContext
from audible_deals.results_cache import load_dismissed_asins
from audible_deals.result_processing import (
    DiscoveryProcessingRequest,
    process_discovery,
    result_recipe,
)
from audible_deals.result_publication import (
    ResultPublicationRequest,
    ResultSessionSpec,
    publish_discovery,
)
from audible_deals.serialization import validate_export_path
from audible_deals.series_identity import (
    normalize_identity_text,
    parse_numeric_series_position,
    series_book_identity,
    series_book_identity_from_parts,
    series_identity,
)
from audible_deals.settings import SettingsResolutionRequest, resolve_settings
from audible_deals.wishlist import load_wishlist, partition_wishlist

logger = logging.getLogger(__name__)

_AUTHOR_SCANS = 3
_GENRE_SCANS = 2
_PAGES_PER_SCAN = 2
_PLACEHOLDER_TITLE = "Series Advisor Placeholder"


def _scan_plan(profile: dict) -> tuple[list[str], list[dict], list[dict]]:
    """Authors, genres, and series the scan will cover, from the profile."""
    authors = [
        author["name"]
        for author in profile.get("authors", [])
        if not taste.is_great_courses_author(author["name"])
    ][:_AUTHOR_SCANS]
    genres = profile.get("genres", [])[:_GENRE_SCANS]
    series = [s for s in profile.get("series", []) if s.get("series_asin")]
    return authors, genres, series


def _for_me_plan(
    profile: dict, authors: list[str], genres: list[dict], series: list[dict]
) -> dict:
    catalog_calls = (len(authors) + len(genres)) * _PAGES_PER_SCAN
    relationship_calls = len(series)
    return {
        "dry_run": True,
        "profile": {
            "library_size": profile.get("library_size", 0),
            "built_at": profile.get("built_at", "?"),
        },
        "series": [item["name"] for item in series],
        "authors": list(authors),
        "genres": [item["name"] for item in genres],
        "known_api_calls": catalog_calls + relationship_calls,
        "series_product_batches": "additional when a relationship has children",
    }


def _print_for_me_plan(plan: dict) -> None:
    console.print("\n[bold]Dry run[/bold] — would scan, based on your library:")
    profile = plan["profile"]
    console.print(
        f"  Profile: {profile.get('library_size', 0)} books "
        f"(built {safe_markup(profile.get('built_at', '?'))})"
    )
    series = plan["series"]
    authors = plan["authors"]
    genres = plan["genres"]
    if series:
        console.print(f"  Series in progress: {safe_markup(', '.join(series))}")
    if authors:
        console.print(f"  Authors: {safe_markup(', '.join(authors))}")
    if genres:
        console.print(f"  Genres: {safe_markup(', '.join(genres))}")
    console.print(
        f"  Known API calls: {plan['known_api_calls']} + series product batches"
    )


def _resolve_for_me_settings(ctx: click.Context, **flags):
    explicit_options = {key for key in flags if ctx.get_parameter_source(key) == _CL}
    try:
        settings = resolve_settings(
            SettingsResolutionRequest(
                config=ctx.obj.get("config", {}),
                profile=None,
                cli_flags=flags,
                explicit_options=explicit_options,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    if not settings.language and not settings.all_languages:
        settings = dataclasses.replace(
            settings, language=LOCALE_LANGUAGES.get(ctx.obj["locale"], "")
        )
    return settings


def _fetch_candidates(
    dc: DealsClient,
    authors: list[str],
    genres: list[dict],
    series: list[dict],
    owned: set[str],
    *,
    show_progress: bool = True,
) -> tuple[
    list[Product],
    dict[str, taste.SeriesMatch],
    tuple[str, ...],
    tuple[str, ...],
]:
    """Fetch series gaps, author works, and genre bestsellers, deduped vs owned."""
    profile_series: dict[str, dict] = {}
    series_aliases: dict[str, str] = {}
    owned_books: dict[str, set[str]] = {}
    for item in series:
        identity = normalize_identity_text(
            item.get("series_asin", "")
        ) or normalize_identity_text(item.get("name", ""))
        profile_series[identity] = item
        for alias in (item.get("series_asin", ""), item.get("name", "")):
            normalized_alias = normalize_identity_text(alias)
            if normalized_alias:
                series_aliases[normalized_alias] = identity
        owned_books[identity] = {
            series_book_identity_from_parts(
                book.get("title", ""), book.get("position", "")
            )
            for book in item.get("books", [])
        }

    candidates_by_edition: dict[tuple[str, str], Product] = {}
    targets_by_edition: dict[tuple[str, str], list[str]] = {}
    failures: list[str] = []
    incomplete: list[str] = []
    segments = len(series) + len(authors) + len(genres)

    def add(p: Product, target_identity: str | None = None) -> None:
        if p.asin in owned or p.price is None or p.title == _PLACEHOLDER_TITLE:
            return
        product_identity = series_identity(p)
        inferred_target = series_aliases.get(product_identity or "")
        target = target_identity or inferred_target
        if target and series_book_identity(p) in owned_books.get(target, set()):
            return
        edition_scope = target or product_identity
        edition_key = (
            (edition_scope, series_book_identity(p)) if edition_scope else ("", p.asin)
        )
        existing = candidates_by_edition.get(edition_key)
        if existing is None or (
            p.price is not None and (existing.price is None or p.price < existing.price)
        ):
            candidates_by_edition[edition_key] = p
        if target:
            targets = targets_by_edition.setdefault(edition_key, [])
            if target not in targets:
                targets.append(target)

    with create_scan_progress(disable=not show_progress) as progress:
        task = progress.add_task("Scanning your taste", total=segments, items=0)
        done = 0

        series_batch = dc.get_series_products_many(
            [item["series_asin"] for item in series]
        )
        for s in series:
            target_identity = normalize_identity_text(
                s.get("series_asin", "")
            ) or normalize_identity_text(s.get("name", ""))
            try:
                failure = series_batch.failures.get(s["series_asin"])
                if failure is not None:
                    raise failure
                for p in series_batch.products.get(s["series_asin"], ()):
                    add(p, target_identity=target_identity)
                product_failures = series_batch.product_failures.get(
                    s["series_asin"], ()
                )
                missing = series_batch.missing_asins.get(s["series_asin"], ())
                issues = []
                if product_failures:
                    issues.append(
                        f"{len(product_failures)} product batch request(s) failed"
                    )
                if missing:
                    issues.append(f"{len(missing)} product(s) unavailable")
                if issues:
                    detail = f"{s['name']}: {', '.join(issues)}"
                    logger.warning("incomplete for-me series scan: %s", detail)
                    incomplete.append(detail)
            except Exception as exc:
                logger.warning(
                    "for-me series scan failed for %s", s["name"], exc_info=True
                )
                failures.append(f"{s['name']}: {type(exc).__name__}: {exc}")
            done += 1
            progress.update(task, completed=done, items=len(candidates_by_edition))

        catalog_requests = [
            CatalogSearchRequest(
                keywords=author,
                sort_by="Relevance",
                max_pages=_PAGES_PER_SCAN,
            )
            for author in authors
        ]
        catalog_requests.extend(
            CatalogSearchRequest(
                category_id=genre["id"],
                sort_by="BestSellers",
                max_pages=_PAGES_PER_SCAN,
            )
            for genre in genres
        )
        catalog_results = dc.search_segments(catalog_requests)

        for author, segment in zip(authors, catalog_results[: len(authors)]):
            for page_products, _, _ in segment.pages:
                for p in page_products:
                    if any(author.lower() in a.lower() for a in p.authors):
                        add(p)
            done += 1
            progress.update(task, completed=done, items=len(candidates_by_edition))

        for segment in catalog_results[len(authors) :]:
            for page_products, _, _ in segment.pages:
                for p in page_products:
                    add(p)
            done += 1
            progress.update(task, completed=done, items=len(candidates_by_edition))

    candidates: dict[str, Product] = {}
    targets_by_asin: dict[str, list[str]] = {}
    for edition_key, product in candidates_by_edition.items():
        existing = candidates.get(product.asin)
        if existing is None or (
            product.price is not None
            and (existing.price is None or product.price < existing.price)
        ):
            candidates[product.asin] = product
        targets = targets_by_asin.setdefault(product.asin, [])
        for target in targets_by_edition.get(edition_key, []):
            if target not in targets:
                targets.append(target)

    lowest_positions: dict[str, Decimal] = {}
    for asin, targets in targets_by_asin.items():
        if not targets:
            continue
        position = parse_numeric_series_position(candidates[asin].series_position)
        if position is None:
            continue
        for target in targets:
            current = lowest_positions.get(target)
            if current is None or position < current:
                lowest_positions[target] = position

    series_matches: dict[str, taste.SeriesMatch] = {}
    for asin, targets in targets_by_asin.items():
        if not targets:
            continue
        position = parse_numeric_series_position(candidates[asin].series_position)
        next_target = next(
            (
                target
                for target in targets
                if position is not None and position == lowest_positions.get(target)
            ),
            None,
        )
        target = next_target or targets[0]
        series_matches[asin] = (
            profile_series[target]["name"],
            next_target is not None,
        )

    return list(candidates.values()), series_matches, tuple(failures), tuple(incomplete)


@click.command("for-me")
@click.option(
    "--refresh",
    is_flag=True,
    default=False,
    help="Rebuild the taste profile (refetches your library)",
)
@click.option(
    "--max-price", type=NONNEGATIVE_FLOAT, default=None, help="Max price filter"
)
@click.option(
    "--max-effective-price",
    "max_effective_price",
    type=NONNEGATIVE_FLOAT,
    default=None,
    help="Max effective price — the cheaper of cash price and one credit",
)
@click.option("--min-rating", type=RATING_FLOAT, default=0.0, help="Minimum rating")
@click.option(
    "--min-ratings", type=NONNEGATIVE_INT, default=0, help="Minimum number of ratings"
)
@click.option(
    "--min-hours", type=NONNEGATIVE_FLOAT, default=0.0, help="Minimum length in hours"
)
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
    if dry_run and output:
        raise click.UsageError("--dry-run cannot be combined with --output/-o")
    validate_export_path(output)
    quiet = _resolve_output_quiet(ctx, output, json_flag, quiet)
    settings = _resolve_for_me_settings(
        ctx,
        max_price=max_price,
        min_rating=min_rating,
        min_ratings=min_ratings,
        min_hours=min_hours,
        on_sale=on_sale,
        limit=limit,
        interactive=interactive,
        sort=sort or "",
        narrator=narrator,
        skip_plus=skip_plus,
        only_plus=only_plus,
        language="",
        all_languages=False,
    )
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
        plan = _for_me_plan(profile, authors, genres, series)
        if json_flag:
            click.echo(json.dumps(plan, indent=2, ensure_ascii=False, allow_nan=False))
        else:
            _print_for_me_plan(plan)
        return

    max_price = settings.max_price
    min_rating = settings.min_rating
    min_ratings = settings.min_ratings
    min_hours = settings.min_hours
    on_sale = settings.on_sale
    limit = settings.limit
    interactive = settings.interactive
    sort = settings.sort
    narrator = settings.narrator
    skip_plus = settings.skip_plus
    only_plus = settings.only_plus
    dismissed_asins = load_dismissed_asins()

    with dc:
        if profile is None:
            from audible_deals.cli.library import fetch_library_with_progress

            lib_products = fetch_library_with_progress(dc, show_progress=not json_flag)
            if not lib_products:
                raise click.ClickException(
                    "Your library is empty — for-me learns your taste from books you own."
                )
            profile = taste.build_profile(lib_products)
            taste.save_profile(profile)
            profile_message = (
                f"Taste profile built from {len(lib_products)} books "
                "(cached for 24h; --refresh to rebuild)."
            )
            if json_flag:
                click.echo(profile_message, err=True)
            else:
                console.print(f"[dim]{profile_message}[/dim]")

        authors, genres, series = _scan_plan(profile)
        if not (authors or genres or series):
            raise click.ClickException(
                "Could not derive a taste profile from your library."
            )

        owned = set(profile.get("owned_asins", []))
        candidates, series_matches, failures, incomplete = _fetch_candidates(
            dc,
            authors,
            genres,
            series,
            owned,
            show_progress=not json_flag,
        )

    _report_partial_series_outcomes(
        failures,
        incomplete,
        len(series),
        json_flag=json_flag,
    )

    credit_price = _credit_price(ctx)
    language = "" if settings.all_languages else settings.language
    candidate_histories = {
        history_key(p.asin, p.locale): load_price_history(p.asin, p.locale)
        for p in candidates
        if p.price is not None
    }
    candidate_atl, candidate_hist_context = price_history_context(
        candidates, histories=candidate_histories
    )
    ranked_candidates, all_match_context, fit_scores = taste.rank_by_fit(
        candidates,
        profile,
        series_matches,
        atl_asins=candidate_atl,
        hist_context=candidate_hist_context,
    )
    allowed_asin_order = [product.asin for product in ranked_candidates]
    allowed_asins = set(allowed_asin_order)
    ranked_candidates.extend(
        product for product in candidates if product.asin not in allowed_asins
    )

    result = process_discovery(
        DiscoveryProcessingRequest(
            products=tuple(ranked_candidates),
            context=FilterContext(
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
                skip_asins=dismissed_asins,
                skip_plus=skip_plus,
                only_plus=only_plus,
                sort=sort or "",
            ),
        ),
    )
    result = dataclasses.replace(
        result,
        products=tuple(
            product for product in result.products if product.asin in allowed_asins
        ),
    )
    atl_asins, hist_context = price_history_context(
        list(result.products), histories=candidate_histories
    )

    wishlist_asins = {i["asin"] for i in partition_wishlist(load_wishlist())[0]}
    for asin in all_match_context:
        if asin in wishlist_asins:
            all_match_context[asin] += " · wishlisted"
    match_context = {
        product.asin: all_match_context.get(product.asin, "")
        for product in result.products
    }
    result = dataclasses.replace(
        result,
        histories=candidate_histories,
        match_reasons=match_context,
        atl_asins=atl_asins,
        hist_context=hist_context,
    )
    publish_discovery(
        ResultPublicationRequest(
            result=result,
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
            candidates=tuple(ranked_candidates),
            session_spec=ResultSessionSpec(
                producer="for-me",
                locale=ctx.obj["locale"],
                recipe=result_recipe(
                    max_price=max_price,
                    max_effective_price=max_effective_price,
                    min_rating=min_rating,
                    min_ratings=min_ratings,
                    min_hours=min_hours,
                    language=language,
                    narrator=narrator,
                    exclude_authors=exclude_authors,
                    exclude_narrators=exclude_narrators,
                    on_sale=on_sale,
                    limit=limit,
                    sort=sort or "",
                    skip_plus=skip_plus,
                    only_plus=only_plus,
                ),
                source={
                    "command": shlex.join(
                        [
                            "deals",
                            "for-me",
                            *(["--refresh"] if refresh else []),
                        ]
                    ),
                    "refresh": refresh,
                    "taste_sources": {
                        "authors": authors,
                        "genres": [genre.get("id", "") for genre in genres],
                        "series": [item.get("series_asin", "") for item in series],
                    },
                },
                constraints={
                    "drop_zero_length": True,
                    "always_skip_asins": sorted(dismissed_asins),
                },
                ranking_context={
                    "allowed_asins": allowed_asin_order,
                    "fit_scores": fit_scores,
                    "match_reasons": {
                        product.asin: all_match_context.get(product.asin, "")
                        for product in candidates
                    },
                },
            ),
            json_writer=click.echo,
        )
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
