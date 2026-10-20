"""Auth, lookup, diagnostic, and completion commands."""

from __future__ import annotations

import datetime
import json as json_mod
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import click
from rich.table import Table

from audible_deals import db, json_import, paths
from audible_deals.auth_state import inspect_auth_file
from audible_deals.cli.helpers import (
    _CL,
    _credit_price,
    _get_client,
    _resolve_cli_selectors,
)
from audible_deals.config_store import (
    CONFIG_SCHEMA,
    config_numeric_errors,
    load_monitors,
)
from audible_deals.constants import product_url
from audible_deals.monitor_service import (
    MonitorServiceError,
    validated_monitor_definition,
)
from audible_deals.presentation.products import (
    display_comparison,
    display_product_detail,
)
from audible_deals.presentation.reports import display_categories
from audible_deals.presentation.terminal import console, safe_markup, safe_text
from audible_deals.settings import (
    _PROFILE_EXTRA_KEYS,
    profile_validation_error,
    resolve_plus_flags,
)
from audible_deals.state_store import (
    load_install_record,
    load_notify_state,
    load_track_state,
    monitor_summaries,
)
from audible_deals.storage import read_text
from audible_deals.wishlist import inspect_wishlist


def _reject_json_constant(value: str):
    raise ValueError(f"invalid JSON constant: {value}")


def _load_raw_json(path: Path, expected_type: type):
    data = json_mod.loads(read_text(path), parse_constant=_reject_json_constant)
    if not isinstance(data, expected_type):
        json_names: dict[type, str] = {dict: "object", list: "array"}
        expected_name = json_names.get(expected_type, expected_type.__name__)
        raise ValueError(f"Expected a JSON {expected_name}, got {type(data).__name__}")
    return data


@click.command()
@click.option(
    "--external/--credentials",
    default=True,
    help="Use browser sign-in (default) or enter Audible credentials in the terminal.",
)
@click.option(
    "--open/--no-open",
    "open_browser",
    default=True,
    help="Open the sign-in URL in your browser (default: open).",
)
@click.option(
    "--via-file",
    type=click.Path(path_type=Path),
    default=None,
    help="File path for the callback URL (you save the URL there after login, then press Enter)",
)
@click.pass_context
def login(ctx, external, open_browser, via_file):
    """Authenticate with Audible.

    \b
    Browser sign-in is the default. After signing in, a "page not found"
    response is expected; copy its full URL back to this terminal.

    \b
    For a remote terminal or long callback URL:
        deals login --no-open --via-file /tmp/url.txt
    """
    if not external:
        if via_file is not None:
            raise click.UsageError("--via-file is only available with browser sign-in.")
        if ctx.get_parameter_source("open_browser") == _CL:
            raise click.UsageError(
                "--open/--no-open is only available with browser sign-in."
            )

    dc = _get_client(ctx.obj["locale"])

    if external:
        dc.login_external(login_url_callback=_login_callback(via_file, open_browser))
    else:
        username = click.prompt("Audible email")
        password = click.prompt("Audible password", hide_input=True)
        dc.login(username, password)

    console.print(
        f"[green]Authenticated.[/green] Auth saved to {safe_markup(dc.auth_file)}"
    )


