"""Shared snapshots with one active working generation and journaled URL selection."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from db_git.backends.mysql.connections import Connection, server_version
from db_git.backends.mysql.dump import capture, restore_archive
from db_git.backends.mysql.operations import (
    MySQLResources,
    generation,
    operation_scope,
    server_identity,
    validate_generation,
)
from db_git.backends.mysql.urls import parse_url
from db_git.errors import DbGitError
from db_git.recovery import run_operation
from db_git.repository import require_safe_shared_mode
from db_git.resources import FileResources
from db_git.storage import (
    Checkpoint,
    make_metadata,
    metadata_path,
    read_metadata,
    snapshot_dump_path,
)

if TYPE_CHECKING:
    from db_git.config import DbGitConfig


def active_path(snapshot_dir: Path) -> Path:
    return snapshot_dir / "mysql-active.json"


def active_database(
    snapshot_dir: Path, seed: str, conn: Connection | None = None
) -> str:
    path = active_path(snapshot_dir)
    if not path.exists():
        return seed
    try:
        data = json.loads(path.read_text())
        name = data["database"]
        if data["seed"] != seed:
            raise ValueError("wrong seed")
        validate_generation(name, seed)
        if conn is not None and (
            data["server"] != server_identity(conn, seed)
            or MySQLResources(conn).identity(name) != data["identity"]
        ):
            raise ValueError("generation missing or changed")
        return str(name)
    except (ValueError, KeyError, TypeError) as e:
        raise DbGitError(
            "MySQL active-generation metadata is invalid, missing its "
            "database, or belongs to another server. Inspect db-git "
            "recover."
        ) from e


def file_names(root: Path, token: str) -> tuple[str, str]:
    return str((root / f"{token}.stage").resolve()), str(
        (root / f"{token}.backup").resolve()
    )


class MySQLDumpStrategy:
    name = "mysqldump"

    def save(
        self,
        db_url: str,
        branch: str,
        snapshot_dir: Path,
        config: DbGitConfig,
        *,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        require_safe_shared_mode(config.mode)
        params = {k: v for k, v in parse_url(db_url).items() if v is not None}
        seed = str(params["dbname"])
        root = snapshot_dir / ".operations"
        checkpoint_id = checkpoint.id if checkpoint else None
        with operation_scope(params, root) as conn:
            source = active_database(snapshot_dir, seed, conn)
            dump = snapshot_dump_path(snapshot_dir, branch, checkpoint_id)
            existing = read_metadata(snapshot_dir, branch, checkpoint_id)
            if dump.exists() and existing is None:
                raise DbGitError("Refusing to replace an untracked MySQL snapshot.")
            if existing and (existing.engine != "mysql" or existing.database != seed):
                raise DbGitError("Existing snapshot belongs to another engine or seed.")

            def after(stage: str) -> str:
                meta = make_metadata(
                    branch,
                    seed,
                    self.name,
                    "mysql",
                    server_version(conn),
                    Path(stage).stat().st_size,
                    checkpoint=checkpoint,
                    resource_identity=FileResources().identity(stage)
                    if checkpoint
                    else None,
                )
                return json.dumps(asdict(meta), indent=2) + "\n"

            run_operation(
                root,
                FileResources(),
                action="save",
                kind="file",
                server=server_identity(conn, seed),
                target=str(dump.resolve()),
                names=lambda token: file_names(root, token),
                metadata=metadata_path(snapshot_dir, branch, checkpoint_id),
                build=lambda stage: capture(conn, params, source, Path(stage)),
                after=after,
                replace=checkpoint is None,
            )

    def restore(
        self,
        db_url: str,
        branch: str,
        snapshot_dir: Path,
        config: DbGitConfig,
        *,
        checkpoint_id: str | None = None,
    ) -> None:
        require_safe_shared_mode(config.mode)
        params = {k: v for k, v in parse_url(db_url).items() if v is not None}
        seed = str(params["dbname"])
        root = snapshot_dir / ".operations"
        with operation_scope(params, root) as conn:
            previous = active_database(snapshot_dir, seed, conn)
            meta = read_metadata(snapshot_dir, branch, checkpoint_id)
            if meta is None or meta.engine != "mysql" or meta.database != seed:
                raise DbGitError(
                    "MySQL snapshot metadata does not match the configured seed."
                )
            dump = snapshot_dump_path(snapshot_dir, branch, checkpoint_id)

            def after(stage: str) -> str:
                return (
                    json.dumps(
                        {
                            "database": stage,
                            "seed": seed,
                            "server": server_identity(conn, seed),
                            "identity": MySQLResources(conn).identity(stage),
                        },
                        indent=2,
                    )
                    + "\n"
                )

            run_operation(
                root,
                MySQLResources(conn),
                action="restore",
                kind="mysql",
                server=server_identity(conn, seed),
                target=previous,
                names=lambda token: (
                    generation(seed, "shared", token),
                    generation(seed, "retained"),
                ),
                metadata=active_path(snapshot_dir),
                build=lambda stage: restore_archive(conn, params, dump, stage),
                after=after,
            )

    def cleanup(
        self,
        branch: str,
        snapshot_dir: Path,
        config: DbGitConfig,
        *,
        checkpoint_id: str | None = None,
    ) -> None:
        require_safe_shared_mode(config.mode)
        params = {
            k: v for k, v in parse_url(config.database_url).items() if v is not None
        }
        root = snapshot_dir / ".operations"
        with operation_scope(params, root) as conn:
            meta = read_metadata(snapshot_dir, branch, checkpoint_id)
            if meta is None:
                return
            if meta.engine != "mysql" or meta.database != str(params["dbname"]):
                raise DbGitError("Snapshot belongs to another engine or seed.")
            run_operation(
                root,
                FileResources(),
                action="prune",
                kind="file",
                server=server_identity(conn, str(params["dbname"])),
                target=str(
                    snapshot_dump_path(snapshot_dir, branch, checkpoint_id).resolve()
                ),
                names=lambda token: file_names(root, token),
                metadata=metadata_path(snapshot_dir, branch, checkpoint_id),
                build=None,
                after=lambda _: None,
            )
