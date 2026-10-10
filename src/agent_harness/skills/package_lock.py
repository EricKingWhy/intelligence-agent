"""Cross-process lock shared by package lifecycle and project Skill writes."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MANIFEST_FILENAME = "plugin-installs.json"

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


@contextmanager
def registry_lock(path: Path) -> Iterator[None]:
    """Lock the project Skills registry and reject linked lock files."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is not None:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        with os.fdopen(descriptor, "r+b", buffering=0) as handle:
            if _is_reparse_point(lock_path):
                raise OSError(f"refusing linked package registry lock: {lock_path}")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover - Windows
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        descriptor = _open_windows_lock_file(lock_path, flags)
        try:
            _verify_windows_lock_handle(descriptor, lock_path)
            with os.fdopen(descriptor, "r+b", buffering=0) as handle:
                descriptor = -1
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    # msvcrt.locking locks a byte range; initialize only after the
                    # opened handle is proven to be the intended local lock file.
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return
    yield  # pragma: no cover


def _is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_attribute)


def _open_windows_lock_file(lock_path: Path, flags: int) -> int:
    """Create exclusively or open an existing regular lock file without O_CREAT."""
    if _is_reparse_point(lock_path):
        raise OSError(f"refusing linked package registry lock: {lock_path}")
    try:
        return os.open(lock_path, flags | os.O_EXCL, 0o600)
    except FileExistsError:
        if _is_reparse_point(lock_path):
            raise OSError(f"refusing linked package registry lock: {lock_path}")
        return os.open(lock_path, flags & ~os.O_CREAT, 0o600)


def _verify_windows_lock_handle(descriptor: int, lock_path: Path) -> None:
    if _is_reparse_point(lock_path):
        raise OSError(f"refusing linked package registry lock: {lock_path}")
    try:
        from agent_harness.skills.inspection import (
            _same_windows_path,
            _windows_open_handle_path,
        )

        opened_path = _windows_open_handle_path(descriptor)
        expected_path = lock_path.resolve(strict=True)
        if not _same_windows_path(opened_path, expected_path):
            raise OSError(f"package registry lock opened outside its path: {lock_path}")
        if not os.path.samestat(os.fstat(descriptor), lock_path.stat()):
            raise OSError(f"package registry lock changed while opening: {lock_path}")
    except OSError:
        raise
    except Exception as error:
        raise OSError("cannot verify package registry lock handle safely") from error