def _login_callback(via_file: Path | None, open_browser: bool):
    """Build the callback URL collector used by the external auth library."""

    def callback(oauth_url: str) -> str:
        click.echo()
        click.echo("Open this URL in your browser and sign in:")
        click.echo()
        click.echo(safe_text(oauth_url))
        click.echo()
        if open_browser:
            try:
                if click.launch(oauth_url) != 0:
                    click.echo("Could not open a browser; use the URL above.", err=True)
            except Exception:
                click.echo("Could not open a browser; use the URL above.", err=True)
        click.echo("A 'Page not found' page after sign-in is expected.")

        if via_file is not None:
            click.echo(
                f"Save the full callback URL to {safe_text(via_file)}, then return here."
            )
            click.prompt(
                "Press Enter once the file is saved", default="", show_default=False
            )
            try:
                file_stat = via_file.stat()
                if not stat.S_ISREG(file_stat.st_mode):
                    raise click.ClickException("Callback path must be a regular file.")
                if file_stat.st_size > 65_536:
                    raise click.ClickException("Callback file is unexpectedly large.")
                if file_stat.st_mode & (stat.S_IRGRP | stat.S_IROTH):
                    click.echo(
                        "Warning: callback file is readable by other users.", err=True
                    )
                callback_url = read_text(via_file).strip()
            except OSError as exc:
                raise click.ClickException(
                    f"Could not read callback file: {exc}"
                ) from exc
            except UnicodeError as exc:
                raise click.ClickException(
                    "Could not read callback file: expected UTF-8 text."
                ) from exc
            click.echo(
                "The callback file contains a sign-in code; delete it after this command finishes."
            )
        else:
            click.echo(
                "For security, the URL will not appear while you paste or type it."
            )
            callback_url = click.prompt(
                "Paste the full callback URL, then press Enter",
                default="",
                show_default=False,
                hide_input=True,
            ).strip()
        if not callback_url:
            raise click.ClickException(
                "No callback URL provided. Try 'deals login' again."
            )
        return _validate_callback_url(callback_url)

    return callback


def _validate_callback_url(callback_url: str) -> str:
    """Validate the redirect shape expected by the Audible login flow."""
    try:
        parsed = urlparse(callback_url)
        code = parse_qs(parsed.query).get("openid.oa2.authorization_code", [])
    except ValueError as exc:
        raise click.ClickException("The callback URL is not valid.") from exc
    if parsed.scheme != "https" or not parsed.netloc:
        raise click.ClickException("The callback URL must be a complete HTTPS URL.")
    if not code or not code[0].strip():
        raise click.ClickException(
            "The callback URL does not contain an Audible authorization code."
        )
    return callback_url


@click.command("import-auth")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def import_auth(ctx, path: Path):
    """Import auth from an audible-cli JSON file or Libation AccountsSettings.json."""
    dc = _get_client(ctx.obj["locale"])
    try:
        dc.import_auth(path)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise click.ClickException(f"Could not import auth: {exc}") from exc
    console.print(f"[green]Auth imported.[/green] Saved to {safe_markup(dc.auth_file)}")


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
            raise click.ClickException(str(e)) from e

    title = "Subcategories" if parent else "Top-Level Categories"
    display_categories(cats, title=title)
    console.print(
        "\n  [dim]Tip: use --parent ID to see subcategories, "
        "or pass the ID to 'deals find --category ID'[/dim]"
    )


@click.command()
@click.argument("asin", required=False, default=None, metavar="SELECTOR")
@click.option(
    "--last",
    "last_ref",
    type=str,
    default=None,
    help="Use result #N from the last result session",
)
@click.pass_context
def detail(ctx, asin, last_ref):
    """Show product details using an ASIN, Audible URL, or @N selector."""
    if not asin and last_ref is None:
        raise click.UsageError("Provide an ASIN, Audible URL, @N, or use --last N.")
    resolved, locale = _resolve_cli_selectors(
        ctx,
        (asin,) if asin else (),
        (last_ref,) if last_ref is not None else (),
        single=True,
    )
    asin = resolved[0].asin
    dc = _get_client(locale)
    with dc:
        try:
            product = dc.get_product(asin)
        except ValueError as e:
            raise click.ClickException(str(e)) from e

    display_product_detail(product, credit_price=_credit_price(ctx))


