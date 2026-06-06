"""Auth, lookup, diagnostic, and completion commands."""

from __future__ import annotations

import datetime
import json as json_mod
import os
import sys
import time
from pathlib import Path

import click
from rich.table import Table

from audible_deals import constants
from audible_deals.cli.helpers import (
    _collect_asins,
    _get_client,
    _resolve_single_last_ref,
)
from audible_deals.config_store import load_notify_state, load_profiles
from audible_deals.constants import _CONFIG_SCHEMA, product_url
from audible_deals.display import (
    console,
    display_categories,
    display_comparison,
    display_product_detail,
)
from audible_deals.results_cache import load_seen_asins
from audible_deals.settings import _PROFILE_EXTRA_KEYS
from audible_deals.validation import validate_asin
from audible_deals.wishlist import load_wishlist


@click.command()
@click.option(
    "--external", is_flag=True, help="Login via external browser (for captcha/2FA)"
)
@click.option(
    "--via-file",
    type=click.Path(path_type=Path),
    default=None,
    help="File path for the callback URL (you save the URL there after login, then press Enter)",
)
@click.pass_context
def login(ctx, external, via_file):
    """Authenticate with Audible.

    \b
    Recommended flow for macOS:
        deals login --external --via-file /tmp/url.txt
    This prints the sign-in URL, waits for you to log in and save the
    callback URL to the file, then press Enter to finish auth.
    """
    dc = _get_client(ctx.obj["locale"])

    if external:
        dc.login_external(callback_url_file=via_file)
    else:
        username = click.prompt("Audible email")
        password = click.prompt("Audible password", hide_input=True)
        dc.login(username, password)

    console.print(f"[green]Authenticated.[/green] Auth saved to {dc.auth_file}")


@click.command("import-auth")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def import_auth(ctx, path: Path):
    """Import auth from an audible-cli JSON file or Libation AccountsSettings.json."""
    dc = _get_client(ctx.obj["locale"])
    dc.import_auth(path)
    console.print(f"[green]Auth imported.[/green] Saved to {dc.auth_file}")


@click.command()
@click.option("--parent", default="", help="Parent category ID (omit for top-level)")
@click.pass_context
def categories(ctx, parent):
    """List Audible categories. Use --parent to drill into subcategories."""
    dc = _get_client(ctx.obj["locale"])
    with dc:
        try:
            cats = dc.get_categories(root=parent)
        except ValueError as e:
            raise click.ClickException(str(e))

    title = "Subcategories" if parent else "Top-Level Categories"
    display_categories(cats, title=title)
    console.print(
        "\n  [dim]Tip: use --parent ID to see subcategories, "
        "or pass the ID to 'deals find --category ID'[/dim]"
    )


@click.command()
@click.argument("asin", required=False, default=None)
@click.option(
    "--last",
    "last_ref",
    type=str,
    default=None,
    help="Use result #N from last search/find",
)
@click.pass_context
def detail(ctx, asin, last_ref):
    """Show detailed info for a product by ASIN."""
    if last_ref is not None:
        asin, desc = _resolve_single_last_ref(last_ref)
        console.print(f"[dim]{desc}[/dim]")
    if not asin:
        raise click.UsageError("Provide an ASIN or use --last N.")
    validate_asin(asin)
    dc = _get_client(ctx.obj["locale"])
    with dc:
        try:
            product = dc.get_product(asin)
        except ValueError as e:
            raise click.ClickException(str(e))

    display_product_detail(product)


@click.command("open")
@click.argument("asin", required=False, default=None)
@click.option(
    "--last",
    "last_ref",
    type=str,
    default=None,
    help="Use result #N from last search/find",
)
@click.pass_context
def open_cmd(ctx, asin, last_ref):
    """Open an audiobook's Audible page in your browser."""
    if last_ref is not None:
        asin, desc = _resolve_single_last_ref(last_ref)
        console.print(f"[dim]{desc}[/dim]")
    if not asin:
        raise click.UsageError("Provide an ASIN or use --last N.")
    validate_asin(asin)
    url = product_url(asin, ctx.obj["locale"])
    console.print(f"[dim]Opening {url}[/dim]")
    click.launch(url)


