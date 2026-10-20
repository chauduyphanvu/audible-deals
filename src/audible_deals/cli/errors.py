"""How a failure becomes one line of text (or one JSON object) and an exit status.

The root group converts every exception that escapes a command with
:func:`convert`; commands that know better raise :class:`CliError` themselves.
A traceback appears only with ``-vv``.
"""

from __future__ import annotations

import json
import sys
from typing import IO

import click

from audible_deals.exit_codes import ExitCode, exit_code_for


class CliError(click.ClickException):
    """A failure with a documented exit status.

    With ``json_mode`` the error is written to stderr as
    ``{"error": {"code": N, "message": "..."}}`` instead of ``Error: ...``.
    """

    def __init__(
        self,
        message: str,
        code: ExitCode | int = ExitCode.ERROR,
        *,
        json_mode: bool = False,
    ):
        super().__init__(message)
        self.exit_code = int(code)
        self.json_mode = json_mode

    def show(self, file: IO[str] | None = None) -> None:
        if not self.json_mode:
            super().show(file)
            return
        payload = {"error": {"code": self.exit_code, "message": self.message}}
        click.echo(
            json.dumps(payload, ensure_ascii=False, allow_nan=False),
            file=file or sys.stderr,
        )


def wants_json(ctx: click.Context) -> bool:
    """Whether this invocation asked for ``--json`` output."""
    from audible_deals.cli import RAW_ARGS_KEY

    args = ctx.meta.get(RAW_ARGS_KEY) or ()
    return "--json" in args


def message_for(exc: BaseException, code: ExitCode) -> str:
    """The one-line text for an authentication or request failure."""
    if code is ExitCode.AUTH:
        text = str(exc)
        if "deals login" in text:
            return text
        return "Audible rejected the saved login. Run 'deals login' to sign in again."
    return (
        "Audible request failed. Check your network connection and "
        "authentication, then try again."
    )


def convert(exc: Exception, ctx: click.Context) -> CliError | None:
    """The :class:`CliError` for an exception, or ``None`` to let it propagate."""
    from audible_deals.presentation.terminal import safe_text

    json_mode = wants_json(ctx)
    if isinstance(exc, CliError):
        exc.json_mode = exc.json_mode or json_mode
        exc.message = safe_text(exc.message, keep_newlines=True)
        return exc
    if isinstance(exc, click.UsageError):
        if not json_mode:
            exc.message = safe_text(exc.message, keep_newlines=True)
            return None
        return CliError(
            safe_text(exc.format_message(), keep_newlines=True),
            ExitCode.USAGE,
            json_mode=True,
        )
    if isinstance(exc, click.ClickException):
        return CliError(
            safe_text(exc.message, keep_newlines=True),
            exc.exit_code or ExitCode.ERROR,
            json_mode=json_mode,
        )
    if isinstance(exc, BrokenPipeError):
        return None
    if (code := exit_code_for(exc)) is not None:
        return CliError(safe_text(message_for(exc, code)), code, json_mode=json_mode)
    if isinstance(exc, OSError):
        return CliError(
            f"Filesystem error: {safe_text(exc)}", ExitCode.ERROR, json_mode=json_mode
        )
    if _is_known(exc, "audible_deals.secret_store", "SecretStoreError"):
        return CliError(
            safe_text(exc, keep_newlines=True), ExitCode.ERROR, json_mode=json_mode
        )
    if _is_known(exc, "audible_deals.storage", "StateFileError"):
        return CliError(safe_text(exc), ExitCode.ERROR, json_mode=json_mode)
    if _is_known(exc, "sqlite3", "Error"):
        return CliError(
            f"Local database error: {safe_text(exc)}. Run 'deals doctor' to check it.",
            ExitCode.ERROR,
            json_mode=json_mode,
        )
    return None


def describe(exc: BaseException) -> str:
    """One line for a failure that does not end the program (a loop keeps going)."""
    from audible_deals.presentation.terminal import safe_text

    if isinstance(exc, click.ClickException):
        return safe_text(exc.format_message())
    code = exit_code_for(exc)
    if code is not None:
        return safe_text(message_for(exc, code))
    return safe_text(f"{type(exc).__name__}: {exc}")


def unexpected(exc: Exception, ctx: click.Context) -> CliError:
    """A one-line error for a bug or an unforeseen failure."""
    from audible_deals.presentation.terminal import safe_text

    return CliError(
        f"Unexpected {type(exc).__name__}: {safe_text(exc) or 'no details'}. "
        "Re-run with -vv for the traceback.",
        ExitCode.ERROR,
        json_mode=wants_json(ctx),
    )


def _is_known(exc: BaseException, module_name: str, class_name: str) -> bool:
    """Class check by name through ``sys.modules``, so nothing is imported to test it."""
    module = sys.modules.get(module_name)
    error_type = getattr(module, class_name, None)
    return isinstance(error_type, type) and isinstance(exc, error_type)
