"""Saved catalog searches that report changes on scheduled tracking runs."""

from __future__ import annotations

import dataclasses
import datetime
import logging
from typing import Any

import click

from audible_deals import constants
from audible_deals.automation_models import MonitorDefinition
from audible_deals.cli.helpers import (
    _CL,
    _credit_price,
    _get_client,
    _load_profile,
    _resolve_categories,
    _resolve_skip_asins,
)
from audible_deals.cli.options import _complete_profile_names, filter_options
from audible_deals.config_store import (
    load_monitor_state,
    load_monitors,
)
from audible_deals.constants import LOCALE_LANGUAGES, WEBHOOK_FORMATS
from audible_deals.filter_specs import Scope, scope_defaults, specs_for
from audible_deals.locking import LockHeldError
from audible_deals.monitor_service import (
    MAX_MONITOR_API_CALLS_PER_RUN,
    MonitorExistsError,
    MonitorNotFoundError,
    MonitorRuntime,
    MonitorServiceError,
    add_monitor,
    monitor_events,
    monitor_snapshot,
    remove_monitor,
    set_monitor_enabled,
    settings_to_dict,
    validated_monitor_definition,
)
from audible_deals.monitor_service import (
    estimate_monitor_calls as _estimate_monitor_calls,
)
from audible_deals.monitor_service import (
    scan_monitor as _scan_monitor,
)
from audible_deals.presentation.terminal import (
    catalog_scan_progress,
    console,
    safe_markup,
    safe_text,
)
from audible_deals.settings import (
    Settings,
    SettingsResolutionRequest,
    filter_combination_error,
    resolve_settings,
)
from audible_deals.validation import validate_webhook_url

logger = logging.getLogger(__name__)

# Flags a monitor can freeze, and the defaults each source command declares
# differently from Settings (all from the filter registry).
_DIRECT_SETTING_FIELDS = tuple(spec.key for spec in specs_for(Scope.MONITOR))


def _complete_monitor_names(ctx, param, incomplete):
    from click.shell_completion import CompletionItem

    return [
        CompletionItem(name) for name in load_monitors() if name.startswith(incomplete)
    ]


def _runtime() -> MonitorRuntime:
    ctx = click.get_current_context(silent=True)
    return MonitorRuntime(
        get_client=_get_client,
        resolve_categories=_resolve_categories,
        resolve_skip_asins=_resolve_skip_asins,
        progress=catalog_scan_progress,
        credit_price=_credit_price(ctx) if ctx is not None and ctx.obj else None,
    )


