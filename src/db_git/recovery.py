"""Write-ahead journals for staged resource replacement and explicit recovery."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from db_git.errors import DbGitError, SnapshotError
from db_git.files import atomic_write, durable_unlink


class Resources(Protocol):
    def identity(self, name: str) -> str | None: ...
    def publish(self, record: Operation) -> None: ...
    def rollback(self, record: Operation) -> None: ...
    def remove(self, name: str, identity: str | None) -> None: ...


@dataclass
class Operation:
    id: str
    action: str
    kind: str
    server: str
    target: str
    stage: str
    backup: str
    original_id: str | None
    stage_id: str | None
    metadata_path: str | None
    before: str | None
    after: str | None = None
    phase: str = "building"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def write(self, root: Path) -> None:
        atomic_write(
            root / f"{self.id}.json", json.dumps(asdict(self), indent=2) + "\n"
        )


def operations(root: Path) -> list[Operation]:
    result = []
    for path in sorted(root.glob("*.json")):
        try:
            record = Operation(**json.loads(path.read_text()))
            if path.stem != record.id or len(record.id) != 32:
                raise ValueError("invalid operation ID")
            if len({record.target, record.stage, record.backup}) != 3:
                raise ValueError("recovery resources overlap")
            if record.phase not in {
                "building",
                "ready",
                "complete",
                "rolling_back",
                "rolled_back",
                "discarding",
            } or record.kind not in {"database", "file", "sqlite", "mysql"}:
                raise ValueError("unknown operation state")
            result.append(record)
        except (ValueError, TypeError, KeyError) as e:
            raise DbGitError(
                f"Invalid recovery journal: {path}. Preserve it for repair."
            ) from e
    return sorted(result, key=lambda record: record.created_at, reverse=True)


def require_recovered(root: Path) -> None:
    pending = [
        op.id for op in operations(root) if op.phase not in {"complete", "rolled_back"}
    ]
    if pending:
        raise DbGitError(
            f"Interrupted operation {pending[0]}; "
            "run db-git recover before changing the database."
        )


def apply_metadata(record: Operation, text: str | None) -> None:
    if record.metadata_path is None:
        return
    path = Path(record.metadata_path)
    if text is None:
        durable_unlink(path)
    else:
        atomic_write(path, text)


def run_operation(
    root: Path,
    resources: Resources,
    *,
    action: str,
    kind: str,
    server: str,
    target: str,
    names: Callable[[str], tuple[str, str]],
    metadata: Path | None,
    build: Callable[[str], None] | None,
    after: Callable[[str], str | None],
    replace: bool = True,
) -> Operation:
    """Caller holds local/server locks. Keep resources until explicit discard."""
    require_recovered(root)
    original = resources.identity(target)
    if original is not None and not replace:
        raise SnapshotError(f"Database or snapshot already exists: {target}")
    identifier = uuid.uuid4().hex
    stage, backup = names(identifier)
    if resources.identity(stage) is not None or resources.identity(backup) is not None:
        raise DbGitError("Staging resource already exists; refusing to overwrite it")
    record = Operation(
        id=identifier,
        action=action,
        kind=kind,
        server=server,
        target=target,
        stage=stage,
        backup=backup,
        original_id=original,
        stage_id=None,
        metadata_path=str(metadata.resolve()) if metadata is not None else None,
        before=metadata.read_text()
        if metadata is not None and metadata.exists()
        else None,
    )
    record.write(root)
    try:
        if build is not None:
            build(stage)
            record.stage_id = resources.identity(stage)
            if record.stage_id is None:
                raise DbGitError("Replacement was not created")
        record.after = after(stage)
        record.phase = "ready"
        record.write(root)
        finish(root, resources, record)
        return record
    except Exception as error:
        try:
            # Capture a partially built resource before retaining it for inspection.
            if record.stage_id is None:
                record.stage_id = resources.identity(stage)
                record.write(root)
            rollback(root, resources, record)
        except Exception as recovery_error:
            raise DbGitError(
                f"{error}. Automatic rollback could not finish: {recovery_error}. "
                f"Run db-git recover {record.id} --rollback."
            ) from error
        raise


def finish(root: Path, resources: Resources, record: Operation) -> None:
    if record.phase not in {"ready", "complete"}:
        raise DbGitError("Replacement is not ready; use --rollback")
    _check_metadata(record)
    record.phase = "ready"
    record.write(root)
    resources.publish(record)
    apply_metadata(record, record.after)
    record.phase = "complete"
    record.write(root)


def rollback(root: Path, resources: Resources, record: Operation) -> None:
    if record.phase == "discarding":
        raise DbGitError("Backup disposal has started; finish with --discard")
    _check_metadata(record)
    if record.phase == "building" and record.stage_id is None:
        record.stage_id = resources.identity(record.stage)
        record.write(root)
    record.phase = "rolling_back"
    record.write(root)
    resources.rollback(record)
    apply_metadata(record, record.before)
    record.phase = "rolled_back"
    record.write(root)


def discard(root: Path, resources: Resources, record: Operation) -> None:
    if record.phase not in {"complete", "rolled_back", "discarding"}:
        raise DbGitError(
            "Finish or roll back the interrupted operation before discarding it"
        )
    record.phase = "discarding"
    record.write(root)
    resources.remove(record.stage, record.stage_id)
    resources.remove(record.backup, record.original_id)
    durable_unlink(root / f"{record.id}.json")


def _check_metadata(record: Operation) -> None:
    if record.metadata_path is None:
        return
    path = Path(record.metadata_path)
    current = path.read_text() if path.exists() else None
    if current not in (record.before, record.after):
        raise DbGitError(
            "Metadata changed since this operation; refusing to overwrite later work"
        )