@click.command("open")
@click.argument("asin", required=False, default=None, metavar="SELECTOR")
@click.option(
    "--last",
    "last_ref",
    type=str,
    default=None,
    help="Use result #N from the last result session",
)
@click.pass_context
def open_cmd(ctx, asin, last_ref):
    """Open a product selected by ASIN, Audible URL, or @N."""
    if not asin and last_ref is None:
        raise click.UsageError("Provide an ASIN, Audible URL, @N, or use --last N.")
    resolved, locale = _resolve_cli_selectors(
        ctx,
        (asin,) if asin else (),
        (last_ref,) if last_ref is not None else (),
        single=True,
    )
    asin = resolved[0].asin
    url = product_url(asin, locale)
    console.print(f"[dim]Opening {safe_markup(url)}[/dim]")
    click.launch(url)


@click.command()
@click.argument("asins", nargs=-1, required=False, metavar="SELECTOR...")
@click.option(
    "--last",
    "last_refs",
    type=str,
    multiple=True,
    help="Use result #N from the last result session (repeatable)",
)
@click.pass_context
def compare(ctx, asins, last_refs):
    """Compare multiple products side-by-side.

    \b
    Example:
        deals compare B00R6S1RCY B00I2VWW5U B019NMZ6FE
        deals compare @1 @3
        deals compare --last 1 --last 3
    """
    if len(asins) == 1 and not last_refs and not asins[0].startswith("@"):
        raise click.UsageError("Provide at least 2 ASINs to compare.")
    resolved, locale = _resolve_cli_selectors(ctx, asins, last_refs)
    all_asins = [item.asin for item in resolved]

    if len(all_asins) < 2:
        raise click.UsageError("Provide at least 2 ASINs to compare.")

    dc = _get_client(locale)
    with dc:
        products = dc.get_products_batch(all_asins)

    found_asins = {p.asin for p in products}
    for asin in all_asins:
        if asin not in found_asins:
            console.print(f"[red]Not found: {safe_markup(asin)}[/red]")

    if len(products) < 2:
        raise click.ClickException("Need at least 2 valid products to compare.")

    # Preserve the order the user specified
    asin_order = {asin: i for i, asin in enumerate(all_asins)}
    products.sort(key=lambda p: asin_order.get(p.asin, 999))

    display_comparison(products, credit_price=_credit_price(ctx))


_Row = tuple[str, str, str]  # (check, status token PASS/WARN/FAIL, detail)


def _auth_checks() -> tuple[list[_Row], bool]:
    """Check config dir, auth file presence/parseability, and token expiry."""
    rows: list[_Row] = []
    directory, why = paths.data_dir_with_reason()
    if directory.exists():
        rows.append(("Config directory", "PASS", str(directory)))
    else:
        rows.append(
            (
                "Config directory",
                "WARN",
                f"Not found — will be created at {directory}",
            )
        )
    rows.append(("Data location", "PASS", why))

    inspection = inspect_auth_file()
    auth_ok = inspection.is_usable
    if inspection.status == "missing":
        rows.append(
            ("Auth file present", "FAIL", "Run 'deals login' or 'deals import-auth'")
        )
    else:
        rows.append(("Auth file present", "PASS", str(paths.auth_file())))

    if inspection.status == "malformed":
        rows.append(("Auth file parseable", "FAIL", inspection.error))
    elif inspection.status != "missing":
        rows.append(("Auth file parseable", "PASS", ""))

    if inspection.status == "expired":
        rows.append(
            (
                "Auth token expiry",
                "WARN",
                "Access token expired — automatic refresh will be attempted",
            )
        )
    elif inspection.status == "expiring":
        rows.append(
            (
                "Auth token expiry",
                "WARN",
                "Token expires within 24h — consider refreshing",
            )
        )
    elif inspection.status == "valid":
        rows.append(("Auth token expiry", "PASS", ""))
    elif inspection.status == "unknown_expiry":
        rows.append(("Auth token expiry", "WARN", "Token freshness unknown"))

    return rows, auth_ok


