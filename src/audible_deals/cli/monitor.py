"""Saved catalog searches that report changes on scheduled tracking runs."""

from __future__ import annotations

import dataclasses
import datetime
import logging
from typing import Any

import click

from audible_deals import constants
from audible_deals.cli.catalog import _fetch_multi_query, monitor_scan_plan
from audible_deals.cli.helpers import (
    _CL,
    _get_client,
    _resolve_categories,
    _resolve_skip_asins,
)
from audible_deals.cli.options import _complete_profile_names
from audible_deals.cli.pipeline import _apply_settings_filters, _fetch_with_progress
from audible_deals.config_store import (
    load_monitor_state,
    load_monitors,
    load_profiles,
    save_monitor_state,
    save_monitors,
)
from audible_deals.constants import (
    ALL_SORT_OPTIONS,
    LOCALE_LANGUAGES,
    WEBHOOK_FORMATS,
    LockHeldError,
    run_lock,
)
from audible_deals.display import console
from audible_deals.serialization import serialize_product
from audible_deals.settings import Settings
from audible_deals.validation import validate_webhook_url

logger = logging.getLogger(__name__)

MAX_MONITOR_API_CALLS_PER_RUN = 60

_FIND_DEFAULTS = {
    "max_price": 5.0,
    "sort": "price-per-hour",
    "pages": 10,
    "min_ratings": 1,
}
_SEARCH_DEFAULTS = {
    "max_price": None,
    "sort": "relevance",
    "pages": 3,
    "min_ratings": 0,
}
_DIRECT_SETTING_FIELDS = (
    "max_price",
    "sort",
    "pages",
    "min_rating",
    "min_ratings",
    "min_hours",
    "min_discount",
    "genre",
    "narrator",
    "author",
    "on_sale",
    "deep",
    "language",
    "skip_owned",
    "first_in_series",
    "skip_plus",
    "only_plus",
    "exclude_authors",
    "exclude_narrators",
    "exclude_keywords",
)


def _complete_monitor_names(ctx, param, incomplete):
    from click.shell_completion import CompletionItem

    return [
        CompletionItem(name) for name in load_monitors() if name.startswith(incomplete)
    ]


def _settings_dict(settings: Settings) -> dict[str, Any]:
    return dataclasses.asdict(settings)


def _settings_from_dict(data: dict) -> Settings:
    fields = {f.name for f in dataclasses.fields(Settings)}
    return Settings(**{key: value for key, value in data.items() if key in fields})


def _monitor_settings(
    ctx: click.Context, mode: str, profile_name: str | None, flags: dict[str, Any]
) -> Settings:
    """Resolve the frozen monitor settings with the command's own defaults."""
    profile = load_profiles().get(profile_name or "") if profile_name else None
    if profile_name and profile is None:
        raise click.ClickException(f"Profile '{profile_name}' not found.")
    values: dict[str, Any] = dict(
        _FIND_DEFAULTS if mode == "find" else _SEARCH_DEFAULTS
    )
    values.update(ctx.obj.get("config", {}))
    if profile:
        values.update(profile)
    for key in _DIRECT_SETTING_FIELDS:
        if ctx.get_parameter_source(key) == _CL:
            values[key] = flags[key]
    # Snapshots must be complete. A display limit would manufacture false
    # disappearance/re-entry events as titles move in and out of the top N.
    values["limit"] = None
    allowed = {field.name for field in dataclasses.fields(Settings)}
    settings = Settings(
        **{key: value for key, value in values.items() if key in allowed}
    )
    if settings.skip_plus and settings.only_plus:
        raise click.UsageError("--skip-plus and --only-plus are mutually exclusive")
    if not settings.language and not settings.all_languages:
        settings = dataclasses.replace(
            settings, language=LOCALE_LANGUAGES[ctx.obj["locale"]]
        )
    return settings


def estimate_monitor_calls(definition: dict) -> int:
    """Estimated catalog-page requests; category lookup is excluded from the budget."""
    settings = _settings_from_dict(definition["settings"])
    queries, sorts = monitor_scan_plan(
        settings, definition["mode"], definition.get("query", "")
    )
    broad_calls = len(queries) * len(sorts) * settings.pages
    title_probes = len(queries) if definition["mode"] == "search" else 0
    return broad_calls + title_probes


