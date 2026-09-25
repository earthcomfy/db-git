from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from psycopg import sql

from db_git.backends import DatabaseBackend
from db_git.backends.postgresql.connections import client_dsn
from db_git.backends.postgresql.operations import (
    PostgresResources,
    operation_scope,
    resource_names,
    server_identity,
)
from db_git.db import parse_database_url
from db_git.errors import SnapshotError, ToolNotFoundError
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


def file_names(root: Path, identifier: str) -> tuple[str, str]:
    return str((root / f"{identifier}.stage").resolve()), str(
        (root / f"{identifier}.backup").resolve()
    )


def _restore_dump(
    pg_restore: str, params: dict[str, str | int], dump: str, env: dict[str, str]
) -> None:
    result = subprocess.run(
        _build_pg_restore_cmd(pg_restore, params, dump),
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    if result.returncode != 0:
        raise SnapshotError(f"pg_restore failed: {result.stderr.strip()}")


class PgDumpStrategy:
    name = "pgdump"

    def __init__(self, backend: DatabaseBackend, pg_version: int) -> None:
        self._backend = backend
        self._pg_version = pg_version

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
        pg_dump = shutil.which("pg_dump")
        if not pg_dump:
            raise ToolNotFoundError(
                "pg_dump not found in PATH. Install PostgreSQL client tools."
            )
        params = self._backend.apply_url_defaults(parse_database_url(db_url))
        root = snapshot_dir / ".operations"
        checkpoint_id = checkpoint.id if checkpoint else None
        with operation_scope(self._backend, params, root):
            dump = snapshot_dump_path(snapshot_dir, branch, checkpoint_id)
            if (
                dump.exists()
                and read_metadata(snapshot_dir, branch, checkpoint_id) is None
            ):
                raise SnapshotError(f"Refusing to replace untracked dump file {dump}")

            def build(stage: str) -> None:
                result = subprocess.run(
                    _build_pg_dump_cmd(pg_dump, params, stage, self._pg_version),
                    capture_output=True,
                    text=True,
                    env=self._backend.build_subprocess_env(params),
                    timeout=300,
                )
                if result.returncode != 0:
                    raise SnapshotError(f"pg_dump failed: {result.stderr.strip()}")
                with open(stage, "rb") as stream:
                    os.fsync(stream.fileno())

            def after(stage: str) -> str:
                meta = make_metadata(
                    branch,
                    str(params["dbname"]),
                    self.name,
                    self._backend.engine,
                    str(self._pg_version),
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
                server=server_identity(params),
                target=str(dump.resolve()),
                names=lambda identifier: file_names(root, identifier),
                metadata=metadata_path(snapshot_dir, branch, checkpoint_id),
                build=build,
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
        pg_restore = shutil.which("pg_restore")
        if not pg_restore:
            raise ToolNotFoundError(
                "pg_restore not found in PATH. Install PostgreSQL client tools."
            )
        params = self._backend.apply_url_defaults(parse_database_url(db_url))
        root = snapshot_dir / ".operations"
        with operation_scope(self._backend, params, root) as conn:
            dump = snapshot_dump_path(snapshot_dir, branch, checkpoint_id)
            if not dump.exists():
                raise SnapshotError(
                    f"No dump file found for branch '{branch}' at {dump}"
                )

            def build(stage: str) -> None:
                conn.execute(
                    sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                        sql.Identifier(stage)
                    )
                )
                _restore_dump(
                    pg_restore,
                    {**params, "dbname": stage},
                    str(dump),
                    self._backend.build_subprocess_env(params),
                )

            run_operation(
                root,
                PostgresResources(conn, config),
                action="restore",
                kind="database",
                server=server_identity(params),
                target=str(params["dbname"]),
                names=lambda identifier: resource_names(params, identifier),
                metadata=None,
                build=build,
                after=lambda _: None,
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
        params = self._backend.apply_url_defaults(
            parse_database_url(config.database_url)
        )
        root = snapshot_dir / ".operations"
        with operation_scope(self._backend, params, root):
            if read_metadata(snapshot_dir, branch, checkpoint_id) is None:
                return
            dump = snapshot_dump_path(snapshot_dir, branch, checkpoint_id)
            run_operation(
                root,
                FileResources(),
                action="prune",
                kind="file",
                server=server_identity(params),
                target=str(dump.resolve()),
                names=lambda identifier: file_names(root, identifier),
                metadata=metadata_path(snapshot_dir, branch, checkpoint_id),
                build=None,
                after=lambda _: None,
            )


def _build_pg_dump_cmd(
    pg_dump: str,
    params: dict[str, str | int],
    dump_path: str,
    pg_version: int,
) -> list[str]:
    """
    Build the pg_dump command list.
    """
    cmd = [
        pg_dump,
        "-Fc",
        "--no-owner",
        "--no-privileges",
        "-f",
        dump_path,
    ]

    if pg_version >= 16:
        cmd.extend(["--compress", "zstd:3"])
    else:
        cmd.extend(["-Z", "1"])

    cmd.extend(["-d", client_dsn(params)])
    return cmd


def _build_pg_restore_cmd(
    pg_restore: str,
    params: dict[str, str | int],
    dump_path: str,
) -> list[str]:
    """
    Build the pg_restore command list.
    """
    return [
        pg_restore,
        "--exit-on-error",
        "--no-owner",
        "--no-privileges",
        "-d",
        client_dsn(params),
        dump_path,
    ]
