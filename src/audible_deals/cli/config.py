"""Config and saved-profile management commands."""

from __future__ import annotations

import click

from audible_deals.cli.helpers import _CL
from audible_deals.cli.options import _complete_profile_names
from audible_deals.config_store import (
    coerce_config_value,
    load_config,
    load_profiles,
    save_config,
    save_profiles,
    validate_config_key,
)
from audible_deals.constants import ALL_SORT_OPTIONS
from audible_deals.presentation.terminal import console


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
    cfg = load_config()
    cfg[norm_key] = coerced
    save_config(cfg)
    console.print(f"[green]Config set:[/green] {norm_key} = {coerced!r}")


@config_cmd.command("get")
@click.argument("key")
def config_get(key):
    """Get a global default value."""
    norm_key = validate_config_key(key)
    cfg = load_config()
    if norm_key not in cfg:
        console.print(f"[dim]{norm_key} is not set[/dim]")
    else:
        console.print(f"{norm_key} = {cfg[norm_key]!r}")


@config_cmd.command("list")
def config_list():
    """List all set global defaults."""
    cfg = load_config()
    if not cfg:
        console.print(
            "[dim]No global defaults set. Use 'deals config set KEY VALUE' to set one.[/dim]"
        )
        return
    for k, v in sorted(cfg.items()):
        console.print(f"  {k} = {v!r}")


@config_cmd.command("reset")
@click.argument("key", required=False, default=None)
def config_reset(key):
    """Remove a key from global defaults, or clear all if no key given."""
    cfg = load_config()
    if key is None:
        if not click.confirm("Remove all global defaults?"):
            console.print("[dim]Cancelled.[/dim]")
            return
        save_config({})
        console.print("[green]All global defaults cleared.[/green]")
        return
    norm_key = validate_config_key(key)
    if norm_key in cfg:
        del cfg[norm_key]
        save_config(cfg)
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
@click.option("--max-price", type=click.FloatRange(min=0), default=None)
@click.option("--sort", default="")
@click.option("--min-rating", type=click.FloatRange(min=0), default=0.0)
@click.option("--min-ratings", type=click.IntRange(min=0), default=0)
@click.option("--min-hours", type=click.FloatRange(min=0), default=0.0)
@click.option("--narrator", default="")
@click.option("--author", default="")
@click.option("--series", default="")
@click.option("--exclude-author", "exclude_authors", multiple=True)
@click.option("--exclude-narrator", "exclude_narrators", multiple=True)
@click.option("--on-sale/--no-on-sale", default=False)
@click.option("--min-discount", type=click.IntRange(min=0, max=100), default=0)
@click.option(
    "--max-price-per-hour", "max_pph", type=click.FloatRange(min=0), default=None
)
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
    profiles = load_profiles()
    # Only save values explicitly passed on the command line
    saved = {k: v for k, v in kwargs.items() if ctx.get_parameter_source(k) == _CL}
    if "sort" in saved and saved["sort"] not in ALL_SORT_OPTIONS:
        raise click.ClickException(
            f"Invalid sort {saved['sort']!r}. Choose from: "
            f"{', '.join(sorted(ALL_SORT_OPTIONS))}."
        )
    profiles[name] = saved
    save_profiles(profiles)
    console.print(f"[green]Profile '{name}' saved[/green] ({len(saved)} options)")


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
        flags = " ".join(_opts_to_flag_parts(opts))
        console.print(f"  [bold]{name}[/bold]  [dim]{flags}[/dim]")


@profile.command("delete")
@click.argument("name", shell_complete=_complete_profile_names)
def profile_delete(name):
    """Delete a saved profile."""
    profiles = load_profiles()
    if name not in profiles:
        raise click.ClickException(f"Profile '{name}' not found.")
    del profiles[name]
    save_profiles(profiles)
    console.print(f"[green]Profile '{name}' deleted[/green]")


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
    console.print(f"\n[bold]Profile: {name}[/bold]\n")
    for part in _opts_to_flag_parts(dict(sorted(opts.items()))):
        console.print(f"  {part}")
    console.print()