def _monitor_slot(state: dict, name: str) -> dict:
    """Return a validated state slot, repairing only this monitor when needed."""
    monitors = state.setdefault("monitors", {})
    raw = monitors.get(name)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning(
            "monitor state for %s is malformed; resetting its baseline", name
        )
        return {}
    products = raw.get("products", {})
    if not isinstance(products, dict):
        logger.warning(
            "monitor state products for %s is malformed; resetting its baseline", name
        )
        return {}
    valid_products = {
        asin: product
        for asin, product in products.items()
        if isinstance(asin, str) and isinstance(product, dict)
    }
    if len(valid_products) != len(products):
        logger.warning("monitor state products for %s contains malformed entries", name)
    return {
        "initialized": bool(raw.get("initialized")),
        "products": valid_products,
        "last_success": raw.get("last_success")
        if isinstance(raw.get("last_success"), str)
        else None,
        "last_error": raw.get("last_error")
        if isinstance(raw.get("last_error"), str)
        else None,
    }


def _save_slot(state: dict, name: str, slot: dict) -> None:
    state.setdefault("monitors", {})[name] = slot


def record_monitor_error(name: str, error: Exception) -> None:
    """Persist a monitor failure while retaining the last known-good snapshot."""
    state = load_monitor_state()
    slot = _monitor_slot(state, name)
    slot["last_error"] = f"{type(error).__name__}: {error}"
    _save_slot(state, name, slot)
    save_monitor_state(state)


def scan_monitor(definition: dict) -> list[dict]:
    """Fetch a monitor without changing result caches, seen state, or history."""
    locale = definition["locale"]
    settings = _settings_from_dict(definition["settings"])
    mode = definition["mode"]
    queries, sort_orders = monitor_scan_plan(
        settings, mode, definition.get("query", "")
    )
    dc = _get_client(locale)
    with dc:
        category, _category_name, excluded = _resolve_categories(
            dc, settings.genre, "", settings.exclude_genre
        )
        skip_asins = _resolve_skip_asins(dc, settings.skip_owned, False)
        if mode == "search":
            products = _fetch_multi_query(
                dc,
                queries,
                category=category,
                sort_orders=sort_orders,
                pages=settings.pages,
            )
        else:
            products = _fetch_with_progress(
                dc,
                keywords=queries[0],
                category_ids=[category],
                sort_orders=sort_orders,
                pages=settings.pages,
                description=f"Checking monitor {definition['name']}",
            )
    filtered, _, _, _, _ = _apply_settings_filters(
        products, settings, skip_asins=skip_asins, exclude_category_ids=excluded
    )
    # A price-less catalog entry cannot produce a usable deal alert and breaks
    # the price formatters. It is intentionally outside monitor snapshots.
    return [
        serialize_product(product) for product in filtered if product.price is not None
    ]


def _events(previous: dict, current: list[dict], name: str) -> list[dict]:
    events: list[dict] = []
    for product in current:
        old = previous.get(product["asin"])
        price = product.get("price")
        if price is None:
            continue
        if old is None:
            kind = "new"
        elif (
            old.get("price") is not None and float(old["price"]) - float(price) >= 0.01
        ):
            kind = "price_drop"
        else:
            continue
        events.append(
            {
                "event": kind,
                "monitor": name,
                "asin": product["asin"],
                "title": product.get("full_title") or product.get("title", ""),
                "price": price,
                "target": price,
                "url": product.get("url", ""),
                "previous_price": old.get("price") if old else None,
            }
        )
    return events


