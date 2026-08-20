"""Generic JSON file persistence with atomic writes and corruption tolerance."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import suppress
from pathlib import Path

logger = logging.getLogger(__name__)


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, content: str, *, durable: bool = False) -> None:
    """Write content to path atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    replaced = False
    try:
        with os.fdopen(fd, "w") as f:
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


def load_json_file(path: Path, expected_type: type, desc: str):
    """Load a JSON file, returning an empty expected_type if missing, corrupt, or wrong shape."""
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, expected_type):
                logger.debug("loaded %s (%d) from %s", desc, len(data), path)
                return data
            logger.warning(
                "%s at %s is not a %s, ignoring", desc, path, expected_type.__name__
            )
        except (json.JSONDecodeError, KeyError, OSError):
            logger.warning("%s at %s is corrupt, ignoring", desc, path, exc_info=True)
    return expected_type()


def save_json_file(path: Path, data, desc: str, *, durable: bool = False) -> None:
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False), durable=durable)
    logger.debug("saved %s (%d) to %s", desc, len(data), path)
