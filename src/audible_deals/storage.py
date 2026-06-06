"""Generic JSON file persistence with atomic writes and corruption tolerance."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from audible_deals.constants import _atomic_write

logger = logging.getLogger(__name__)


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


def save_json_file(path: Path, data, desc: str) -> None:
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False))
    logger.debug("saved %s (%d) to %s", desc, len(data), path)
