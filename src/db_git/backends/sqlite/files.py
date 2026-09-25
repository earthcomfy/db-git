"""Bounded online backups that include committed WAL contents."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from db_git.errors import DatabaseError
from db_git.files import sync_directory


def connect(path: Path, *, writable: bool = False) -> sqlite3.Connection:
    if not path.is_file():
        raise DatabaseError(f"SQLite database does not exist: {path}")
    try:
        return sqlite3.connect(
            path.as_uri() + ("?mode=rw" if writable else "?mode=ro"),
            uri=True,
            timeout=0.1,
        )
    except sqlite3.Error as e:
        raise DatabaseError(f"Cannot open SQLite database: {path}") from e


def check_database(path: Path) -> None:
    try:
        with closing(connect(path)) as conn:
            # Read the schema without accidentally creating a missing seed file.
            conn.execute("SELECT count(*) FROM sqlite_schema").fetchone()
    except sqlite3.Error as e:
        raise DatabaseError(f"Invalid or inaccessible SQLite database: {path}") from e


def backup(source: Path, destination: Path, timeout_ms: int) -> None:
    deadline = time.monotonic() + timeout_ms / 1000

    def progress(status: int, remaining: int, total: int) -> None:
        if time.monotonic() > deadline:
            raise DatabaseError("SQLite backup timed out; stop writers and retry.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    # Never truncate an existing file, even after a previous interrupted operation.
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        with (
            closing(connect(source)) as src,
            closing(connect(destination, writable=True)) as dst,
        ):
            src.backup(dst, pages=256, progress=progress, sleep=0.01)
            # Make the staged copy self-contained; do not copy/delete source sidecars.
            dst.execute("PRAGMA journal_mode=DELETE").fetchone()
            if dst.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise DatabaseError("SQLite backup failed its integrity check.")
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())
        sync_directory(destination.parent)
    except sqlite3.Error as e:
        raise DatabaseError(
            "SQLite backup failed; the source database is unchanged."
        ) from e