def _monitor_settings(
    ctx: click.Context, mode: str, profile_name: str | None, flags: dict[str, Any]
) -> Settings:
    """Resolve the frozen monitor settings with the command's own defaults."""
    profile = _load_profile(profile_name)
    values: dict[str, Any] = scope_defaults(
        Scope.FIND if mode == "find" else Scope.SEARCH
    )
    explicit_options: set[str] = set()
    for key in _DIRECT_SETTING_FIELDS:
        if ctx.get_parameter_source(key) == _CL:
            values[key] = flags[key]
            explicit_options.add(key)
    # Snapshots must be complete. A display limit would manufacture false
    # disappearance/re-entry events as titles move in and out of the top N.
    values["limit"] = None
    explicit_options.add("limit")
    try:
        settings = resolve_settings(
            SettingsResolutionRequest(
                config=ctx.obj.get("config", {}),
                profile=profile,
                cli_flags=values,
                explicit_options=explicit_options,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    if problem := filter_combination_error(settings):
        raise click.UsageError(problem)
    if settings.exclude_seen:
        # "Seen" changes every time results are shown, which would make a
        # frozen snapshot appear and disappear; monitors never use it.
        console.print(
            "[dim]Note: exclude-seen does not apply to monitors and was ignored.[/dim]"
        )
        settings = dataclasses.replace(settings, exclude_seen=False)
    if not settings.language and not settings.all_languages:
        settings = dataclasses.replace(
            settings, language=LOCALE_LANGUAGES[ctx.obj["locale"]]
        )
    return settings


def estimate_monitor_calls(definition: dict) -> int:
    """Estimated catalog-page requests; category lookup is excluded from the budget."""
    try:
        return _estimate_monitor_calls(validated_monitor_definition(definition))
    except (TypeError, MonitorServiceError) as exc:
        raise click.UsageError(str(exc)) from None


def _monitor_slot(state: dict, name: str) -> dict:
    """Return a validated state slot, repairing only this monitor when needed."""
    return monitor_snapshot(state, name).to_dict()


def scan_monitor(definition: dict) -> list[dict]:
    """Fetch a monitor without changing result caches, seen state, or history."""
    try:
        return list(_scan_monitor(validated_monitor_definition(definition), _runtime()))
    except MonitorServiceError as exc:
        raise click.UsageError(str(exc)) from None


def _events(previous: dict, current: list[dict], name: str) -> list[dict]:
    return [event.to_dict() for event in monitor_events(previous, tuple(current), name)]


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
    console.print(f"[bold]{safe_markup(name)}[/bold]  {status}")
    console.print(
        f"  Mode: {safe_markup(definition.get('mode'))}  "
        f"Locale: {safe_markup(definition.get('locale'))}  "
        f"Source: {safe_markup(source)}"
    )
    if query:
        console.print(f"  Query: {safe_markup(query)}")
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
        console.print(
            f"    {safe_markup(key.replace('_', '-'))}: {safe_markup(rendered)}"
        )
    console.print(
        f"  Estimated catalog calls/run: {_estimate_label(definition)} (shared budget {MAX_MONITOR_API_CALLS_PER_RUN})"
    )
    console.print(
        f"  Snapshot: {len(slot.get('products', {}))} item(s)  Last success: {slot.get('last_success') or 'never'}"
    )
    if slot.get("last_error"):
        console.print(f"  [red]Last error: {safe_markup(slot['last_error'])}[/red]")


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


@monitor.command("add")
@click.argument("name")
@click.option("--profile", "profile_name", shell_complete=_complete_profile_names)
@click.option("--query", default=None, help="Search query; use | for OR queries")
@click.option("--webhook", default=None, help="Monitor-specific webhook URL")
@click.option("--webhook-format", type=click.Choice(WEBHOOK_FORMATS), default=None)
@filter_options(Scope.MONITOR)
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
        "settings": settings_to_dict(settings),
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
        add_monitor(MonitorDefinition.from_dict(definition))
    except LockHeldError as exc:
        raise click.ClickException(
            f"Another run is in progress, try again: {exc}"
        ) from exc
    except MonitorExistsError as exc:
        raise click.ClickException(str(exc)) from exc
    console.print(
        f"[green]Monitor '{safe_markup(name)}' added[/green] "
        f"({mode}, locale {ctx.obj['locale']}, {estimate} catalog calls/run)"
    )
    source = f"profile {profile_name}" if profile_name else "direct query/options"
    console.print(f"  Source: {safe_markup(source)}")
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
        try:
            validated_monitor_definition(definition)
        except MonitorServiceError as exc:
            console.print(
                f"  [red]{safe_markup(name)}: malformed definition "
                f"({safe_markup(exc)})[/red]"
            )
            continue
        slot = _monitor_slot(state, name)
        status = "enabled" if definition.get("enabled", True) else "paused"
        source = (
            f"profile {definition['profile']}"
            if definition.get("profile")
            else "direct options"
        )
        console.print(
            f"  [bold]{safe_markup(name)}[/bold]  {status}, "
            f"{safe_markup(definition.get('mode'))} "
            f"{safe_markup(definition.get('locale'))}, {safe_markup(source)} — "
            f"{len(slot.get('products', {}))} matches, last success: "
            f"{safe_markup(slot.get('last_success') or 'never')}, "
            f"estimate: {_estimate_label(definition)}"
        )
        if slot.get("last_error"):
            console.print(
                f"    [red]last error: {safe_markup(slot['last_error'])}[/red]"
            )


@monitor.command("show")
@click.argument("name", shell_complete=_complete_monitor_names)
def show(name):
    """Show frozen settings and latest health for one monitor."""
    definition = load_monitors().get(name)
    if definition is None:
        raise click.ClickException(f"Monitor '{name}' not found.")
    try:
        validated_monitor_definition(definition)
    except MonitorServiceError as exc:
        raise click.ClickException(f"Monitor '{name}' is malformed: {exc}") from exc
    _print_monitor(name, definition, load_monitor_state())


@monitor.command("remove")
@click.argument("name", shell_complete=_complete_monitor_names)
@click.option("--yes", is_flag=True, help="Do not ask for confirmation")
def remove(name, yes):
    """Remove a monitor and its baseline."""
    if not yes and not click.confirm(f"Remove monitor '{safe_text(name)}'?"):
        console.print("[dim]Cancelled.[/dim]")
        return
    try:
        remove_monitor(name)
    except LockHeldError as exc:
        raise click.ClickException(
            f"Another run is in progress, try again: {exc}"
        ) from exc
    except MonitorNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    console.print(f"[green]Monitor '{safe_markup(name)}' removed[/green]")


def _set_enabled(name: str, enabled: bool) -> None:
    try:
        changed = set_monitor_enabled(name, enabled)
    except LockHeldError as exc:
        raise click.ClickException(
            f"Another run is in progress, try again: {exc}"
        ) from exc
    except MonitorNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except MonitorServiceError as exc:
        raise click.ClickException(f"Monitor '{name}' is malformed: {exc}") from exc
    if not changed:
        console.print(
            f"[dim]Monitor '{safe_markup(name)}' is already "
            f"{'enabled' if enabled else 'paused'}.[/dim]"
        )
        return
    console.print(
        f"[green]Monitor '{safe_markup(name)}' "
        f"{'resumed' if enabled else 'paused'}[/green]"
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
    if definition is None:
        raise click.ClickException(f"Monitor '{name}' not found.")
    try:
        validated_monitor_definition(definition)
    except MonitorServiceError as exc:
        raise click.ClickException(f"Monitor '{name}' is malformed: {exc}") from exc
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
        currency = constants.LOCALE_CURRENCY.get(definition.get("locale") or "", "$")
        for product in current:
            title = product.get("full_title") or product.get("title", "")
            price = product.get("price")
            price_text = (
                f"{currency}{price:.2f}" if isinstance(price, (int, float)) else "-"
            )
            click.echo(
                f"  {safe_text(title)} — {price_text} — "
                f"{safe_text(product.get('asin', ''))}"
            )
    for event in events:
        click.echo(f"{safe_text(event['event'])}: {safe_text(event['title'])}")
