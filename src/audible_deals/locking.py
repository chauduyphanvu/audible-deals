"""Cross-process run lock for unattended commands."""

from __future__ import annotations

import contextlib
import os
import time

_LOCK_STALE_SECONDS = 600  # 10 minutes


class LockHeldError(Exception):
    """Raised when the run lock is held by another process."""


@contextlib.contextmanager
def run_lock():
    """Exclusive cross-process lock for unattended commands.

    Acquires via O_CREAT|O_EXCL (atomic on POSIX and Windows NTFS).
    Treats the lock as stale when its mtime is older than 10 minutes.
    Raises LockHeldError when a fresh lock is held by another process.

    PID-ownership: writes our PID to the lock file; the finally block only
    unlinks the file when it still contains our PID, so we never remove
    another process's lock on exit.

    Stale-break: after unlinking a stale lock we retry the O_EXCL create
    exactly once; if that create also fails, we lost the race and raise
    LockHeldError rather than looping indefinitely.
    """
    # Imported here to break the cycle with constants, which re-exports
    # run_lock/LockHeldError for back-compat. LOCK_FILE is resolved at call
    # time so test fixtures can patch it on the constants module.
    from audible_deals import constants

    lock_file = constants.LOCK_FILE
    my_pid = str(os.getpid()).encode()
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    def _try_create() -> bool:
        """Attempt O_EXCL create; return True on success, False on FileExistsError."""
        try:
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        try:
            os.write(fd, my_pid)
        except OSError:
            os.close(fd)
            lock_file.unlink(missing_ok=True)
            raise
        os.close(fd)
        return True

    if not _try_create():
        # Lock file exists — check staleness
        try:
            age = time.time() - lock_file.stat().st_mtime
        except FileNotFoundError:
            age = _LOCK_STALE_SECONDS + 1  # vanished between checks; retry once

        if age > _LOCK_STALE_SECONDS:
            try:
                lock_file.unlink()
            except FileNotFoundError:
                pass
            # Retry once; if another racer beat us, raise immediately
            if not _try_create():
                raise LockHeldError(f"Lock held (mtime {age:.0f}s ago): {lock_file}")
        else:
            raise LockHeldError(f"Lock held (mtime {age:.0f}s ago): {lock_file}")

    try:
        yield
    finally:
        try:
            if lock_file.read_bytes() == my_pid:
                lock_file.unlink()
        except (FileNotFoundError, OSError):
            pass
