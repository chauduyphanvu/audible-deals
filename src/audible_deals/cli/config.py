"""Config and saved-profile management commands."""

from __future__ import annotations

import click

from audible_deals.cli.helpers import _CL
from audible_deals.cli.options import _complete_profile_names
from audible_deals.config_store import (
    coerce_config_value,
    config_transaction,
    load_config,
    load_profiles,
    profiles_transaction,
    validate_config_key,
)
from audible_deals.constants import ALL_SORT_OPTIONS
from audible_deals.presentation.terminal import console, safe_markup
from audible_deals.settings import profile_validation_error, resolve_plus_flags
from audible_deals.validation import NONNEGATIVE_FLOAT, NONNEGATIVE_INT, RATING_FLOAT


@click.group("config", invoke_without_command=True)
@click.pass_context
def config_cmd(ctx):
    """Manage global defaults for deals commands."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(config_list)


@config_cmd.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key, value):
    """Set a global default. KEY uses hyphens or underscores.

    \b
    Valid keys: skip-owned, max-price, max-pph (or max-price-per-hour), min-rating,
                min-ratings, min-hours, min-discount, language, locale, sort,
                pages, on-sale, deep, first-in-series, all-languages,
                interactive, limit, narrator, author, series, publisher,
                skip-plus, only-plus, credit-price, webhook, webhook-format,
                webhook-headers
    Example:
        deals config set max-price 5
        deals config set skip-owned true
    """
    norm_key = validate_config_key(key)
    coerced = coerce_config_value(norm_key, value)
    removed_key = None
    with config_transaction() as cfg:
        if norm_key in {"skip_plus", "only_plus"} and coerced:
            opposite = "only_plus" if norm_key == "skip_plus" else "skip_plus"
            if opposite in cfg:
                removed_key = opposite
                del cfg[opposite]
        else:
            try:
                resolve_plus_flags(
                    cfg.get("skip_plus", False), cfg.get("only_plus", False)
                )
            except ValueError as exc:
                raise click.ClickException(str(exc)) from None
        cfg[norm_key] = coerced
        if norm_key == "language" and coerced:
            cfg["all_languages"] = False
        elif norm_key == "all_languages" and coerced:
            cfg.pop("language", None)
    console.print(
        f"[green]Config set:[/green] {norm_key} = "
        f"{safe_markup(_config_value(norm_key, coerced, False))}"
    )
    if removed_key:
        console.print(f"[yellow]Removed conflicting config key:[/yellow] {removed_key}")


@config_cmd.command("get")
@click.argument("key")
@click.option(
    "--show-secrets", is_flag=True, help="Show webhook URLs and header values."
)
def config_get(key, show_secrets):
    """Get a global default value."""
    norm_key = validate_config_key(key)
    cfg = load_config()
    if norm_key not in cfg:
        console.print(f"[dim]{norm_key} is not set[/dim]")
    else:
        console.print(
            f"{norm_key} = "
            f"{safe_markup(_config_value(norm_key, cfg[norm_key], show_secrets))}"
        )


@config_cmd.command("list")
@click.option(
    "--show-secrets", is_flag=True, help="Show webhook URLs and header values."
)
def config_list(show_secrets=False):
    """List all set global defaults."""
    cfg = load_config()
    if not cfg:
        console.print(
            "[dim]No global defaults set. Use 'deals config set KEY VALUE' to set one.[/dim]"
        )
        return
    for k, v in sorted(cfg.items()):
        console.print(
            f"  {safe_markup(k)} = {safe_markup(_config_value(k, v, show_secrets))}"
        )


def _config_value(key: str, value, show_secrets: bool) -> str:
    if show_secrets or key not in {"webhook", "webhook_headers"}:
        return repr(value)
    if key == "webhook":
        return "<redacted>"
    if not isinstance(value, (list, tuple)):
        return "<redacted>"
    names = []
    for item in value:
        if not isinstance(item, str):
            continue
        name, separator, _ = item.partition(":")
        if separator and name.strip():
            names.append(f"{name.strip()}: <redacted>")
    return repr(names) if names else "<redacted>"


@config_cmd.command("reset")
@click.argument("key", required=False, default=None)
def config_reset(key):
    """Remove a key from global defaults, or clear all if no key given."""
    if key is None:
        if not click.confirm("Remove all global defaults?"):
            console.print("[dim]Cancelled.[/dim]")
            return
        with config_transaction() as cfg:
            cfg.clear()
        console.print("[green]All global defaults cleared.[/green]")
        return
    norm_key = validate_config_key(key)
    removed = False
    with config_transaction() as cfg:
        if norm_key in cfg:
            del cfg[norm_key]
            removed = True
    if removed:
        console.print(f"[green]Config key '{norm_key}' removed.[/green]")
    else:
        console.print(f"[dim]Config key '{norm_key}' was not set.[/dim]")


@click.group(invoke_without_command=True)
@click.pass_context
def profile(ctx):
    """Manage saved search profiles."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(profile_list)


