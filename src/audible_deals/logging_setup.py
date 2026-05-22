"""Process-wide logging configuration for audible-deals.

The CLI calls ``configure_logging`` once at startup. All other modules just do
``logger = logging.getLogger(__name__)`` and emit records normally — the
handler installed here decides what reaches stderr.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

_ROOT = "audible_deals"
_NOISY = ("audible", "urllib3", "httpx", "httpcore")
_BRIEF = "%(levelname)s %(name)s: %(message)s"
_DEBUG_FMT = "%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"

logger = logging.getLogger(__name__)


def configure_logging(verbose: int = 0) -> None:
    """Install a single stderr handler on the package root logger.

    verbose=0 → WARNING, 1 → INFO, 2+ → DEBUG. ``DEALS_DEBUG=1`` in the
    environment forces DEBUG regardless of the flag. Idempotent: repeated
    calls replace the existing handler so test runs stay clean.
    """
    if os.environ.get("DEALS_DEBUG", "").strip() in ("1", "true", "yes"):
        verbose = max(verbose, 2)

    if verbose >= 2:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    else:
        level = logging.WARNING

    root = logging.getLogger(_ROOT)
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    handler = logging.StreamHandler()
    handler.setLevel(level)
    fmt = _DEBUG_FMT if level == logging.DEBUG else _BRIEF
    handler.setFormatter(logging.Formatter(fmt, datefmt=_DATEFMT))
    root.addHandler(handler)
    root.setLevel(level)

    noisy_level = logging.DEBUG if verbose >= 2 else logging.WARNING
    for name in _NOISY:
        logging.getLogger(name).setLevel(noisy_level)

    log_file_env = os.environ.get("DEALS_LOG_FILE", "").strip()
    if log_file_env:
        try:
            log_path = Path(log_file_env).expanduser()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(logging.Formatter(_DEBUG_FMT, datefmt=_DATEFMT))
            root.addHandler(file_handler)
            root.setLevel(logging.DEBUG)
        except OSError as e:
            logger.warning("Could not open log file %r: %s", log_file_env, e)
