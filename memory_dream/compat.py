"""Platform shims. POSIX behavior is the reference; Windows is best-effort
but must fail loudly, never silently skip a safety mechanism."""

from __future__ import annotations

import os
from pathlib import Path

if os.name == "posix":
    import fcntl

    def _lock_exclusive_nonblocking(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

elif os.name == "nt":  # pragma: no cover - exercised on Windows CI
    import msvcrt

    def _lock_exclusive_nonblocking(fh) -> None:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:  # pragma: no cover
    raise ImportError(f"unsupported platform: {os.name}")


class LockHeld(RuntimeError):
    """Another process holds the single-flight lock."""


class FileLock:
    """Non-blocking exclusive advisory lock; single-flight guard for apply.

    Context manager. Raises LockHeld immediately when the lock is taken —
    the caller reports which lock file and stops; it never waits.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._fh = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            _lock_exclusive_nonblocking(fh)
        except OSError as exc:
            fh.close()
            raise LockHeld(f"lock {self.path} is held by another process") from exc
        self._fh = fh
        return self

    def __exit__(self, *exc_info) -> None:
        if self._fh is not None:
            try:
                _unlock(self._fh)
            finally:
                self._fh.close()
                self._fh = None


def restrict_permissions(path: Path | str) -> None:
    """Owner-only (0700) on POSIX; no-op on Windows (ACLs are the operator's)."""
    if os.name == "posix":
        os.chmod(path, 0o700)


def is_escaping_link(path: Path) -> bool:
    """True when the path or its parent is a symlink (POSIX) or any reparse
    point / junction (Windows) — the tree could point outside itself."""
    candidates = (path, path.parent)
    if any(p.is_symlink() for p in candidates):
        return True
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        for p in candidates:
            try:
                if os.lstat(p).st_file_attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
                    return True
            except OSError:
                continue
    return False
