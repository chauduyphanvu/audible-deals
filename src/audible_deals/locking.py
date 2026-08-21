"""Cross-process run lock for unattended commands."""

from __future__ import annotations

import contextlib
import errno
import os
import time


_LEGACY_PARTIAL_STALE_SECONDS = 600


class LockHeldError(Exception):
    """Raised when the run lock is held by another process."""


def _acquire(fd: int) -> None:
    """Acquire a non-blocking exclusive advisory lock for *fd*."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            raise LockHeldError("lock held by another process") from e
        return

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as e:
        raise LockHeldError("lock held by another process") from e


def _release(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def advisory_lock(lock_file, *, wait: bool = False):
    """Acquire a crash-safe advisory lock on a dedicated lock file."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
    while True:
        try:
            _acquire(fd)
            break
        except LockHeldError:
            if not wait:
                os.close(fd)
                raise
            time.sleep(0.01)
        except Exception:
            os.close(fd)
            raise
    try:
        yield
    finally:
        try:
            _release(fd)
        finally:
            os.close(fd)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as e:
        return e.errno != errno.ESRCH
    return True


def _legacy_file_is_fresh(lock_file) -> bool:
    try:
        return time.time() - lock_file.stat().st_mtime < _LEGACY_PARTIAL_STALE_SECONDS
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _write_advisory_marker(fd: int) -> None:
    os.ftruncate(fd, 0)
    os.write(fd, f"advisory:{os.getpid()}".encode())


def _acquire_existing_lock(lock_file):
    try:
        fd = os.open(str(lock_file), os.O_RDWR)
    except FileNotFoundError:
        return None
    try:
        content = os.read(fd, os.fstat(fd).st_size).decode(errors="replace")
        if not content.startswith("advisory:"):
            if content.strip().isdigit() and _pid_is_alive(int(content.strip())):
                raise LockHeldError("lock held by a running legacy process")
            if not content.strip() and _legacy_file_is_fresh(lock_file):
                raise LockHeldError("lock file is being created by another process")
        _acquire(fd)
        try:
            same_file = os.path.samestat(os.fstat(fd), lock_file.stat())
        except FileNotFoundError:
            same_file = False
        if not same_file:
            _release(fd)
            os.close(fd)
            return None
        _write_advisory_marker(fd)
        return fd
    except Exception:
        os.close(fd)
        raise


@contextlib.contextmanager
def run_lock():
    """Acquire an exclusive, crash-safe lock for unattended commands.

    The operating system releases the advisory lock if its owner exits or
    crashes. The lock-file timestamp is deliberately not used: a legitimate
    catalog run may take longer than any fixed stale threshold.
    """
    # Resolve LOCK_FILE at call time so test fixtures can patch constants.
    from audible_deals import constants

    lock_file = constants.LOCK_FILE
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError:
        fd = _acquire_existing_lock(lock_file)
        if fd is None:
            try:
                fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                fd = _acquire_existing_lock(lock_file)
                if fd is None:
                    raise LockHeldError("lock file disappeared while acquiring")
            else:
                try:
                    _acquire(fd)
                    _write_advisory_marker(fd)
                except Exception:
                    os.close(fd)
                    raise
    else:
        try:
            _acquire(fd)
            _write_advisory_marker(fd)
        except Exception:
            os.close(fd)
            raise

    try:
        yield
    finally:
        try:
            _release(fd)
        finally:
            os.close(fd)
