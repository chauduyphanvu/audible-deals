"""Config and saved-profile management commands."""

from __future__ import annotations

import textwrap

import click

from audible_deals import json_output, secret_store
from audible_deals.cli.helpers import _CL
from audible_deals.cli.options import (
    _complete_profile_names,
    filter_options,
    json_option,
)
from audible_deals.config_store import (
    CONFIG_SCHEMA,
    coerce_config_value,
    config_transaction,
    load_config,
    load_profiles,
    profiles_transaction,
    validate_config_key,
)
from audible_deals.filter_specs import (
    FILTERS,
    Scope,
    config_key_aliases,
)
from audible_deals.notices import note
from audible_deals.presentation.terminal import console, safe_markup
from audible_deals.settings import (
    profile_validation_error,
    resolve_plus_flags,
)


@click.group("config", invoke_without_command=True)
@click.pass_context
def config_cmd(ctx):
    """Manage global defaults for deals commands."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(config_list)


def _config_set_help() -> str:
    """Help text for ``config set`` with the valid keys taken from the schema."""
    spelled = {key: key.replace("_", "-") for key in CONFIG_SCHEMA}
    for alias, key in config_key_aliases().items():
        spelled[key] = f"{spelled[key]} (or {alias.replace('_', '-')})"
    keys = textwrap.fill(
        ", ".join(sorted(spelled.values())),
        width=62,
        initial_indent="Valid keys: ",
        subsequent_indent=" " * 12,
        break_on_hyphens=False,
    )
    return (
        "Set a global default. KEY uses hyphens or underscores.\n\n"
        f"\b\n{keys}\n"
        "Example:\n"
        "    deals config set max-price 5\n"
        "    deals config set skip-owned true"
    )


_SECRET_KEYS = {
    "webhook": secret_store.WEBHOOK_ACCOUNT,
    "webhook_headers": secret_store.HEADERS_ACCOUNT,
}


def _switch_secret_storage(target: str, yes: bool) -> None:
    """``config set secret-storage``: move the secrets, then record the choice."""
    from audible_deals import secret_migration

    plan = secret_migration.plan_for(target)
    if target == "keyring":
        # Say so now if there is no keychain, before asking anything.
        secret_store.ensure_available()
    if plan.count:
        if target == "keyring":
            destination = (
                "into the OS keychain, leaving only references in auth.json "
                "and config.json."
            )
        else:
            destination = (
                "into plain files in the data directory (auth.json stays readable "
                "only by you) and removes them from the keychain."
            )
        click.echo("This moves:")
        for item in plan.describe():
            click.echo(f"  - {item}")
        click.echo(destination)
        if not yes and not click.confirm("Continue?"):
            console.print("[dim]Cancelled. Nothing was changed.[/dim]")
            return
    report = secret_migration.migrate(target)
    console.print(f"[green]Config set:[/green] secret_storage = {target}")
    for item in report.moved:
        console.print(f"  moved: {safe_markup(item)}")
    if report.leftovers:
        note(
            "Could not remove these keychain entries (remove them in your keychain "
            f"app, service '{secret_store.SERVICE}'): {', '.join(report.leftovers)}"
        )
    if target == "keyring":
        note(
            "Background runs (deals track) read the keychain too; if the keychain "
            "is locked or unavailable when they run, they fail. 'deals doctor' "
            "checks this."
        )


@config_cmd.command("set", help=_config_set_help())
@click.argument("key")
@click.argument("value")
@click.option("--yes", "-y", is_flag=True, help="Do not ask before moving secrets")
def config_set(key, value, yes):
    norm_key = validate_config_key(key)
    coerced = coerce_config_value(norm_key, value)
    if norm_key == "secret_storage":
        _switch_secret_storage(coerced, yes)
        return
    removed_key = None
    previous_secret = None
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
        if norm_key in _SECRET_KEYS:
            previous_secret = cfg.get(norm_key)
            coerced = secret_store.protect(
                coerced, _SECRET_KEYS[norm_key], mode=secret_store.mode_of(cfg)
            )
        cfg[norm_key] = coerced
        if norm_key == "language" and coerced:
            cfg["all_languages"] = False
        elif norm_key == "all_languages" and coerced:
            cfg.pop("language", None)
    if (
        norm_key in _SECRET_KEYS
        and not _is_secret_reference(coerced)
        and _is_secret_reference(previous_secret)
    ):
        secret_store.discard(previous_secret)  # the old keychain entry is orphaned
    console.print(
        f"[green]Config set:[/green] {norm_key} = "
        f"{safe_markup(_config_value(norm_key, coerced, False))}"
    )
    if removed_key:
        console.print(f"[yellow]Removed conflicting config key:[/yellow] {removed_key}")
    if norm_key == "webhook_allow_private" and coerced:
        note(
            "Webhooks may now reach servers on your own network (localhost, LAN). "
            "Only use webhook URLs you trust."
        )


@config_cmd.command("get")
@click.argument("key")
@click.option(
    "--show-secrets", is_flag=True, help="Show webhook URLs and header values."
)
@json_option("Output the key and its value as JSON")
def config_get(key, show_secrets, json_flag):
    """Get a global default value."""
    norm_key = validate_config_key(key)
    cfg = _with_secrets(load_config()) if show_secrets else load_config()
    if json_flag:
        shown = cfg if show_secrets else json_output.redact_config(cfg)
        click.echo(
            json_output.dumps(
                json_output.with_schema(
                    key=norm_key,
                    set=norm_key in cfg,
                    value=shown.get(norm_key),
                )
            )
        )
        return
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
@json_option("Output the global defaults as JSON")
def config_list(show_secrets=False, json_flag=False):
    """List all set global defaults."""
    cfg = _with_secrets(load_config()) if show_secrets else load_config()
    if json_flag:
        shown = cfg if show_secrets else json_output.redact_config(cfg)
        click.echo(
            json_output.dumps(
                json_output.with_schema(config=dict(sorted(shown.items())))
            )
        )
        return
    if not cfg:
        console.print(
            "[dim]No global defaults set. Use 'deals config set KEY VALUE' to set one.[/dim]"
        )
        return
    for k, v in sorted(cfg.items()):
        console.print(
            f"  {safe_markup(k)} = {safe_markup(_config_value(k, v, show_secrets))}"
        )


def _is_secret_reference(value: object) -> bool:
    if isinstance(value, list):
        return len(value) == 1 and secret_store.is_reference(value[0])
    return secret_store.is_reference(value)


def _with_secrets(cfg: dict) -> dict:
    """*cfg* with keychain references replaced by the secrets they name."""
    shown = dict(cfg)
    for key in _SECRET_KEYS:
        if key in shown:
            shown[key] = secret_store.resolve(shown[key])
    return shown


def _config_value(key: str, value, show_secrets: bool) -> str:
    if show_secrets or key not in json_output.SECRET_CONFIG_KEYS:
        return repr(value)
    shown = json_output.redact_config_value(key, value)  # same rule as --json
    return shown if isinstance(shown, str) else repr(shown)


@config_cmd.command("reset")
@click.argument("key", required=False, default=None)
@click.option("--yes", "-y", is_flag=True, help="Do not ask before clearing everything")
def config_reset(key, yes):
    """Remove a key from global defaults, or clear all if no key given."""
    if key is None:
        if not yes and not click.confirm("Remove all global defaults?"):
            console.print("[dim]Cancelled.[/dim]")
            return
        with config_transaction() as cfg:
            orphaned = [cfg.get(key) for key in _SECRET_KEYS]
            cfg.clear()
        for value in orphaned:
            secret_store.discard(value)
        console.print("[green]All global defaults cleared.[/green]")
        return
    norm_key = validate_config_key(key)
    removed = False
    orphaned_value = None
    with config_transaction() as cfg:
        if norm_key in cfg:
            orphaned_value = cfg[norm_key] if norm_key in _SECRET_KEYS else None
            del cfg[norm_key]
            removed = True
    if removed and orphaned_value is not None:
        secret_store.discard(orphaned_value)
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
@filter_options(Scope.PROFILE)
@click.pass_context
def profile_save(ctx, name, **kwargs):
    """Save a search profile.

    Any filter accepted by `deals find` can be saved; `deals find --profile
    NAME`, `deals search --profile NAME` and `deals monitor add --profile NAME`
    then start from it.

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
    for spec in FILTERS:
        valid = spec.valid
        if valid is not None and spec.key in saved and saved[spec.key] not in valid:
            raise click.ClickException(
                f"Invalid {spec.key} {saved[spec.key]!r}. Choose from: "
                f"{', '.join(sorted(valid))}."
            )
    if (
        saved.get("released_after")
        and saved.get("released_before")
        and saved["released_after"] > saved["released_before"]
    ):
        raise click.UsageError(
            "--released-after cannot be later than --released-before"
        )
    with profiles_transaction() as profiles:
        profiles[name] = saved
    console.print(
        f"[green]Profile '{safe_markup(name)}' saved[/green] ({len(saved)} options)"
    )