@click.command()
@click.argument("asins", nargs=-1, required=False)
@click.option(
    "--last",
    "last_refs",
    type=str,
    multiple=True,
    help="Use result #N from last search/find (repeatable)",
)
@click.pass_context
def compare(ctx, asins, last_refs):
    """Compare multiple products side-by-side.

    \b
    Example:
        deals compare B00R6S1RCY B00I2VWW5U B019NMZ6FE
        deals compare --last 1 --last 3
    """
    all_asins = _collect_asins(asins, last_refs)

    if len(all_asins) < 2:
        raise click.UsageError("Provide at least 2 ASINs to compare.")

    for asin in all_asins:
        validate_asin(asin)

    dc = _get_client(ctx.obj["locale"])
    with dc:
        products = dc.get_products_batch(all_asins)

    found_asins = {p.asin for p in products}
    for asin in all_asins:
        if asin not in found_asins:
            console.print(f"[red]Not found: {asin}[/red]")

    if len(products) < 2:
        raise click.ClickException("Need at least 2 valid products to compare.")

    # Preserve the order the user specified
    asin_order = {asin: i for i, asin in enumerate(all_asins)}
    products.sort(key=lambda p: asin_order.get(p.asin, 999))

    display_comparison(products)


@click.command()
@click.pass_context
def doctor(ctx):
    """Diagnostic checks for auth, config, and marketplace reachability."""
    rows: list[tuple[str, str, str]] = []
    failures = 0

    def add(check, status, detail=""):
        nonlocal failures
        if status == "FAIL":
            failures += 1
            rendered = "[bold red]✗ FAIL[/bold red]"
        elif status == "WARN":
            rendered = "[yellow]⚠ WARN[/yellow]"
        else:
            rendered = "[green]✓ PASS[/green]"
        rows.append((check, rendered, detail))

    if constants.CONFIG_DIR.exists():
        add("Config directory", "PASS", str(constants.CONFIG_DIR))
    else:
        add(
            "Config directory",
            "WARN",
            f"Not found — will be created at {constants.CONFIG_DIR}",
        )

    auth_ok = constants.AUTH_FILE.exists()
    if not auth_ok:
        add("Auth file present", "FAIL", "Run 'deals login' or 'deals import-auth'")
    else:
        add("Auth file present", "PASS", str(constants.AUTH_FILE))

    auth_data = None
    if auth_ok:
        try:
            auth_data = json_mod.loads(constants.AUTH_FILE.read_text())
            if not isinstance(auth_data, dict):
                raise ValueError("not a JSON object")
            add("Auth file parseable", "PASS")
        except Exception as e:
            add("Auth file parseable", "FAIL", str(e))
            auth_ok = False

    if auth_ok and auth_data is not None:
        expires = auth_data.get("expires")
        if expires is None:
            add(
                "Auth token expiry",
                "WARN",
                "expires field missing — token freshness unknown",
            )
        else:
            try:
                exp = float(expires)
                now = time.time()
                if exp < now:
                    add(
                        "Auth token expiry",
                        "FAIL",
                        "Token has expired — run 'deals login'",
                    )
                    auth_ok = False
                elif exp < now + 86400:
                    add(
                        "Auth token expiry",
                        "WARN",
                        "Token expires within 24h — consider refreshing",
                    )
                else:
                    add("Auth token expiry", "PASS")
            except (TypeError, ValueError):
                add("Auth token expiry", "WARN", "Could not parse expires field")

    if auth_ok:
        try:
            dc = _get_client(ctx.obj["locale"])
            with dc:
                dc._api_get("1.0/catalog/products", num_results=1)
            add("Marketplace reachable", "PASS")
        except Exception as e:
            add("Marketplace reachable", "FAIL", f"{type(e).__name__}: {e}")
    else:
        add("Marketplace reachable", "WARN", "Skipped — auth checks failed")

    if constants.CONFIG_FILE.exists():
        try:
            cfg = json_mod.loads(constants.CONFIG_FILE.read_text())
            if not isinstance(cfg, dict):
                add(
                    "Config file valid",
                    "FAIL",
                    f"Expected a JSON object, got {type(cfg).__name__}",
                )
                add("Unknown config keys", "WARN", "Skipped — config file unparseable")
            else:
                errors = [
                    f"{k}: expected {_CONFIG_SCHEMA[k].__name__}, got {type(v).__name__}"
                    for k, v in cfg.items()
                    if k in _CONFIG_SCHEMA and not isinstance(v, _CONFIG_SCHEMA[k])
                ]
                if errors:
                    add("Config file valid", "FAIL", "; ".join(errors))
                else:
                    add("Config file valid", "PASS")
                unknown = sorted(k for k in cfg if k not in _CONFIG_SCHEMA)
                if unknown:
                    add("Unknown config keys", "WARN", ", ".join(unknown))
                else:
                    add("Unknown config keys", "PASS")
        except Exception as e:
            add("Config file valid", "FAIL", str(e))
            add("Unknown config keys", "WARN", "Skipped — config file unparseable")
    else:
        add("Config file valid", "PASS", "No config file (using defaults)")

    if constants.PROFILES_FILE.exists():
        try:
            profiles = load_profiles()
            valid_profile_keys = set(_CONFIG_SCHEMA) | set(_PROFILE_EXTRA_KEYS)
            bad_profiles: dict[str, list[str]] = {}
            for pname, popts in profiles.items():
                bad = sorted(k for k in popts if k not in valid_profile_keys)
                if bad:
                    bad_profiles[pname] = bad
            if bad_profiles:
                detail = "; ".join(
                    f"{n}: {', '.join(ks)}" for n, ks in sorted(bad_profiles.items())
                )
                add("Unknown profile keys", "WARN", detail)
            else:
                add("Unknown profile keys", "PASS")
        except Exception as e:
            add("Unknown profile keys", "WARN", str(e))

    try:
        ns = load_notify_state()
        today = datetime.date.today()
        malformed = 0
        stale = 0
        for entry in ns.values():
            if not isinstance(entry, dict):
                malformed += 1
                continue
            try:
                d = datetime.date.fromisoformat(entry["date"])
                age = abs((today - d).days)
                if age > 365:
                    stale += 1
            except (KeyError, ValueError, TypeError):
                malformed += 1
                continue
            if not isinstance(entry.get("price"), (int, float)):
                malformed += 1
        if malformed or stale:
            parts = []
            if malformed:
                parts.append(f"{malformed} malformed")
            if stale:
                parts.append(f"{stale} stale (>365 days)")
            add("Notify-state health", "WARN", "; ".join(parts))
        elif ns:
            add("Notify-state health", "PASS", f"{len(ns)} suppressed ASIN(s) tracked")
        else:
            add("Notify-state health", "PASS", "No entries")
    except Exception as e:
        add("Notify-state health", "WARN", str(e))

    try:
        load_wishlist()
        add("Wishlist parseable", "PASS")
    except Exception as e:
        add("Wishlist parseable", "FAIL", str(e))

    try:
        load_profiles()
        add("Profiles parseable", "PASS")
    except Exception as e:
        add("Profiles parseable", "FAIL", str(e))

    try:
        load_seen_asins()
        add("Seen-ASINs parseable", "PASS")
    except Exception as e:
        add("Seen-ASINs parseable", "FAIL", str(e))

    if constants.HISTORY_DIR.exists():
        count = sum(1 for _ in constants.HISTORY_DIR.glob("*.json"))
        add("Price history directory", "PASS", f"{count} ASIN(s) tracked")
    else:
        add("Price history directory", "PASS", "Not yet created (optional)")

    table = Table(title="deals doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", style="dim")
    for r in rows:
        table.add_row(*r)
    console.print(table)
    if failures:
        ctx.exit(1)


@click.command("completions")
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def completions(shell):
    """Generate shell completion script.

    \b
    Install completions:
        deals completions bash >> ~/.bashrc
        deals completions zsh >> ~/.zshrc
        deals completions fish > ~/.config/fish/completions/deals.fish
    """
    import shutil
    import subprocess

    env = {**os.environ, "_DEALS_COMPLETE": f"{shell}_source"}

    deals_bin = shutil.which("deals")
    if deals_bin:
        result = subprocess.run(
            [deals_bin],
            capture_output=True,
            text=True,
            env=env,
        )
    else:
        result = subprocess.run(
            [sys.executable, "-m", "audible_deals"],
            capture_output=True,
            text=True,
            env=env,
        )

    if result.stdout:
        click.echo(result.stdout)
    else:
        click.echo(result.stderr, err=True)
