"""Immutable shared-mode checkpoints and explicit, recoverable retention."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from db_git.backends import DatabaseBackend, DbConnection, get_backend
from db_git.config import DbGitConfig
from db_git.db import parse_database_url
from db_git.errors import SnapshotError
from db_git.recovery import operations
from db_git.repository import require_safe_shared_mode
from db_git.resources import FileResources
from db_git.storage import (
    Checkpoint,
    SnapshotMetadata,
    _read_metadata_file,
    metadata_path,
    read_metadata,
    snapshot_db_name,
    snapshot_dump_path,
    validate_checkpoint_id,
)


def list_checkpoints(
    directory: Path, branch: str | None = None
) -> list[SnapshotMetadata]:
    """Do not silently skip damaged history when making retention decisions."""
    result = []
    names: set[tuple[str, str]] = set()
    for path in sorted(directory.glob("checkpoint_*.meta.json")):
        meta = _read_metadata_file(path)
        if meta is None:
            raise SnapshotError("Checkpoint metadata disappeared; retry history.")
        # A pre-checkpoint version may have used this prefix for a Git branch.
        if meta.checkpoint_id is None:
            if (
                isinstance(meta.branch, str)
                and meta.branch
                and metadata_path(directory, meta.branch) == path
            ):
                continue
            raise SnapshotError(f"Checkpoint metadata has no identity: {path}")
        validate_checkpoint_id(meta.checkpoint_id)
        if (
            not isinstance(meta.branch, str)
            or not meta.branch
            or path != metadata_path(directory, meta.branch, meta.checkpoint_id)
            or not isinstance(meta.resource_identity, str)
            or not meta.resource_identity
        ):
            raise SnapshotError(f"Invalid checkpoint metadata: {path}")
        try:
            created = datetime.fromisoformat(meta.created_at)
            if created.tzinfo is None:
                raise ValueError
        except (TypeError, ValueError) as e:
            raise SnapshotError(f"Invalid checkpoint timestamp: {path}") from e
        if meta.checkpoint_name is not None:
            validate_name(meta.checkpoint_name)
            key = (meta.branch, meta.checkpoint_name)
            if key in names:
                raise SnapshotError(
                    "Duplicate checkpoint names; repair metadata first."
                )
            names.add(key)
        if branch is None or meta.branch == branch:
            result.append(meta)
    return sorted(
        result,
        key=lambda m: (datetime.fromisoformat(m.created_at), m.checkpoint_id),
        reverse=True,
    )


def validate_name(name: str) -> None:
    if not isinstance(name, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", name
    ):
        raise SnapshotError(
            "Checkpoint names need 1-80 letters, digits, dots, hyphens, or "
            "underscores, starting with a letter or digit."
        )


def require_history(config: DbGitConfig) -> None:
    if config.mode != "shared":
        raise SnapshotError(
            "Checkpoints require shared mode (PostgreSQL or MySQL). "
            "Per-branch databases use create --from and reset."
        )
    require_safe_shared_mode(config.mode)


@contextmanager
def history_scope(
    config: DbGitConfig, backend: DatabaseBackend
) -> Iterator[DbConnection]:
    require_history(config)
    params = backend.apply_url_defaults(parse_database_url(config.database_url))
    root = config.snapshot_dir / ".operations"
    if backend.engine == "mysql":
        from db_git.backends.mysql.operations import operation_scope

        with operation_scope(params, root) as conn:
            yield conn
    else:
        from db_git.backends.postgresql.operations import operation_scope as pg_scope

        with pg_scope(backend, params, root) as pg_conn:
            yield pg_conn


def create_checkpoint(
    config: DbGitConfig, branch: str, name: str | None = None
) -> SnapshotMetadata:
    require_history(config)
    if name is not None:
        validate_name(name)
    backend = get_backend(config.database_url)
    with history_scope(config, backend):
        entries = list_checkpoints(config.snapshot_dir, branch)
        if name is not None and any(m.checkpoint_name == name for m in entries):
            raise SnapshotError(
                f"Checkpoint '{name}' already exists for '{branch}'; "
                "choose another name. Checkpoints are immutable."
            )
        if name is not None:
            # A pruned name can still return through recovery. Reserve it until
            # those journals are discarded, so rollback cannot duplicate labels.
            for record in operations(config.snapshot_dir / ".operations"):
                for text in (record.before, record.after):
                    if text is None:
                        continue
                    try:
                        data = json.loads(text)
                    except (TypeError, ValueError) as e:
                        raise SnapshotError(
                            "Recovery metadata cannot be inspected; preserve "
                            "the journals for repair before reusing checkpoint names."
                        ) from e
                    if isinstance(data, dict) and (
                        data.get("branch") == branch
                        and data.get("checkpoint_name") == name
                    ):
                        raise SnapshotError(
                            f"Checkpoint name '{name}' is retained in recovery "
                            f"operation {record.id}; use another name or resolve "
                            "and discard its old recovery records first."
                        )
        checkpoint = Checkpoint(uuid.uuid4().hex, name)
        if metadata_path(config.snapshot_dir, branch, checkpoint.id).exists():
            raise SnapshotError("Checkpoint ID already exists; retry with a new ID.")
        backend.detect_strategy(config).save(
            config.database_url,
            branch,
            config.snapshot_dir,
            config,
            checkpoint=checkpoint,
        )
        meta = read_metadata(config.snapshot_dir, branch, checkpoint.id)
        if meta is None:
            raise SnapshotError(
                "Checkpoint publication is incomplete; inspect db-git recover."
            )
        return meta


def resolve_checkpoint(
    directory: Path, reference: str, branch: str
) -> SnapshotMetadata:
    entries = list_checkpoints(directory, branch)
    by_id = [m for m in entries if reference == m.checkpoint_id]
    by_name = [m for m in entries if reference == m.checkpoint_name]
    matches = (
        by_id
        or by_name
        or [
            m
            for m in entries
            if len(reference) >= 8 and (m.checkpoint_id or "").startswith(reference)
        ]
    )
    if len(matches) != 1:
        raise SnapshotError(
            "Checkpoint not found or ambiguous for this branch; "
            "use a full ID from db-git history."
        )
    return matches[0]


def verify_checkpoint(
    meta: SnapshotMetadata,
    config: DbGitConfig,
    backend: DatabaseBackend,
    conn: DbConnection,
) -> None:
    params = backend.apply_url_defaults(parse_database_url(config.database_url))
    if (
        meta.database != str(params["dbname"])
        or meta.engine != backend.engine
        or meta.strategy not in backend.capabilities.strategies
    ):
        raise SnapshotError(
            "Checkpoint belongs to another seed, engine, or unsupported strategy."
        )
    if meta.strategy == "template":
        from db_git.backends.postgresql.operations import PostgresResources

        name = snapshot_db_name(
            meta.branch,
            meta.database,
            snapshot_dir=config.snapshot_dir,
            checkpoint_id=meta.checkpoint_id,
        )
        actual = PostgresResources(conn, config).identity(name)
    else:
        path = snapshot_dump_path(config.snapshot_dir, meta.branch, meta.checkpoint_id)
        actual = FileResources().identity(str(path))
    if actual is None or actual != meta.resource_identity:
        raise SnapshotError(
            f"Checkpoint {meta.checkpoint_id} is missing or changed; "
            "preserve history and inspect db-git doctor/recover."
        )


def restore_checkpoint(
    config: DbGitConfig, branch: str, reference: str
) -> SnapshotMetadata:
    backend = get_backend(config.database_url)
    with history_scope(config, backend) as conn:
        meta = resolve_checkpoint(config.snapshot_dir, reference, branch)
        verify_checkpoint(meta, config, backend, conn)
        # Checkpoints keep the strategy they were written with, independently of
        # the strategy configured for the next ordinary branch snapshot.
        original_config = replace(config, strategy=meta.strategy)
        backend.detect_strategy(original_config).restore(
            config.database_url,
            branch,
            config.snapshot_dir,
            original_config,
            checkpoint_id=meta.checkpoint_id,
        )
        return meta


def retention_plan(
    entries: list[SnapshotMetadata], keep: int, include_named: bool = False
) -> list[SnapshotMetadata]:
    if keep < 1:
        raise SnapshotError("Retention must keep at least one checkpoint per branch.")
    ordered = sorted(
        entries,
        key=lambda m: (datetime.fromisoformat(m.created_at), m.checkpoint_id),
        reverse=True,
    )
    counts: dict[str, int] = {}
    stale = []
    for meta in ordered:
        counts[meta.branch] = counts.get(meta.branch, 0) + 1
        if counts[meta.branch] > keep and (
            include_named or meta.checkpoint_name is None
        ):
            stale.append(meta)
    return stale


def prune_checkpoints(
    config: DbGitConfig,
    approved_ids: set[str],
    *,
    keep: int,
    branch: str | None = None,
    include_named: bool = False,
) -> None:
    backend = get_backend(config.database_url)
    with history_scope(config, backend) as conn:
        entries = list_checkpoints(config.snapshot_dir, branch)
        eligible = retention_plan(entries, keep, include_named)
        if not approved_ids <= {m.checkpoint_id for m in eligible}:
            raise SnapshotError(
                "History changed since the retention preview; rerun history --prune."
            )
        # Refuse to retire older copies if another checkpoint is missing or has
        # changed. In particular, a damaged newest copy cannot evict a good one.
        for meta in entries:
            verify_checkpoint(meta, config, backend, conn)
        for meta in eligible:
            if meta.checkpoint_id not in approved_ids:
                continue
            original_config = replace(config, strategy=meta.strategy)
            backend.detect_strategy(original_config).cleanup(
                meta.branch,
                config.snapshot_dir,
                original_config,
                checkpoint_id=meta.checkpoint_id,
            )
