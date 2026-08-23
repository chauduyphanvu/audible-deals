"""CLI for finding Audible audiobook deals.

Usage:
    deals login                    Authenticate with Audible
    deals import-auth PATH         Import auth from audible-cli or Libation
    deals categories [--parent ID] List categories
    deals search QUERY [options]   Search catalog with filters
    deals find [options]           Browse & filter deals (main command)
    deals detail SELECTOR          Show detailed product info
    deals open SELECTOR            Open Audible page in browser
    deals compare SELECTOR ...     Side-by-side comparison
    deals wishlist add/remove/list/sync/repair Manage your watchlist
    deals watch                    Check wishlist for price drops
    deals notify [--webhook URL]   Send notifications for deals at target
    deals profile save/list/delete Manage saved search profiles
    deals monitor add/list/show/remove Manage saved-search monitors
    deals history SELECTOR         View price history with sparkline
    deals recap [--days N]         Recap of recent price changes
    deals completions SHELL        Generate shell completions
"""

from __future__ import annotations

import importlib
import sys

try:
    import readline  # noqa: F401 — required on macOS for input() with long strings
except ImportError:
    pass  # unavailable on Windows

import click


_COMMAND_SPECS = {
    "login": (
        "audible_deals.cli.misc",
        "login",
        "Authenticate with Audible.",
        False,
    ),
    "import-auth": (
        "audible_deals.cli.misc",
        "import_auth",
        "Import auth from an audible-cli JSON file or Libation AccountsSettings.json.",
        False,
    ),
    "categories": (
        "audible_deals.cli.misc",
        "categories",
        "List Audible categories.",
        False,
    ),
    "detail": (
        "audible_deals.cli.misc",
        "detail",
        "Show product details using an ASIN, Audible URL, or @N selector.",
        False,
    ),
    "open": (
        "audible_deals.cli.misc",
        "open_cmd",
        "Open a product selected by ASIN, Audible URL, or @N.",
        False,
    ),
    "compare": (
        "audible_deals.cli.misc",
        "compare",
        "Compare multiple products side-by-side.",
        False,
    ),
    "doctor": (
        "audible_deals.cli.misc",
        "doctor",
        "Diagnostic checks for auth, config, and marketplace reachability.",
        False,
    ),
    "completions": (
        "audible_deals.cli.misc",
        "completions",
        "Generate shell completion script.",
        False,
    ),
    "search": (
        "audible_deals.cli.catalog",
        "search",
        "Search the Audible catalog by keyword.",
        False,
    ),
    "find": (
        "audible_deals.cli.catalog",
        "find",
        "Find deals: browse the catalog filtered by price and genre.",
        False,
    ),
    "for-me": (
        "audible_deals.cli.foryou",
        "for_me",
        "Personalized deals from your own library's taste profile.",
        False,
    ),
    "for-you": (
        "audible_deals.cli.foryou",
        "for_you",
        "Deprecated alias for `deals for-me`.",
        True,
    ),
    "library": (
        "audible_deals.cli.library",
        "library",
        "List all audiobooks in your Audible library.",
        False,
    ),
    "series": (
        "audible_deals.cli.series",
        "series",
        "Find continuation books in series you're invested in.",
        False,
    ),
    "last": (
        "audible_deals.cli.last",
        "last_cmd",
        "Re-display and cumulatively refine the last result session without API calls.",
        False,
    ),
    "wishlist": (
        "audible_deals.cli.wishlist",
        "wishlist",
        "Manage your audiobook wishlist.",
        False,
    ),
    "watch": (
        "audible_deals.cli.wishlist",
        "watch",
        "Check wishlist prices and highlight deals.",
        False,
    ),
    "config": (
        "audible_deals.cli.config",
        "config_cmd",
        "Manage global defaults for deals commands.",
        False,
    ),
    "profile": (
        "audible_deals.cli.config",
        "profile",
        "Manage saved search profiles.",
        False,
    ),
    "history": (
        "audible_deals.cli.history",
        "history",
        "Show price history for an ASIN, Audible URL, or @N selector.",
        False,
    ),
    "recap": (
        "audible_deals.cli.recap",
        "recap",
        "Show a recap of price changes across tracked items.",
        False,
    ),
    "notify": (
        "audible_deals.cli.notify",
        "notify",
        "Check wishlist and send notifications for items at target price.",
        False,
    ),
    "track": (
        "audible_deals.cli.track",
        "track",
        "Background price tracking on an OS schedule.",
        False,
    ),
    "monitor": (
        "audible_deals.cli.monitor",
        "monitor",
        "Manage saved-search monitors run by deals track run.",
        False,
    ),
}

