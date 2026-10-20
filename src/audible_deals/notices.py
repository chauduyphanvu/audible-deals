"""Warnings and notes for the person at the terminal.

Everything the tool says *about* a run rather than *as* its result goes to
stderr as one line that starts ``Warning: `` (something is off but the run
continues) or ``Note: `` (something worth knowing). That keeps piped and
``--json`` output clean and gives scripts one pattern to match. Log records at
WARNING and the ``NOTE`` level (see ``logging_setup``) are rendered the same
way unless ``-v`` asks for the detailed log format.
"""

from __future__ import annotations

import click

_ESCAPES: dict[int, str] = {
    **{code: f"\\x{code:02x}" for code in [*range(0x20), 0x7F]},
    ord("\t"): "\\t",
    ord("\n"): "\\n",
    ord("\r"): "\\r",
}


def _plain(message: object) -> str:
    """One printable line: control characters in untrusted text are escaped."""
    return (message if isinstance(message, str) else str(message)).translate(_ESCAPES)


def warn(message: object) -> None:
    """``Warning: ...`` on stderr."""
    click.echo(f"Warning: {_plain(message)}", err=True)


def note(message: object) -> None:
    """``Note: ...`` on stderr."""
    click.echo(f"Note: {_plain(message)}", err=True)
