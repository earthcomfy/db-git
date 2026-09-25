"""Durable local writes and process locks, independent of database engines."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from db_git.errors import DbGitError

_local = threading.local()


def sync_directory(path: Path) -> None:
    if sys.platform != "win32":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def durable_unlink(path: Path) -> None:
    path.unlink(missing_ok=True)
    if path.parent.exists():
        sync_directory(path.parent)


@contextmanager
def local_lock(directory: Path) -> Iterator[None]:
    """Fail fast on concurrent writers; nested calls in one thread are allowed."""
    directory = directory.resolve()
    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = set()
    key = (os.getpid(), directory)
    if key in held:
        yield
        return
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "lock").open("a+b") as stream:
        try:
            if sys.platform == "win32":
                import msvcrt

                stream.seek(0)
                if not stream.read(1):
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise DbGitError(
                "Another db-git operation is running; retry when it finishes."
            ) from e
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)
            if sys.platform == "win32":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