_COMMAND_GROUPS = (
    ("Discover", ("find", "search", "for-me", "series", "categories")),
    ("Library & Results", ("library", "last", "detail", "compare", "open")),
    (
        "Watch & Automate",
        ("wishlist", "watch", "history", "recap", "notify", "monitor", "track"),
    ),
    (
        "Setup & Support",
        ("login", "import-auth", "profile", "config", "doctor", "completions"),
    ),
)


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

    def get_command(self, ctx, cmd_name):
        command = super().get_command(ctx, cmd_name)
        if command is not None:
            return command
        spec = _COMMAND_SPECS.get(cmd_name)
        if spec is None:
            return None
        module_name, attribute, _, _ = spec
        command = getattr(importlib.import_module(module_name), attribute)
        self.add_command(command, cmd_name)
        return command

    def list_commands(self, ctx):
        return sorted({*super().list_commands(ctx), *_COMMAND_SPECS})

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except click.ClickException as exc:
            exc.message = _safe_text(exc.message)
            raise
        except RuntimeError as e:
            if "Not authenticated" in str(e):
                raise click.ClickException(_safe_text(e))
            raise
        except BrokenPipeError:
            raise
        except OSError as e:
            raise click.ClickException(f"Filesystem error: {_safe_text(e)}")
        except Exception as exc:
            if not _is_audible_request_error(exc):
                raise
            raise click.ClickException(
                "Audible request failed. Check your network connection and authentication, then try again."
            )

    def format_commands(self, ctx, formatter):
        """Render command help in workflows rather than one long alphabetic list."""
        classified = {name for _, names in _COMMAND_GROUPS for name in names}

        for heading, names in (
            *_COMMAND_GROUPS,
            ("Other", tuple(self.list_commands(ctx))),
        ):
            rows = []
            for name in names:
                if heading == "Other" and name in classified:
                    continue
                spec = _COMMAND_SPECS.get(name)
                if spec is not None:
                    _, _, short_help, hidden = spec
                    if not hidden:
                        rows.append((name, short_help))
                    continue
                command = super().get_command(ctx, name)
                if command is not None and not command.hidden:
                    rows.append((name, command.get_short_help_str(limit=10_000)))
            if rows:
                with formatter.section(heading):
                    formatter.write_dl(rows)


def _safe_text(value: object) -> str:
    from audible_deals.presentation.terminal import safe_text

    return safe_text(value)


def _is_audible_request_error(exc: Exception) -> bool:
    """Recognize SDK request errors without importing Audible during startup."""
    exceptions_module = sys.modules.get("audible.exceptions")
    request_error = getattr(exceptions_module, "RequestError", None)
    return isinstance(request_error, type) and isinstance(exc, request_error)


def _print_version(ctx: click.Context, param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    try:
        from importlib.metadata import version as _pkg_version

        v = _pkg_version("audible-deals")
    except Exception:
        v = "0.11.0"  # fallback for PyInstaller frozen builds
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
    from audible_deals.config_store import load_config
    from audible_deals.constants import LOCALE_DOMAIN

    ctx.ensure_object(dict)
    cfg = load_config()
    ctx.obj["config"] = cfg
    locale_explicit = (
        ctx.get_parameter_source("locale") == click.core.ParameterSource.COMMANDLINE
    )
    if not locale_explicit:
        cfg_locale = cfg.get("locale")
        if isinstance(cfg_locale, str) and cfg_locale in LOCALE_DOMAIN:
            locale = cfg_locale
        elif cfg_locale is not None:
            if ctx.invoked_subcommand not in {"config", "doctor"}:
                raise click.ClickException(
                    f"Stored locale {cfg_locale!r} is invalid. "
                    "Run 'deals config set locale us' or 'deals config reset locale'."
                )
    if locale not in LOCALE_DOMAIN:
        raise click.BadParameter(
            f"Invalid locale {locale!r}. Valid: {', '.join(sorted(LOCALE_DOMAIN))}",
            param_hint="--locale",
        )
    ctx.obj["locale"] = locale
    ctx.obj["locale_explicit"] = locale_explicit
    if ctx.invoked_subcommand is None:
        _print_dashboard(locale)
        return
    import logging

    from audible_deals.logging_setup import configure_logging

    configure_logging(verbose)
    logging.getLogger(__name__).debug(
        "cli start locale=%s subcommand=%s", locale, ctx.invoked_subcommand
    )


def _print_dashboard(locale: str) -> None:
    """Print a useful, local-only starting point for a bare invocation."""
    from audible_deals.auth_state import inspect_auth_file

    inspection = inspect_auth_file()
    click.echo(f"audible-deals · marketplace: {locale}")

    if inspection.status in {"missing", "malformed"}:
        if inspection.status == "missing":
            click.echo("Authentication is not set up.")
        else:
            click.echo("Saved authentication cannot be read.")
        click.echo("Start with: deals login")
        click.echo("Or import existing credentials: deals import-auth PATH")
        click.echo("Diagnose setup: deals doctor")
    else:
        click.echo("Authentication is available.")
        if inspection.status == "expired":
            click.echo("The access token has expired and will refresh automatically.")
        elif inspection.status == "expiring":
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
