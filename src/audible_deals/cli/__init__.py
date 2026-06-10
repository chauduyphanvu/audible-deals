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
    deals history ASIN             View price history with sparkline
    deals recap [--days N]         Recap of recent price changes
    deals completions SHELL        Generate shell completions
"""

from __future__ import annotations

import logging

try:
    import readline  # noqa: F401 — required on macOS for input() with long strings
except ImportError:
    pass  # unavailable on Windows

import click

from audible_deals.cli import config as config_commands
from audible_deals.cli import misc as misc_commands
from audible_deals.cli import notify as notify_commands
from audible_deals.cli import scan as scan_commands
from audible_deals.cli import track as track_commands
from audible_deals.cli import wishlist as wishlist_commands
from audible_deals.cli.helpers import _CL
from audible_deals.config_store import load_config
from audible_deals.display import console
from audible_deals.logging_setup import configure_logging

logger = logging.getLogger(__name__)


class _HandleAuthErrors(click.Group):
    """Catch RuntimeError from missing auth and show a friendly message."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except RuntimeError as e:
            if "Not authenticated" in str(e):
                raise click.ClickException(str(e))
            raise


def _print_version(ctx: click.Context, param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    try:
        from importlib.metadata import version as _pkg_version

        v = _pkg_version("audible-deals")
    except Exception:
        v = "0.7.0"  # fallback for PyInstaller frozen builds
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
    configure_logging(verbose)
    ctx.ensure_object(dict)
    cfg = load_config()
    ctx.obj["config"] = cfg
    if ctx.get_parameter_source("locale") != _CL:
        cfg_locale = cfg.get("locale")
        if cfg_locale:
            locale = cfg_locale
    ctx.obj["locale"] = locale
    logger.debug("cli start locale=%s subcommand=%s", locale, ctx.invoked_subcommand)
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        console.print(
            "\n  [dim]Quick start: deals find --genre sci-fi --max-price 5[/dim]"
        )


cli.add_command(misc_commands.login)
cli.add_command(misc_commands.import_auth)
cli.add_command(misc_commands.categories)
cli.add_command(misc_commands.detail)
cli.add_command(misc_commands.open_cmd)
cli.add_command(misc_commands.compare)
cli.add_command(misc_commands.doctor)
cli.add_command(misc_commands.completions)
cli.add_command(scan_commands.search)
cli.add_command(scan_commands.find)
cli.add_command(scan_commands.library)
cli.add_command(scan_commands.series)
cli.add_command(scan_commands.last_cmd)
cli.add_command(wishlist_commands.wishlist)
cli.add_command(wishlist_commands.watch)
cli.add_command(config_commands.config_cmd)
cli.add_command(config_commands.profile)
cli.add_command(notify_commands.history)
cli.add_command(notify_commands.recap)
cli.add_command(notify_commands.notify)
cli.add_command(track_commands.track)