def _connectivity_check(ctx, auth_ok: bool) -> _Row:
    """Probe the marketplace API (skipped when auth checks failed)."""
    if not auth_ok:
        return ("Marketplace reachable", "WARN", "Skipped — auth checks failed")
    try:
        dc = _get_client(ctx.obj["locale"])
        with dc:
            dc.check_connection()
        return ("Marketplace reachable", "PASS", "")
    except Exception as e:
        return ("Marketplace reachable", "FAIL", f"{type(e).__name__}: {e}")


def _config_checks() -> list[_Row]:
    """Validate the config file schema and profile option keys."""
    rows: list[_Row] = []
    if paths.config_file().exists():
        try:
            cfg = json_mod.loads(read_text(paths.config_file()))
            if not isinstance(cfg, dict):
                rows.append(
                    (
                        "Config file valid",
                        "FAIL",
                        f"Expected a JSON object, got {type(cfg).__name__}",
                    )
                )
                rows.append(
                    ("Unknown config keys", "WARN", "Skipped — config file unparseable")
                )
            else:
                errors = [
                    f"{k}: expected {CONFIG_SCHEMA[k].__name__}, got {type(v).__name__}"
                    for k, v in cfg.items()
                    if k in CONFIG_SCHEMA and not isinstance(v, CONFIG_SCHEMA[k])
                ]
                errors.extend(config_numeric_errors(cfg))
                if all(
                    key not in cfg or type(cfg[key]) is bool
                    for key in ("skip_plus", "only_plus")
                ):
                    try:
                        resolve_plus_flags(
                            cfg.get("skip_plus", False),
                            cfg.get("only_plus", False),
                        )
                    except ValueError as exc:
                        errors.append(str(exc))
                if errors:
                    rows.append(("Config file valid", "FAIL", "; ".join(errors)))
                else:
                    rows.append(("Config file valid", "PASS", ""))
                unknown = sorted(k for k in cfg if k not in CONFIG_SCHEMA)
                if unknown:
                    rows.append(("Unknown config keys", "WARN", ", ".join(unknown)))
                else:
                    rows.append(("Unknown config keys", "PASS", ""))
        except Exception as e:
            rows.append(("Config file valid", "FAIL", str(e)))
            rows.append(
                ("Unknown config keys", "WARN", "Skipped — config file unparseable")
            )
    else:
        rows.append(("Config file valid", "PASS", "No config file (using defaults)"))

    if paths.profiles_file().exists():
        try:
            profiles = _load_raw_json(paths.profiles_file(), dict)
            valid_profile_keys = set(CONFIG_SCHEMA) | set(_PROFILE_EXTRA_KEYS)
            bad_profiles: dict[str, list[str]] = {}
            invalid_profiles: dict[str, str] = {}
            for pname, popts in profiles.items():
                if not isinstance(popts, dict):
                    invalid_profiles[pname] = "expected an object"
                    continue
                bad = sorted(k for k in popts if k not in valid_profile_keys)
                if bad:
                    bad_profiles[pname] = bad
                if error := profile_validation_error(popts):
                    invalid_profiles[pname] = error
            if bad_profiles:
                detail = "; ".join(
                    f"{n}: {', '.join(ks)}" for n, ks in sorted(bad_profiles.items())
                )
                rows.append(("Unknown profile keys", "WARN", detail))
            else:
                rows.append(("Unknown profile keys", "PASS", ""))
            if invalid_profiles:
                detail = "; ".join(
                    f"{name}: {error}"
                    for name, error in sorted(invalid_profiles.items())
                )
                rows.append(("Profile settings valid", "FAIL", detail))
            else:
                rows.append(("Profile settings valid", "PASS", ""))
        except Exception as e:
            rows.append(("Profiles parseable", "FAIL", str(e)))
            rows.append(("Unknown profile keys", "WARN", str(e)))

    return rows