def run_monitor(definition: dict, *, deliver=None) -> tuple[list[dict], bool]:
    """Run one monitor. Delivery precedes persistence so failed posts retry."""
    state = load_monitor_state()
    slot = _monitor_slot(state, definition["name"])
    current = [
        product
        for product in scan_monitor(definition)
        if product.get("price") is not None
    ]
    initialized = bool(slot.get("initialized"))
    events = (
        _events(slot.get("products", {}), current, definition["name"])
        if initialized
        else []
    )
    if events and deliver:
        deliver(events, definition)
    _save_slot(
        state,
        definition["name"],
        {
            "initialized": True,
            "products": {product["asin"]: product for product in current},
            "last_success": datetime.datetime.now().isoformat(timespec="seconds"),
            "last_error": None,
        },
    )
    save_monitor_state(state)
    return events, not initialized


def select_monitors_for_run(
    monitors: dict[str, dict], track_state: dict
) -> list[tuple[str, dict]]:
    """Round-robin enabled monitors under a deterministic catalog-call budget."""
    enabled = [
        (name, definition)
        for name, definition in sorted(monitors.items())
        if isinstance(definition, dict) and definition.get("enabled", True)
    ]
    if not enabled:
        return []
    start = int(track_state.get("monitor_cursor", 0)) % len(enabled)
    ordered = enabled[start:] + enabled[:start]
    selected: list[tuple[str, dict]] = []
    used = 0
    for name, definition in ordered:
        try:
            estimate = estimate_monitor_calls(definition)
        except (KeyError, TypeError, click.ClickException) as exc:
            logger.warning("Skipping malformed monitor %s: %s", name, exc)
            continue
        if estimate > MAX_MONITOR_API_CALLS_PER_RUN:
            logger.warning(
                "Skipping monitor %s: estimate %s exceeds per-run budget",
                name,
                estimate,
            )
            continue
        if used + estimate > MAX_MONITOR_API_CALLS_PER_RUN:
            if selected:
                break
            continue
        selected.append((name, definition))
        used += estimate
    if selected:
        next_name = selected[-1][0]
        track_state["monitor_cursor"] = (
            next(index for index, item in enumerate(enabled) if item[0] == next_name)
            + 1
        ) % len(enabled)
    return selected


def _print_monitor(name: str, definition: dict, state: dict) -> None:
    slot = _monitor_slot(state, name)
    status = "enabled" if definition.get("enabled", True) else "paused"
    query = (
        definition.get("query")
        if definition.get("mode") == "search"
        else definition["settings"].get("keywords", "")
    )
    source = (
        f"profile {definition['profile']}"
        if definition.get("profile")
        else "direct options"
    )
    console.print(f"[bold]{name}[/bold]  {status}")
    console.print(
        f"  Mode: {definition.get('mode')}  Locale: {definition.get('locale')}  Source: {source}"
    )
    if query:
        console.print(f"  Query: {query}")
    console.print("  Frozen settings:")
    for key, value in sorted(definition.get("settings", {}).items()):
        if key == "limit":
            rendered = "unlimited"
        elif value in (None, "", False, (), []):
            continue
        elif isinstance(value, (tuple, list)):
            rendered = ", ".join(str(item) for item in value)
        else:
            rendered = str(value)
        console.print(f"    {key.replace('_', '-')}: {rendered}")
    console.print(
        f"  Estimated catalog calls/run: {_estimate_label(definition)} (shared budget {MAX_MONITOR_API_CALLS_PER_RUN})"
    )
    console.print(
        f"  Snapshot: {len(slot.get('products', {}))} item(s)  Last success: {slot.get('last_success') or 'never'}"
    )
    if slot.get("last_error"):
        console.print(f"  [red]Last error: {slot['last_error']}[/red]")


def _estimate_label(definition: dict) -> str:
    try:
        return str(estimate_monitor_calls(definition))
    except (KeyError, TypeError, click.ClickException):
        return "invalid"