@profile.command("save")
@click.argument("name")
@click.option("--genre", default="")
@click.option("--exclude-genre", multiple=True)
@click.option("--keywords", default="")
@click.option("--max-price", type=NONNEGATIVE_FLOAT, default=None)
@click.option("--sort", default="")
@click.option("--min-rating", type=RATING_FLOAT, default=0.0)
@click.option("--min-ratings", type=NONNEGATIVE_INT, default=0)
@click.option("--min-hours", type=NONNEGATIVE_FLOAT, default=0.0)
@click.option("--narrator", default="")
@click.option("--author", default="")
@click.option("--series", default="")
@click.option("--exclude-author", "exclude_authors", multiple=True)
@click.option("--exclude-narrator", "exclude_narrators", multiple=True)
@click.option("--on-sale/--no-on-sale", default=False)
@click.option("--min-discount", type=click.IntRange(min=0, max=100), default=0)
@click.option("--max-price-per-hour", "max_pph", type=NONNEGATIVE_FLOAT, default=None)
@click.option("--publisher", default="")
@click.option("--deep/--no-deep", default=False)
@click.option("--pages", type=click.IntRange(min=1), default=None)
@click.option("--first-in-series/--no-first-in-series", default=False)
@click.option("--all-languages/--no-all-languages", default=False)
@click.option("--limit", "-n", type=click.IntRange(min=0), default=None)
@click.option("--skip-owned/--no-skip-owned", default=False)
@click.option("--language", default="")
@click.option("--interactive/--no-interactive", "-i", default=False)
@click.option("--skip-plus/--no-skip-plus", default=False)
@click.option("--only-plus/--no-only-plus", default=False)
@click.option("--exclude-keyword", "exclude_keywords", multiple=True)
@click.pass_context
def profile_save(ctx, name, **kwargs):
    """Save a search profile.

    \b
    Example:
        deals profile save my-scifi --genre sci-fi --max-price 5 --min-rating 4 --first-in-series
        deals profile save work --skip-owned --language english --interactive
        deals find --profile my-scifi
        deals search "Brandon Sanderson" --profile my-scifi
    """
    # Only save values explicitly passed on the command line
    saved = {k: v for k, v in kwargs.items() if ctx.get_parameter_source(k) == _CL}
    try:
        resolve_plus_flags(saved.get("skip_plus", False), saved.get("only_plus", False))
    except ValueError as exc:
        raise click.UsageError(str(exc)) from None
    if saved.get("language") and saved.get("all_languages"):
        raise click.UsageError("Use --language or --all-languages, not both.")
    if "sort" in saved and saved["sort"] not in ALL_SORT_OPTIONS:
        raise click.ClickException(
            f"Invalid sort {saved['sort']!r}. Choose from: "
            f"{', '.join(sorted(ALL_SORT_OPTIONS))}."
        )
    with profiles_transaction() as profiles:
        profiles[name] = saved
    console.print(
        f"[green]Profile '{safe_markup(name)}' saved[/green] ({len(saved)} options)"
    )


@profile.command("list")
def profile_list():
    """List saved profiles."""
    profiles = load_profiles()
    if not profiles:
        console.print(
            "[dim]No profiles saved. Use 'deals profile save NAME --flags...' to create one.[/dim]"
        )
        return

    for name, opts in profiles.items():
        error = profile_validation_error(opts)
        if not isinstance(name, str) or error:
            console.print(
                f"  [red]{safe_markup(name)}  malformed profile"
                f"{': ' + safe_markup(error) if error else ''}[/red]"
            )
            continue
        flags = " ".join(_opts_to_flag_parts(opts))
        console.print(
            f"  [bold]{safe_markup(name)}[/bold]  [dim]{safe_markup(flags)}[/dim]"
        )


@profile.command("delete")
@click.argument("name", shell_complete=_complete_profile_names)
def profile_delete(name):
    """Delete a saved profile."""
    with profiles_transaction() as profiles:
        if name not in profiles:
            raise click.ClickException(f"Profile '{name}' not found.")
        del profiles[name]
    console.print(f"[green]Profile '{safe_markup(name)}' deleted[/green]")


_KEY_TO_FLAG: dict[str, str] = {
    "exclude_authors": "exclude-author",
    "exclude_narrators": "exclude-narrator",
    "exclude_keywords": "exclude-keyword",
    "max_pph": "max-price-per-hour",
}


def _opts_to_flag_parts(opts: dict) -> list[str]:
    """Convert profile opts dict to a list of CLI flag strings."""
    parts: list[str] = []
    for k, v in opts.items():
        flag = _KEY_TO_FLAG.get(k, k.replace("_", "-"))
        if isinstance(v, bool):
            parts.append(f"--{flag}" if v else f"--no-{flag}")
        elif isinstance(v, (list, tuple)):
            parts.extend(f"--{flag} {item}" for item in v)
        else:
            parts.append(f"--{flag} {v}")
    return parts


@profile.command("show")
@click.argument("name", shell_complete=_complete_profile_names)
def profile_show(name):
    """Show the saved flags for a named profile."""
    profiles = load_profiles()
    if name not in profiles:
        raise click.ClickException(f"Profile '{name}' not found.")
    opts = profiles[name]
    if error := profile_validation_error(opts):
        raise click.ClickException(
            f"Profile '{name}' is malformed: {error}; save it again."
        )
    console.print(f"\n[bold]Profile: {safe_markup(name)}[/bold]\n")
    for part in _opts_to_flag_parts(dict(sorted(opts.items()))):
        console.print(f"  {safe_markup(part)}")
    console.print()
