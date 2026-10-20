"""Keeping ``track.log`` small, and reading only the end of it.

The scheduler appends everything a background run prints to ``track.log`` (its
own redirect opens the file, so this process cannot reopen it). At the start of
a run the log is rotated when it has grown past :data:`MAX_BYTES`: the older
generations shift up (``track.log.1`` -> ``.2`` -> ``.3``, the oldest is
dropped), the current content is copied to ``track.log.1`` and the file is
truncated *in place*. Truncating rather than renaming matters: the scheduler's
append-mode descriptor stays valid, so the rest of the current run still lands in
the new, short log.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_BYTES = 1_000_000
KEEP = 3
_BLOCK = 8192


def _generation(path: Path, number: int) -> Path:
    return path.with_name(f"{path.name}.{number}")


def rotate_log(path: Path, *, max_bytes: int = MAX_BYTES, keep: int = KEEP) -> bool:
    """Rotate *path* when it is larger than *max_bytes*. Returns whether it did.

    Never raises: a log that cannot be rotated (a permission problem, a file
    another process holds open on Windows) is not a reason to fail a run.
    """
    try:
        if path.stat().st_size <= max_bytes:
            return False
    except OSError:
        return False
    try:
        for number in range(keep, 1, -1):
            older = _generation(path, number - 1)
            if older.exists():
                os.replace(older, _generation(path, number))
        mode = path.stat().st_mode & 0o777
        newest = _generation(path, 1)
        fd = os.open(newest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as target, path.open("rb") as source:
            shutil.copyfileobj(source, target)
        with path.open("r+b") as current:
            current.truncate(0)
    except OSError as exc:
        logger.debug("could not rotate %s: %s", path, exc)
        return False
    return True


def tail_lines(path: Path, count: int) -> list[str]:
    """The last *count* lines of *path*, reading backwards from the end."""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        data = b""
        # count + 1 line breaks guarantee count complete lines (the first
        # fragment may be cut in the middle).
        while position > 0 and data.count(b"\n") <= count:
            read = min(_BLOCK, position)
            position -= read
            handle.seek(position)
            data = handle.read(read) + data
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if position > 0 and lines:
        lines = lines[1:]  # the first line was cut off by the block boundary
    return lines[-count:] if count > 0 else []