@click.group(invoke_without_command=True)
@click.pass_context
def monitor(ctx):
    """Manage saved-search monitors run by deals track run."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(list_monitors)


def _monitor_filter_options(func):
    options = [
        click.option("--max-price", type=click.FloatRange(min=0), default=None),
        click.option(
            "--sort", type=click.Choice(sorted(ALL_SORT_OPTIONS)), default=None
        ),
        click.option("--pages", type=click.IntRange(min=1), default=None),
        click.option("--min-rating", type=click.FloatRange(min=0), default=None),
        click.option("--min-ratings", type=click.IntRange(min=0), default=None),
        click.option("--min-hours", type=click.FloatRange(min=0), default=None),
        click.option(
            "--min-discount", type=click.IntRange(min=0, max=100), default=None
        ),
        click.option("--genre", default=None),
        click.option("--narrator", default=None),
        click.option("--author", default=None),
        click.option("--on-sale/--no-on-sale", default=False),
        click.option("--deep/--no-deep", default=False),
        click.option("--language", default=None),
        click.option("--skip-owned/--no-skip-owned", default=False),
        click.option("--first-in-series/--no-first-in-series", default=False),
        click.option("--skip-plus/--no-skip-plus", default=False),
        click.option("--only-plus/--no-only-plus", default=False),
        click.option("--exclude-author", "exclude_authors", multiple=True),
        click.option("--exclude-narrator", "exclude_narrators", multiple=True),
        click.option("--exclude-keyword", "exclude_keywords", multiple=True),
    ]
    for option in reversed(options):
        func = option(func)
    return func


@monitor.command("add")
@click.argument("name")
@click.option("--profile", "profile_name", shell_complete=_complete_profile_names)
@click.option("--query", default=None, help="Search query; use | for OR queries")
@click.option("--webhook", default=None, help="Monitor-specific webhook URL")
@click.option("--webhook-format", type=click.Choice(WEBHOOK_FORMATS), default=None)
@_monitor_filter_options
@click.pass_context
def add(ctx, name, profile_name, query, webhook, webhook_format, **flags):
    """Create a profile-backed find monitor or a direct-query search monitor."""
    if (profile_name is None) == (query is None):
        raise click.UsageError(
            "Specify exactly one of --profile NAME or --query QUERY."
        )
    if webhook:
        validate_webhook_url(webhook)
    elif webhook_format:
        raise click.UsageError("--webhook-format requires --webhook")
    mode = "search" if query is not None else "find"
    settings = _monitor_settings(ctx, mode, profile_name, flags)
    definition = {
        "version": 1,
        "name": name,
        "enabled": True,
        "locale": ctx.obj["locale"],
        "mode": mode,
        "query": query or "",
        "profile": profile_name,
        "settings": _settings_dict(settings),
        "webhook": webhook,
        "webhook_format": webhook_format,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    estimate = estimate_monitor_calls(definition)
    if estimate > MAX_MONITOR_API_CALLS_PER_RUN:
        raise click.UsageError(
            f"Monitor would use {estimate} catalog calls; limit is {MAX_MONITOR_API_CALLS_PER_RUN} per run."
        )
    try:
        with run_lock():
            monitors = load_monitors()
            if name in monitors:
                raise click.ClickException(f"Monitor '{name}' already exists.")
            monitors[name] = definition
            save_monitors(monitors)
            state = load_monitor_state()
            state["monitors"].pop(name, None)
            save_monitor_state(state)
    except LockHeldError as exc:
        raise click.ClickException(f"Another run is in progress, try again: {exc}")
    console.print(
        f"[green]Monitor '{name}' added[/green] ({mode}, locale {ctx.obj['locale']}, {estimate} catalog calls/run)"
    )
    console.print(
        f"  Source: {'profile ' + profile_name if profile_name else 'direct query/options'}"
    )
    console.print(
        "[dim]Its first successful tracked run establishes a silent baseline.[/dim]"
    )


@monitor.command("list")
def list_monitors():
    """List monitors and their persisted status without exposing webhook secrets."""
    monitors = load_monitors()
    state = load_monitor_state()
    if not monitors:
        console.print("[dim]No monitors. Use 'deals monitor add --profile NAME'.[/dim]")
        return
    for name, definition in sorted(monitors.items()):
        if not isinstance(definition, dict):
            console.print(f"  [red]{name}: malformed definition[/red]")
            continue
        slot = _monitor_slot(state, name)
        status = "enabled" if definition.get("enabled", True) else "paused"
        source = (
            f"profile {definition['profile']}"
            if definition.get("profile")
            else "direct options"
        )
        console.print(
            f"  [bold]{name}[/bold]  {status}, {definition.get('mode')} {definition.get('locale')}, {source} — {len(slot.get('products', {}))} matches, last success: {slot.get('last_success') or 'never'}, estimate: {_estimate_label(definition)}"
        )
        if slot.get("last_error"):
            console.print(f"    [red]last error: {slot['last_error']}[/red]")


@monitor.command("show")
@click.argument("name", shell_complete=_complete_monitor_names)
def show(name):
    """Show frozen settings and latest health for one monitor."""
    definition = load_monitors().get(name)
    if not isinstance(definition, dict):
        raise click.ClickException(f"Monitor '{name}' not found.")
    _print_monitor(name, definition, load_monitor_state())


@monitor.command("remove")
@click.argument("name", shell_complete=_complete_monitor_names)
@click.option("--yes", is_flag=True, help="Do not ask for confirmation")
def remove(name, yes):
    """Remove a monitor and its baseline."""
    if not yes and not click.confirm(f"Remove monitor '{name}'?"):
        console.print("[dim]Cancelled.[/dim]")
        return
    try:
        with run_lock():
            monitors = load_monitors()
            if name not in monitors:
                raise click.ClickException(f"Monitor '{name}' not found.")
            del monitors[name]
            save_monitors(monitors)
            state = load_monitor_state()
            state["monitors"].pop(name, None)
            save_monitor_state(state)
    except LockHeldError as exc:
        raise click.ClickException(f"Another run is in progress, try again: {exc}")
    console.print(f"[green]Monitor '{name}' removed[/green]")


def _set_enabled(name: str, enabled: bool) -> None:
    try:
        with run_lock():
            monitors = load_monitors()
            definition = monitors.get(name)
            if not isinstance(definition, dict):
                raise click.ClickException(f"Monitor '{name}' not found.")
            if definition.get("enabled", True) == enabled:
                console.print(
                    f"[dim]Monitor '{name}' is already {'enabled' if enabled else 'paused'}.[/dim]"
                )
                return
            definition["enabled"] = enabled
            save_monitors(monitors)
    except LockHeldError as exc:
        raise click.ClickException(f"Another run is in progress, try again: {exc}")
    console.print(
        f"[green]Monitor '{name}' {'resumed' if enabled else 'paused'}[/green]"
    )


@monitor.command("pause")
@click.argument("name", shell_complete=_complete_monitor_names)
def pause(name):
    """Pause a monitor without deleting its snapshot."""
    _set_enabled(name, False)


@monitor.command("resume")
@click.argument("name", shell_complete=_complete_monitor_names)
def resume(name):
    """Resume a previously paused monitor."""
    _set_enabled(name, True)


@monitor.command("test")
@click.argument("name", shell_complete=_complete_monitor_names)
def test(name):
    """Scan a monitor and show events without changing its baseline or health."""
    definition = load_monitors().get(name)
    if not isinstance(definition, dict):
        raise click.ClickException(f"Monitor '{name}' not found.")
    current = scan_monitor(definition)
    slot = _monitor_slot(load_monitor_state(), name)
    events = (
        _events(slot.get("products", {}), current, name)
        if slot.get("initialized")
        else []
    )
    if not slot.get("initialized"):
        console.print(
            f"[dim]{len(current)} match(es); first tracked run will establish a silent baseline.[/dim]"
        )
    else:
        console.print(f"[dim]{len(current)} match(es), {len(events)} event(s).[/dim]")
    if current:
        console.print("[bold]Current matches[/bold]")
        currency = constants.LOCALE_CURRENCY.get(definition.get("locale"), "$")
        for product in current:
            title = product.get("full_title") or product.get("title", "")
            price = product.get("price")
            price_text = (
                f"{currency}{price:.2f}" if isinstance(price, (int, float)) else "-"
            )
            click.echo(f"  {title} — {price_text} — {product.get('asin', '')}")
    for event in events:
        click.echo(f"{event['event']}: {event['title']}")