def _database_checks() -> list[_Row]:
    """Health of state.db and cache.db and of the one-time import of the old JSON files."""
    rows: list[_Row] = []
    try:
        conn = db.state()
    except Exception as exc:
        return [("State database", "FAIL", str(exc))]
    try:
        verdict = db.quick_check(conn)
    except Exception as exc:
        verdict = [str(exc)]
    size = db.file_size(paths.state_db_file())
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if verdict == ["ok"]:
        rows.append(
            (
                "State database",
                "PASS",
                f"schema {version}, {size:,} bytes, integrity check ok",
            )
        )
    else:
        rows.append(
            (
                "State database",
                "FAIL",
                f"integrity check failed: {verdict[0]} — move {paths.state_db_file()} "
                "aside and run any command to start fresh (old JSON files are kept as "
                "*.migrated if you need them)",
            )
        )
    imported = json_import.summary(conn)
    if imported is None:
        rows.append(("JSON migration", "WARN", "Not run yet"))
    elif imported["renames_pending"]:
        rows.append(
            (
                "JSON migration",
                "WARN",
                "Data imported, but these old files could not be renamed yet: "
                + ", ".join(imported["renames_pending"]),
            )
        )
    elif not imported["sources"]:
        rows.append(("JSON migration", "PASS", "Nothing to migrate"))
    else:
        counts = imported["counts"]
        detail = (
            f"Imported {imported['at']} "
            f"({counts.get('history_entries', 0)} price entries for "
            f"{counts.get('history_asins', 0)} ASIN(s)); originals kept as *.migrated"
        )
        skipped = sum(imported["skipped"].values())
        if skipped or imported["notes"]:
            rows.append(
                (
                    "JSON migration",
                    "WARN",
                    f"{detail}; skipped {skipped} unusable item(s)"
                    + "".join(f"; {note}" for note in imported["notes"]),
                )
            )
        else:
            rows.append(("JSON migration", "PASS", detail))
    try:
        cache_conn = db.cache()
        cache_version = cache_conn.execute("PRAGMA user_version").fetchone()[0]
        rows.append(
            (
                "Cache database",
                "PASS",
                f"schema {cache_version}, {db.file_size(paths.cache_db_file()):,} bytes "
                "(disposable; 'deals cache clear' empties it)",
            )
        )
    except Exception as exc:
        rows.append(("Cache database", "WARN", str(exc)))
    return rows


