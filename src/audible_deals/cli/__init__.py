"""The ``deals`` command line.

This module holds only what every command shares; each command lives in its own
submodule and is imported the first time it is used, so ``deals --help`` and
``deals --version`` load none of them.

* ``_COMMAND_SPECS`` is the registry: command name -> (module, attribute, short
  help, deprecated). ``deals --help`` is generated from it, grouped by
  ``_COMMAND_GROUPS``.
* ``DealsGroup`` is the root group. It loads a command on demand, attaches the
  options that are accepted before or after any subcommand (``--locale``,
  ``-v``, ``--color``; see ``global_options``), remembers the raw arguments (so
  an error can still honor ``--json``), and turns every failure into a one-line
  error with a documented exit status (``errors``, ``exit_codes``).
* ``cli`` (below) is the group object itself: before any subcommand runs it
  sets up logging, loads the saved config and settles the marketplace. A bare
  ``deals`` prints a local-only starting dashboard.

Modules: ``catalog`` (find, search), ``foryou``, ``library``, ``series``,
``last``, ``wishlist`` (also ``watch``), ``history``, ``recap``, ``notify``,
``track``, ``monitor``, ``config`` (also ``profile``), ``cache`` and ``misc``
(login, detail, compare, categories, doctor, completions, ...). ``options``
declares the shared option sets, ``helpers`` the pieces most commands need
(client, selectors, output), and ``json_output`` (one level up) the
machine-readable payloads.
"""

from __future__ import annotations

import importlib
import sys

try:
    import readline  # noqa: F401 — required on macOS for input() with long strings
except ImportError:
    pass  # unavailable on Windows

import click

#: ``ctx.meta`` key holding the arguments the root group was invoked with.
RAW_ARGS_KEY = "deals.raw_args"

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
    "cache": (
        "audible_deals.cli.cache",
        "cache",
        "Inspect and clear local databases and caches.",
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
        (
            "login",
            "import-auth",
            "profile",
            "config",
            "cache",
            "doctor",
            "completions",
        ),
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


class DealsGroup(click.Group):
    """The root group: lazy command loading and one-line, coded error handling."""

    def get_command(self, ctx, cmd_name):
        command = super().get_command(ctx, cmd_name)
        if command is not None:
            return command
        spec = _COMMAND_SPECS.get(cmd_name)
        if spec is None:
            return None
        module_name, attribute, _, _ = spec
        command = getattr(importlib.import_module(module_name), attribute)
        from audible_deals.cli.global_options import attach_global_options

        # --locale / -v are accepted after the subcommand as well as before it.
        attach_global_options(command)
        self.add_command(command, cmd_name)
        return command

    def list_commands(self, ctx):
        return sorted({*super().list_commands(ctx), *_COMMAND_SPECS})

    def parse_args(self, ctx, args):
        # Remember what was typed: a usage error raised while a subcommand's
        # arguments are still being parsed must still honor --json.
        ctx.meta[RAW_ARGS_KEY] = tuple(args)
        return super().parse_args(ctx, args)

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except (click.exceptions.Exit, click.Abort):
            raise
        except KeyboardInterrupt:
            from audible_deals.cli import errors
            from audible_deals.exit_codes import ExitCode

            if errors.wants_json(ctx):
                errors.CliError(
                    "Interrupted", ExitCode.INTERRUPTED, json_mode=True
                ).show()
            else:
                click.echo("\nAborted!", err=True)
            raise click.exceptions.Exit(ExitCode.INTERRUPTED) from None
        except Exception as exc:
            from audible_deals.cli import errors

            converted = errors.convert(exc, ctx)
            if converted is not None:
                raise converted from exc
            if isinstance(exc, (click.ClickException, BrokenPipeError)):
                raise
            if ctx.obj and ctx.obj.get("verbose", 0) >= 2:
                raise  # -vv: the traceback is wanted
            raise errors.unexpected(exc, ctx) from exc

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


def _print_version(ctx: click.Context, param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    try:
        from importlib.metadata import version as _pkg_version

        v = _pkg_version("audible-deals")
    except Exception:
        v = "0.11.1"  # fallback for PyInstaller frozen builds
    click.echo(f"deals, version {v}")
    ctx.exit()


def _apply_root_color(ctx, param, value):
    """``--color`` before the subcommand; the shared callback does the work."""
    if value is None:
        return value
    from audible_deals.cli.global_options import _apply_color

    return _apply_color(ctx, param, value)


@click.group(cls=DealsGroup, invoke_without_command=True)
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
    "--color",
    type=click.Choice(["auto", "always", "never"]),
    default=None,
    expose_value=False,
    callback=_apply_root_color,
    metavar="WHEN",
    help=(
        "Colored output: auto (default; honors NO_COLOR and FORCE_COLOR), "
        "always or never. An explicit value wins over those variables."
    ),
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
    import logging

    from audible_deals.config_store import load_config
    from audible_deals.constants import LOCALE_DOMAIN

    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    if ctx.invoked_subcommand is not None:
        # Configure logging before reading any state so load-time warnings
        # (e.g. a corrupt config file) honor -v/-vv.
        from audible_deals.logging_setup import configure_logging

        configure_logging(verbose)
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
