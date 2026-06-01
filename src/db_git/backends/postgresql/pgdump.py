from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
from psycopg import sql

from db_git.backends import DatabaseBackend
from db_git.backends.postgresql.connections import handle_active_connections
from db_git.db import parse_database_url
from db_git.errors import DatabaseError, SnapshotError, ToolNotFoundError
from db_git.storage import (
    ensure_snapshot_dir,
    make_metadata,
    metadata_path,
    snapshot_dump_path,
    write_metadata,
)

if TYPE_CHECKING:
    from db_git.config import DbGitConfig


class PgDumpStrategy:
    """
    Snapshot strategy using pg_dump/pg_restore.
    """

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
    ) -> None:
        pg_dump = shutil.which("pg_dump")
        if not pg_dump:
            raise ToolNotFoundError(
                "pg_dump not found in PATH. Install PostgreSQL client tools."
            )

        params = self._backend.apply_url_defaults(parse_database_url(db_url))
        env = self._backend.build_subprocess_env(params)

        ensure_snapshot_dir(snapshot_dir)
        dump_file = snapshot_dump_path(snapshot_dir, branch)

        cmd = _build_pg_dump_cmd(
            pg_dump,
            params,
            str(dump_file),
            self._pg_version,
        )
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
        if result.returncode != 0:
            raise SnapshotError(f"pg_dump failed: {result.stderr.strip()}")

        write_metadata(
            snapshot_dir,
            make_metadata(
                branch=branch,
                database=str(params["dbname"]),
                strategy=self.name,
                engine=self._backend.engine,
                engine_version=str(self._pg_version),
                file_size_bytes=dump_file.stat().st_size,
            ),
        )

    def restore(
        self,
        db_url: str,
        branch: str,
        snapshot_dir: Path,
        config: DbGitConfig,
    ) -> None:
        pg_restore = shutil.which("pg_restore")
        if not pg_restore:
            raise ToolNotFoundError(
                "pg_restore not found in PATH. Install PostgreSQL client tools."
            )

        params = self._backend.apply_url_defaults(parse_database_url(db_url))
        env = self._backend.build_subprocess_env(params)
        dump_file = snapshot_dump_path(snapshot_dir, branch)

        if not dump_file.exists():
            raise SnapshotError(
                f"No dump file found for branch '{branch}' at {dump_file}"
            )

        self._drop_and_create_db(params, config)

        cmd = _build_pg_restore_cmd(pg_restore, params, str(dump_file))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
        if result.returncode != 0 and "ERROR" in result.stderr:
            raise SnapshotError(f"pg_restore failed: {result.stderr.strip()}")

    def cleanup(
        self,
        branch: str,
        snapshot_dir: Path,
        config: DbGitConfig,
    ) -> None:
        dump = snapshot_dump_path(snapshot_dir, branch)
        meta = metadata_path(snapshot_dir, branch)
        if dump.exists():
            dump.unlink()
        if meta.exists():
            meta.unlink()

    def _drop_and_create_db(
        self,
        params: dict[str, str | int],
        config: DbGitConfig,
    ) -> None:
        """
        Drop and recreate the target database via the maintenance connection.
        """
        dbname = str(params["dbname"])
        conn = self._backend.connect_maintenance(params)
        try:
            handle_active_connections(conn, dbname, config)
            db_ident = sql.Identifier(dbname)
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(db_ident)
            )
            conn.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(db_ident)
            )
        except (psycopg.Error, DatabaseError) as e:
            raise SnapshotError(f"Drop/create database failed: {e}") from e
        finally:
            conn.close()


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
        "-h",
        str(params["host"]),
        "-p",
        str(params["port"]),
        "-U",
        str(params["user"]),
        "-f",
        dump_path,
    ]

    if pg_version >= 16:
        cmd.extend(["--compress", "zstd:3"])
    else:
        cmd.extend(["-Z", "1"])

    cmd.append(str(params["dbname"]))
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
        "--no-owner",
        "--no-privileges",
        "-h",
        str(params["host"]),
        "-p",
        str(params["port"]),
        "-U",
        str(params["user"]),
        "-d",
        str(params["dbname"]),
        dump_path,
    ]
