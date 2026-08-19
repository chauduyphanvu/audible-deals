"""CLI for finding Audible audiobook deals.

Usage:
    deals login                    Authenticate with Audible
    deals import-auth PATH         Import auth from audible-cli or Libation
    deals categories [--parent ID] List categories
    deals search QUERY [options]   Search catalog with filters
    deals find [options]           Browse & filter deals (main command)
    deals detail ASIN              Show detailed product info
    deals open ASIN                Open Audible page in browser
    deals compare ASIN ASIN ...    Side-by-side comparison
    deals wishlist add/remove/list/sync Manage your watchlist
    deals watch                    Check wishlist for price drops
    deals notify [--webhook URL]   Send notifications for deals at target
    deals profile save/list/delete Manage saved search profiles
    deals monitor add/list/show/remove Manage saved-search monitors
    deals history ASIN             View price history with sparkline
    deals recap [--days N]         Recap of recent price changes
    deals completions SHELL        Generate shell completions
"""

from __future__ import annotations

import logging
import sys

try:
    import readline  # noqa: F401 — required on macOS for input() with long strings
except ImportError:
    pass  # unavailable on Windows

import click
from audible.exceptions import RequestError

from audible_deals.auth_state import inspect_auth_file
from audible_deals.cli import catalog as catalog_commands
from audible_deals.cli import config as config_commands
from audible_deals.cli import foryou as foryou_commands
from audible_deals.cli import history as history_commands
from audible_deals.cli import last as last_commands
from audible_deals.cli import library as library_commands
from audible_deals.cli import misc as misc_commands
from audible_deals.cli import monitor as monitor_commands
from audible_deals.cli import notify as notify_commands
from audible_deals.cli import recap as recap_commands
from audible_deals.cli import series as series_commands
from audible_deals.cli import track as track_commands
from audible_deals.cli import wishlist as wishlist_commands
from audible_deals.cli.helpers import _CL
from audible_deals.config_store import load_config
from audible_deals.constants import LOCALE_DOMAIN
from audible_deals.logging_setup import configure_logging

logger = logging.getLogger(__name__)


def _force_utf8_output() -> None:
    """Reconfigure the standard streams to UTF-8 on Windows.

    Windows consoles often default to a legacy code page (e.g. cp1252) that
    cannot encode the Unicode glyphs Rich renders, which otherwise crashes the
    CLI with UnicodeEncodeError. reconfigure() mutates the existing stream in
    place; guarded because captured/redirected/frozen streams may not support it.
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_force_utf8_output()


class _HandleAuthErrors(click.Group):
    """Catch RuntimeError from missing auth and show a friendly message."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except RuntimeError as e:
            if "Not authenticated" in str(e):
                raise click.ClickException(str(e))
            raise
        except RequestError:
            raise click.ClickException(
                "Audible request failed. Check your network connection and authentication, then try again."
            )
        except BrokenPipeError:
            raise
        except OSError as e:
            raise click.ClickException(f"Filesystem error: {e}")

    def format_commands(self, ctx, formatter):
        """Render command help in workflows rather than one long alphabetic list."""
        groups = (
            ("Discover", ("find", "search", "for-me", "series", "categories")),
            (
                "Library & Results",
                ("library", "last", "detail", "compare", "open"),
            ),
            (
                "Watch & Automate",
                ("wishlist", "watch", "history", "recap", "notify", "monitor", "track"),
            ),
            (
                "Setup & Support",
                ("login", "import-auth", "profile", "config", "doctor", "completions"),
            ),
        )
        classified = {name for _, names in groups for name in names}

        for heading, names in (*groups, ("Other", tuple(self.list_commands(ctx)))):
            rows = []
            for name in names:
                if heading == "Other" and name in classified:
                    continue
                command = self.get_command(ctx, name)
                if command is not None and not command.hidden:
                    rows.append((name, command.get_short_help_str(limit=10_000)))
            if rows:
                with formatter.section(heading):
                    formatter.write_dl(rows)


