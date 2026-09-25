"""Journal new SQLite files and ownership changes, never live-file moves."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from db_git.errors import DbGitError
from db_git.files import local_lock, sync_directory
from db_git.recovery import Operation, require_recovered


def server_identity(seed: str) -> str:
    return hashlib.sha256(("sqlite:" + seed).encode()).hexdigest()


@contextmanager
def operation_scope(root: Path, *, recovering: bool = False) -> Iterator[None]:
    with local_lock(root):
        if not recovering:
            require_recovered(root)
        yield


class SQLiteResources:
    """Published generations remain usable by existing connections after reset/rollback.

    Recovery changes ownership metadata; it never moves or deletes published files.
    Identity follows the file, allowing applications to modify its contents normally.
    File cleanup is deliberately manual, because idle connections cannot be detected
    portably and unlinking an open SQLite database is unsafe.
    """

    def identity(self, name: str) -> str | None:
        path = Path(name)
        if path.is_symlink():
            raise DbGitError("Managed SQLite files must not be symbolic links.")
        if not path.exists():
            return None
        info = path.stat()
        if not path.is_file() or info.st_nlink != 1:
            raise DbGitError(
                "Managed SQLite files must be regular files without hard links."
            )
        return f"{info.st_dev}:{info.st_ino}"

    def publish(self, record: Operation) -> None:
        if record.action == "drop":
            if self.identity(record.target) != record.original_id:
                raise DbGitError(
                    "SQLite file identity changed; preserve it for repair."
                )
            return
        target, stage = self.identity(record.target), self.identity(record.stage)
        if target == record.stage_id and stage is None and target is not None:
            return
        if target is not None or stage != record.stage_id or stage is None:
            raise DbGitError(
                "SQLite generation changed or is missing; refusing publication."
            )
        # Only the private staging file is renamed, before any application sees it.
        os.rename(record.stage, record.target)
        sync_directory(Path(record.target).parent)

    def rollback(self, record: Operation) -> None:
        # Keep both generations. The journal restores the previous ownership map.
        target = self.identity(record.target)
        expected = record.original_id if record.action == "drop" else record.stage_id
        if target is not None and target != expected:
            raise DbGitError("SQLite file identity changed; refusing recovery.")

    def remove(self, name: str, identity: str | None) -> None:
        # Discarding a journal does not prove that an application closed the file.
        # Retain all SQLite files, including staged files, for explicit manual cleanup.
        return
