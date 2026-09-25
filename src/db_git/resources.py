"""Resource transitions shared by file and database recovery."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from db_git.errors import DbGitError
from db_git.files import durable_unlink, sync_directory
from db_git.recovery import Operation, Resources


def transition(store: Resources, op: Operation, *, undo: bool) -> list[tuple[str, str]]:
    target = store.identity(op.target)
    stage = store.identity(op.stage)
    backup = store.identity(op.backup)
    if backup is not None and backup != op.original_id:
        raise DbGitError("Backup identity changed; refusing to modify it")
    if undo:
        if target == op.original_id and backup is None:
            return []
        if target is None and backup == op.original_id:
            return [(op.backup, op.target)] if backup is not None else []
        if op.stage_id is not None and target == op.stage_id and stage is None:
            if backup != op.original_id:
                raise DbGitError("Original backup is missing")
            pairs = [(op.target, op.stage)]
            if backup is not None:
                pairs.append((op.backup, op.target))
            return pairs
    else:
        if target == op.stage_id and stage is None and backup == op.original_id:
            return []
        if stage != op.stage_id:
            raise DbGitError("Staged replacement identity changed or is missing")
        pairs = []
        if target == op.original_id and backup is None:
            if target is not None:
                pairs.append((op.target, op.backup))
        elif target is not None or backup != op.original_id:
            raise DbGitError("Target identity changed; refusing to overwrite it")
        if stage is not None:
            pairs.append((op.stage, op.target))
        return pairs
    raise DbGitError(
        "Resource identities do not match the journal; preserve them for repair"
    )


class FileResources:
    def identity(self, name: str) -> str | None:
        path = Path(name)
        if not path.exists():
            return None
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def publish(self, record: Operation) -> None:
        self._move(transition(self, record, undo=False))

    def rollback(self, record: Operation) -> None:
        self._move(transition(self, record, undo=True))

    def _move(self, pairs: list[tuple[str, str]]) -> None:
        for source, target in pairs:
            os.replace(source, target)
            sync_directory(Path(target).parent)
            if Path(source).parent != Path(target).parent:
                sync_directory(Path(source).parent)

    def remove(self, name: str, identity: str | None) -> None:
        actual = self.identity(name)
        if actual is None:
            return
        if identity is None or actual != identity:
            raise DbGitError(f"Resource identity changed: {name}")
        durable_unlink(Path(name))
