"""``deals completions SHELL --install``: put the script where the shell looks.

Nothing here edits a shell startup file. Each shell has a per-user directory it
loads completions from by itself (bash through the bash-completion package, fish
always, zsh once the directory is on ``fpath``), so installing is writing one
file there, and updating is writing it again.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from audible_deals.storage import _atomic_write, read_text

SHELLS = ("bash", "zsh", "fish")

#: Every script Click generates for ``deals`` mentions this variable, which is
#: how an existing file is recognized as ours (and so safe to replace).
_MARKER = "_DEALS_COMPLETE"


class ForeignFileError(Exception):
    """The target exists and is not a ``deals`` completion script."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"{path} already exists and is not a deals completion script. "
            "Move it aside and run the command again."
        )
        self.path = path


@dataclass(frozen=True)
class InstallResult:
    path: Path
    #: ``installed``, ``updated`` or ``unchanged``.
    status: str
    #: What to do so the shell picks it up (may be several lines).
    hint: str


def _base(variable: str, default: Path, env: Mapping[str, str]) -> Path:
    value = env.get(variable)
    # The XDG specification says a relative value must be ignored.
    if value and Path(value).is_absolute():
        return Path(value)
    return default


def target_path(
    shell: str, *, env: Mapping[str, str] | None = None, home: Path | None = None
) -> Path:
    """The per-user file *shell* loads ``deals`` completions from."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    if shell == "bash":
        data = _base("XDG_DATA_HOME", home / ".local" / "share", env)
        return data / "bash-completion" / "completions" / "deals"
    if shell == "zsh":
        return home / ".zfunc" / "_deals"
    if shell == "fish":
        config = _base("XDG_CONFIG_HOME", home / ".config", env)
        return config / "fish" / "completions" / "deals.fish"
    raise ValueError(f"unsupported shell: {shell}")


def _tilde(path: Path, home: Path) -> str:
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


def activation_hint(shell: str, path: Path, home: Path | None = None) -> str:
    home = Path.home() if home is None else home
    if shell == "bash":
        return (
            "bash-completion loads it the first time you press Tab after "
            "'deals '; start a new shell. (This needs the bash-completion "
            "package.)"
        )
    if shell == "zsh":
        directory = _tilde(path.parent, home)
        return (
            f"If {directory} is not on your fpath yet, add this to ~/.zshrc "
            "before compinit runs:\n"
            f"    fpath+={directory}\n"
            "    autoload -Uz compinit && compinit\n"
            "Then start a new shell."
        )
    return "Fish loads it by itself; start a new shell."


def install(
    shell: str,
    script: str,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> InstallResult:
    """Write *script* to the file *shell* loads (replacing an older copy of it)."""
    home = Path.home() if home is None else home
    path = target_path(shell, env=env, home=home)
    hint = activation_hint(shell, path, home)
    content = script if script.endswith("\n") else script + "\n"
    status = "installed"
    if path.exists():
        try:
            existing = read_text(path)
        except (OSError, UnicodeDecodeError) as exc:
            raise ForeignFileError(path) from exc
        if _MARKER not in existing:
            raise ForeignFileError(path)
        if existing == content:
            return InstallResult(path, "unchanged", hint)
        status = "updated"
    _atomic_write(path, content)
    with suppress(OSError):
        path.chmod(
            stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
        )  # a script, not a secret
    return InstallResult(path, status, hint)