def _print_version(ctx: click.Context, param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    try:
        from importlib.metadata import version as _pkg_version

        v = _pkg_version("audible-deals")
    except Exception:
        v = "0.10.0"  # fallback for PyInstaller frozen builds
    click.echo(f"deals, version {v}")
    ctx.exit()


@click.group(cls=_HandleAuthErrors, invoke_without_command=True)
@click.option(
    "--version",
    is_flag=True,
    expose_value=False,
    is_eager=True,
    callback=_print_version,
    help="Show the version and exit.",
)
@click.option(
    "--locale",
    default="us",
    help="Audible marketplace (us, uk, ca, de, fr, au, jp, in, es)",
)
@click.option(
    "-v",
    "--verbose",
    "verbose",
    count=True,
    help="Enable debug logging (-v for INFO, -vv for DEBUG). DEALS_DEBUG=1 also enables it.",
)
@click.pass_context
def cli(ctx, locale, verbose):
    """Audible deal finder - find cheap audiobooks during sales."""
    ctx.ensure_object(dict)
    cfg = load_config()
    ctx.obj["config"] = cfg
    if ctx.get_parameter_source("locale") != _CL:
        cfg_locale = cfg.get("locale")
        if cfg_locale:
            locale = cfg_locale
    if locale not in LOCALE_DOMAIN:
        raise click.BadParameter(
            f"Invalid locale {locale!r}. Valid: {', '.join(sorted(LOCALE_DOMAIN))}",
            param_hint="--locale",
        )
    ctx.obj["locale"] = locale
    if ctx.invoked_subcommand is None:
        _print_dashboard(locale)
        return
    configure_logging(verbose)
    logger.debug("cli start locale=%s subcommand=%s", locale, ctx.invoked_subcommand)


def _print_dashboard(locale: str) -> None:
    """Print a useful, local-only starting point for a bare invocation."""
    inspection = inspect_auth_file()
    click.echo(f"audible-deals · marketplace: {locale}")

    if inspection.status in {"missing", "malformed", "expired"}:
        if inspection.status == "missing":
            click.echo("Authentication is not set up.")
        elif inspection.status == "expired":
            click.echo("Authentication has expired.")
        else:
            click.echo("Saved authentication cannot be read.")
        click.echo("Start with: deals login")
        click.echo("Or import existing credentials: deals import-auth PATH")
        click.echo("Diagnose setup: deals doctor")
    else:
        click.echo("Authentication is available.")
        if inspection.status == "expiring":
            click.echo(
                "Warning: authentication expires within 24 hours; run deals login to refresh it."
            )
        elif inspection.status == "unknown_expiry":
            click.echo(
                "Warning: authentication expiry is unknown; run deals login if requests fail."
            )
        click.echo("Try: deals find --genre sci-fi --max-price 5")
        click.echo('     deals search "Brandon Sanderson"')
        click.echo("     deals for-me --max-price 5")
        click.echo("     deals wishlist")
        click.echo("     deals track")
    click.echo("Run deals --help for the complete reference.")


cli.add_command(misc_commands.login)
cli.add_command(misc_commands.import_auth)
cli.add_command(misc_commands.categories)
cli.add_command(misc_commands.detail)
cli.add_command(misc_commands.open_cmd)
cli.add_command(misc_commands.compare)
cli.add_command(misc_commands.doctor)
cli.add_command(misc_commands.completions)
cli.add_command(catalog_commands.search)
cli.add_command(catalog_commands.find)
cli.add_command(foryou_commands.for_me)
cli.add_command(foryou_commands.for_you)
cli.add_command(library_commands.library)
cli.add_command(series_commands.series)
cli.add_command(last_commands.last_cmd)
cli.add_command(wishlist_commands.wishlist)
cli.add_command(wishlist_commands.watch)
cli.add_command(config_commands.config_cmd)
cli.add_command(config_commands.profile)
cli.add_command(history_commands.history)
cli.add_command(recap_commands.recap)
cli.add_command(notify_commands.notify)
cli.add_command(track_commands.track)
cli.add_command(monitor_commands.monitor)
