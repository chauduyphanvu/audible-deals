"""Global options that are accepted before *and* after the subcommand.

``deals --locale uk find`` and ``deals find --locale uk`` mean the same thing.
The root group owns the canonical ``--locale`` / ``-v`` options; this module
attaches matching options to every other command (leaf commands and nested
groups) when the root group lazily loads them, so users do not need to know
where an option "belongs".

Each shared option is a small builder returning a fresh ``click.Option`` whose
callback updates the root ``ctx.obj`` dict, which every context in the chain
shares. A value given on the subcommand wins over one given on the root.
Adding another global option means writing one callback + builder and listing
the builder in ``_OPTION_BUILDERS`` (``--color`` was added that way).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import click


def _given_on_command_line(ctx: click.Context, param: click.Parameter) -> bool:
    source = ctx.get_parameter_source(param.name) if param.name else None
    return source == click.core.ParameterSource.COMMANDLINE


def _shared_obj(ctx: click.Context) -> dict[str, Any] | None:
    """The root's shared state dict, or ``None`` when run without the root group."""
    return ctx.obj if isinstance(ctx.obj, dict) else None


def validate_locale(value: str) -> str:
    """Return *value* if it is a supported marketplace, else raise BadParameter."""
    from audible_deals.constants import LOCALE_DOMAIN

    if value not in LOCALE_DOMAIN:
        raise click.BadParameter(
            f"Invalid locale {value!r}. Valid: {', '.join(sorted(LOCALE_DOMAIN))}",
            param_hint="--locale",
        )
    return value


def _apply_locale(ctx: click.Context, param: click.Parameter, value: str | None):
    if value is None or not _given_on_command_line(ctx, param):
        return value
    validate_locale(value)
    obj = _shared_obj(ctx)
    if obj is not None:
        obj["locale"] = value
        obj["locale_explicit"] = True
    return value


def _apply_verbose(ctx: click.Context, param: click.Parameter, value: int):
    if not _given_on_command_line(ctx, param):
        return value
    from audible_deals.logging_setup import configure_logging

    configure_logging(value)
    obj = _shared_obj(ctx)
    if obj is not None:
        obj["verbose"] = value
    return value


def _apply_color(ctx: click.Context, param: click.Parameter, value: str | None):
    if value is None or not _given_on_command_line(ctx, param):
        return value
    from audible_deals.presentation.terminal import set_color_mode

    set_color_mode(value)
    obj = _shared_obj(ctx)
    if obj is not None:
        obj["color"] = value
    return value


def color_option() -> click.Option:
    return click.Option(
        ["--color"],
        type=click.Choice(["auto", "always", "never"]),
        default=None,
        expose_value=False,
        callback=_apply_color,
        metavar="WHEN",
        help=(
            "Colored output: auto (default; honors NO_COLOR and FORCE_COLOR), "
            "always or never. An explicit value wins over those variables."
        ),
    )


def _locale_option() -> click.Option:
    return click.Option(
        ["--locale"],
        default=None,
        expose_value=False,
        callback=_apply_locale,
        metavar="LOCALE",
        help=(
            "Audible marketplace (us, uk, ca, de, fr, au, jp, in, es). "
            "Same as the global --locale; this one wins if both are given."
        ),
    )


def _verbose_option() -> click.Option:
    return click.Option(
        ["-v", "--verbose"],
        count=True,
        expose_value=False,
        callback=_apply_verbose,
        help=(
            "Debug logging (-v for INFO, -vv for DEBUG). "
            "Same as the global -v; this one wins if both are given."
        ),
    )


_OPTION_BUILDERS: tuple[Callable[[], click.Option], ...] = (
    _locale_option,
    _verbose_option,
    color_option,
)


def attach_global_options(command: click.Command) -> click.Command:
    """Add the shared options to *command* and every command nested below it.

    Idempotent, and never overrides an option the command already declares
    under the same flag. Returns *command* for convenient chaining.
    """
    if isinstance(command, click.Group):
        for subcommand in command.commands.values():
            attach_global_options(subcommand)
    taken = {flag for param in command.params for flag in _flags(param)}
    for build in _OPTION_BUILDERS:
        option = build()
        if taken.isdisjoint(_flags(option)):
            command.params.append(option)
            taken.update(_flags(option))
    return command


def _flags(param: click.Parameter) -> set[str]:
    return {*param.opts, *param.secondary_opts}