@profile.command("list")
@json_option("Output the saved profiles as JSON")
def profile_list(json_flag):
    """List saved profiles."""
    profiles = load_profiles()
    if json_flag:
        click.echo(json_output.dumps(_profiles_payload(profiles)))
        return
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


_KEY_TO_FLAG: dict[str, str] = {spec.key: spec.flag_name for spec in FILTERS}


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


def _profiles_payload(profiles: dict) -> dict:
    entries = []
    for name, opts in profiles.items():
        error = profile_validation_error(opts)
        entry: dict = {"name": name, "valid": error is None}
        if error is None:
            entry["options"] = dict(sorted(opts.items()))
        else:
            entry["options"] = None
            entry["error"] = error
        entries.append(entry)
    return json_output.with_schema(profiles=entries)


@profile.command("show")
@click.argument("name", shell_complete=_complete_profile_names)
@json_option("Output the profile as JSON")
def profile_show(name, json_flag):
    """Show the saved flags for a named profile."""
    profiles = load_profiles()
    if name not in profiles:
        raise click.ClickException(f"Profile '{name}' not found.")
    opts = profiles[name]
    if error := profile_validation_error(opts):
        raise click.ClickException(
            f"Profile '{name}' is malformed: {error}; save it again."
        )
    if json_flag:
        click.echo(
            json_output.dumps(
                json_output.with_schema(
                    profile={"name": name, "options": dict(sorted(opts.items()))}
                )
            )
        )
        return
    console.print(f"\n[bold]Profile: {safe_markup(name)}[/bold]\n")
    for part in _opts_to_flag_parts(dict(sorted(opts.items()))):
        console.print(f"  {safe_markup(part)}")
    console.print()