def _store_checks() -> list[_Row]:
    """Check notify-state health and that local state files are parseable."""
    rows: list[_Row] = []
    try:
        ns = load_notify_state()
        today = datetime.date.today()
        stale = 0
        for entry in ns.values():
            try:
                if abs((today - datetime.date.fromisoformat(entry["date"])).days) > 365:
                    stale += 1
            except (KeyError, ValueError, TypeError):
                stale += 1
        if stale:
            rows.append(("Notify-state health", "WARN", f"{stale} stale (>365 days)"))
        elif ns:
            rows.append(
                ("Notify-state health", "PASS", f"{len(ns)} suppressed ASIN(s) tracked")
            )
        else:
            rows.append(("Notify-state health", "PASS", "No entries"))
    except Exception as e:
        rows.append(("Notify-state health", "WARN", str(e)))

    if not paths.wishlist_file().exists():
        rows.append(("Wishlist health", "PASS", "No entries"))
    else:
        try:
            raw_wishlist = json_mod.loads(read_text(paths.wishlist_file()))
        except Exception as e:
            rows.append(("Wishlist health", "FAIL", str(e)))
        else:
            if not isinstance(raw_wishlist, list):
                rows.append(
                    (
                        "Wishlist health",
                        "FAIL",
                        f"Expected a list, got {type(raw_wishlist).__name__}",
                    )
                )
            else:
                inspection = inspect_wishlist(raw_wishlist)
                if inspection.issues:
                    shown = "; ".join(
                        f"[{issue.index}] {issue.reason}"
                        for issue in inspection.issues[:5]
                    )
                    remaining = len(inspection.issues) - 5
                    if remaining > 0:
                        shown += f"; +{remaining} more"
                    shown += "; run 'deals wishlist repair --dry-run'"
                    rows.append(("Wishlist health", "WARN", shown))
                else:
                    count = len(inspection.asin_items) + len(inspection.author_items)
                    rows.append(("Wishlist health", "PASS", f"{count} entries"))

    try:
        counts = db.table_counts(db.state(), ("dismissed", "seen"))
    except Exception as exc:
        rows.append(("Dismissed-ASINs state", "FAIL", str(exc)))
        rows.append(("Seen-ASINs state", "FAIL", str(exc)))
    else:
        dismissed = counts["dismissed"]
        rows.append(
            (
                "Dismissed-ASINs state",
                "PASS",
                f"{dismissed} ASIN(s) dismissed" if dismissed else "No entries",
            )
        )
        rows.append(
            (
                "Seen-ASINs state",
                "PASS",
                f"{counts['seen']} ASIN(s) seen" if counts["seen"] else "No entries",
            )
        )

    for check, path, expected_type in (
        ("Profiles parseable", paths.profiles_file(), dict),
        ("Monitors parseable", paths.monitors_file(), dict),
    ):
        if not path.exists():
            rows.append((check, "PASS", "No entries"))
            continue
        try:
            data = _load_raw_json(path, expected_type)
            malformed_entries: list[str]
            if path == paths.profiles_file():
                malformed_entries = [
                    name for name, value in data.items() if not isinstance(value, dict)
                ]
            else:
                malformed_entries = []
                for name, value in data.items():
                    try:
                        validated_monitor_definition(value)
                    except MonitorServiceError:
                        malformed_entries.append(name)
            if malformed_entries:
                rows.append(
                    (
                        check,
                        "FAIL",
                        "Malformed entries: " + ", ".join(map(str, malformed_entries)),
                    )
                )
            else:
                rows.append((check, "PASS", ""))
        except Exception as e:
            rows.append((check, "FAIL", str(e)))

    try:
        history = (
            db.state()
            .execute("SELECT COUNT(*), COUNT(DISTINCT asin) FROM price_history")
            .fetchone()
        )
        if history[0]:
            rows.append(
                (
                    "Price history",
                    "PASS",
                    f"{history[1]} ASIN(s) tracked, {history[0]} price entries",
                )
            )
        else:
            rows.append(("Price history", "PASS", "Not recorded yet (optional)"))
    except Exception as exc:
        rows.append(("Price history", "FAIL", str(exc)))

    return rows


def _track_checks() -> list[_Row]:
    """Check background-tracking schedule health."""
    try:
        install_info = load_install_record()
        runs = load_track_state().runs
    except Exception as exc:
        return [("Background tracking state", "FAIL", str(exc))]
    if not install_info:
        return [
            (
                "Background tracking",
                "PASS",
                "Not installed (optional — 'deals track install')",
            )
        ]

    rows: list[_Row] = []
    try:
        from audible_deals import scheduler

        present, where = scheduler.installed()
        if present:
            rows.append(
                (
                    "Background tracking",
                    "PASS",
                    f"every {install_info.get('every', '?')} via {install_info.get('method', '?')}",
                )
            )
        else:
            rows.append(
                (
                    "Background tracking",
                    "WARN",
                    f"Install record exists but schedule missing at {where}",
                )
            )
    except Exception as e:
        rows.append(("Background tracking", "WARN", str(e)))

    last = runs[0] if runs else None
    if not last:
        rows.append(("Last tracked run", "WARN", "Never ran — check 'deals track log'"))
    elif last.get("error"):
        streak = 0
        for r in runs:
            if r.get("error"):
                streak += 1
            else:
                break
        if streak >= 3:
            rows.append(
                (
                    "Last tracked run",
                    "FAIL",
                    f"Failing for {streak} consecutive runs (latest {last.get('at')}: {last['error']})",
                )
            )
        else:
            rows.append(
                ("Last tracked run", "FAIL", f"{last.get('at')}: {last['error']}")
            )
    else:
        detail = f"{last.get('at')} ({last.get('hits', 0)} at target)"
        try:
            ran_at = datetime.datetime.fromisoformat(last["at"])
            interval = float(install_info.get("interval_s", 0)) or 21600.0
            age = (datetime.datetime.now() - ran_at).total_seconds()
            if age > 2 * interval:
                rows.append(
                    (
                        "Last tracked run",
                        "WARN",
                        f"Stale — last ran {last.get('at')} (expected every {install_info.get('every', '?')})",
                    )
                )
                return rows
        except (KeyError, ValueError, TypeError):
            pass
        rows.append(("Last tracked run", "PASS", detail))
    return rows


