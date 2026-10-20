"""Generic JSON file persistence with atomic writes and corruption tolerance."""

from __future__ import annotations

import json
import locale
import logging
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_text(path: Path) -> str:
    """Read a state file as UTF-8 (a BOM is tolerated).

    Earlier releases wrote the platform's locale encoding, which is not UTF-8
    on some Windows setups, so a file that is not valid UTF-8 is retried in
    that encoding before giving up.
    """
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        fallback = locale.getpreferredencoding(False)
        if fallback.lower().replace("-", "").replace("_", "") in {"utf8", "ascii"}:
            raise
        return raw.decode(fallback)


def _atomic_write(path: Path, content: str, *, durable: bool = False) -> None:
    """Write content to path atomically via temp file + rename (UTF-8)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            if durable:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
        replaced = True
        if durable:
            _fsync_parent(path)
    except BaseException:
        if not replaced:
            with suppress(FileNotFoundError):
                os.unlink(tmp)
        raise


def _atomic_write_bytes(path: Path, content: bytes, *, durable: bool = False) -> None:
    """Write bytes to path atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    replaced = False
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            if durable:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
        replaced = True
        if durable:
            _fsync_parent(path)
    except BaseException:
        if not replaced:
            with suppress(FileNotFoundError):
                os.unlink(tmp)
        raise


class StateFileError(Exception):
    """A saved data file exists but cannot be safely read for modification.

    Raised instead of overwriting a file whose contents we could not parse, so a
    typo in a hand-edited ``profiles.json`` never costs the user their data.
    """

    def __init__(self, path: Path, message: str) -> None:
        super().__init__(message)
        self.path = path


class _Unusable(Exception):
    """Internal: the file exists but its contents cannot be used as expected."""

    def __init__(self, detail: str, cause: BaseException | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.cause = cause


_JSON_KINDS = {
    dict: "object",
    list: "array",
    str: "string",
    bool: "boolean",
    int: "number",
    float: "number",
    type(None): "null",
}


def _json_kind(value: object) -> str:
    return _JSON_KINDS.get(type(value), type(value).__name__)


def _parse_json_file(path: Path, expected_type: type) -> Any | None:
    """Return the parsed document, or ``None`` when *path* does not exist.

    A blank file holds nothing worth protecting and reads as an empty
    *expected_type*. Anything else that cannot be used raises ``_Unusable``
    with a short human-readable detail that includes the parse position.
    """
    try:
        text = read_text(path)
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise _Unusable(
            f"not valid text: {exc.reason} at byte {exc.start}", exc
        ) from exc
    except OSError as exc:
        raise _Unusable(f"could not be read: {exc.strerror or exc}", exc) from exc
    if not text.strip():
        return expected_type()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _Unusable(
            f"not valid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}", exc
        ) from exc
    if not isinstance(data, expected_type):
        raise _Unusable(
            f"expected a JSON {_json_kind(expected_type())} "
            f"but found a JSON {_json_kind(data)}"
        )
    return data


_warned: set[tuple[str, str]] = set()


def _warn_unusable(path: Path, desc: str, unusable: _Unusable) -> None:
    """Warn once per process about an unusable file; traceback only when debugging."""
    key = (str(path), unusable.detail)
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(
        "%s at %s is corrupt (%s), ignoring",
        desc,
        path,
        unusable.detail,
        exc_info=unusable.cause if logger.isEnabledFor(logging.DEBUG) else None,
    )


def load_json_file(path: Path, expected_type: type, desc: str, *, quiet: bool = False):
    """Load a JSON file, returning an empty expected_type if missing, corrupt, or wrong shape.

    Read-only callers use this: a corrupt file is reported with a one-line
    warning (plus a traceback under ``-vv``) and treated as empty. Callers that
    go on to write must use :func:`ensure_updatable` first so the corrupt
    contents are never silently replaced. ``quiet`` skips the warning for a
    caller that is about to do exactly that, whose refusal says more.
    """
    try:
        data = _parse_json_file(path, expected_type)
    except _Unusable as unusable:
        if not quiet:
            _warn_unusable(path, desc, unusable)
        return expected_type()
    if data is None:
        return expected_type()
    logger.debug("loaded %s (%d) from %s", desc, len(data), path)
    return data


def ensure_updatable(path: Path, expected_type: type, desc: str) -> None:
    """Raise ``StateFileError`` if *path* exists but is not a valid *expected_type*.

    Call this at the start of every read-modify-write of a file the user owns
    (config, profiles, monitors, ...) so a corrupt file is never overwritten.
    A missing or blank file is fine: there is nothing to lose.
    """
    try:
        _parse_json_file(path, expected_type)
    except _Unusable as unusable:
        raise StateFileError(
            path,
            f"Cannot update {desc}: {path} is corrupt ({unusable.detail}). "
            "Fix the file or move it aside, then try again; nothing was changed.",
        ) from unusable.cause


def save_json_file(path: Path, data, desc: str, *, durable: bool = False) -> None:
    _atomic_write(
        path,
        json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False),
        durable=durable,
    )
    logger.debug("saved %s (%d) to %s", desc, len(data), path)