def _monitor_checks() -> list[_Row]:
    """Report saved-search monitor health without reading webhook configuration."""
    monitors = load_monitors()
    if not monitors:
        return [("Saved-search monitors", "PASS", "No monitors configured")]
    try:
        state = monitor_summaries()
    except Exception:
        state = {}  # the "State database" row already reports why
    invalid: dict[str, str] = {}
    for name, definition in monitors.items():
        try:
            validated_monitor_definition(definition)
        except MonitorServiceError as exc:
            invalid[name] = str(exc)
    if invalid:
        detail = "; ".join(
            f"{name}: {error}" for name, error in sorted(invalid.items())
        )
        return [("Saved-search monitors", "FAIL", detail)]
    enabled = [
        name
        for name, definition in monitors.items()
        if isinstance(definition, dict) and definition.get("enabled", True)
    ]
    failed = [name for name in enabled if name in state and state[name].last_error]
    if failed:
        return [
            (
                "Saved-search monitors",
                "WARN",
                f"{len(failed)} enabled monitor(s) failed: {', '.join(failed)}",
            )
        ]
    return [
        (
            "Saved-search monitors",
            "PASS",
            f"{len(enabled)} enabled, {len(monitors) - len(enabled)} paused",
        )
    ]


def _render_doctor_rows(rows: list[_Row]) -> int:
    """Render the doctor table. Returns the number of FAIL rows."""
    failures = 0
    table = Table(title="deals doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", style="dim")
    for check, status, detail in rows:
        if status == "FAIL":
            failures += 1
            rendered = "[bold red]✗ FAIL[/bold red]"
        elif status == "WARN":
            rendered = "[yellow]⚠ WARN[/yellow]"
        else:
            rendered = "[green]✓ PASS[/green]"
        table.add_row(safe_markup(check), rendered, safe_markup(detail))
    console.print(table)
    return failures


@click.command()
@click.pass_context
def doctor(ctx):
    """Diagnostic checks for auth, config, and marketplace reachability."""
    rows, auth_ok = _auth_checks()
    rows.append(_connectivity_check(ctx, auth_ok))
    rows.extend(_config_checks())
    rows.extend(_database_checks())
    rows.extend(_store_checks())
    rows.extend(_track_checks())
    rows.extend(_monitor_checks())
    if _render_doctor_rows(rows):
        ctx.exit(1)


@click.command("completions")
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
@click.pass_context
def completions(ctx, shell):
    """Generate shell completion script.

    \b
    Install completions:
        deals completions bash >> ~/.bashrc
        deals completions zsh >> ~/.zshrc
        deals completions fish > ~/.config/fish/completions/deals.fish
    """
    from click.shell_completion import get_completion_class

    completion_class = get_completion_class(shell)
    if completion_class is None:
        raise click.ClickException(f"Unsupported shell: {shell}")
    completion = completion_class(
        ctx.find_root().command,
        {},
        "deals",
        "_DEALS_COMPLETE",
    )
    click.echo(completion.source())
