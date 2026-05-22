from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
from psycopg import sql

from git_db.backends import DatabaseBackend
from git_db.backends.postgresql.connections import handle_active_connections
from git_db.backends.postgresql.template import _create_from_template
from git_db.db import parse_database_url
from git_db.errors import SnapshotError, ToolNotFoundError
from git_db.state import BranchDbEntry, load_state, record_branch_db, remove_branch_db

if TYPE_CHECKING:
    from git_db.config import GitDbConfig


class PostgresBranchDbManager:
    """
    Per-branch database operations for PostgreSQL.
    """

    def __init__(self, backend: DatabaseBackend, config: GitDbConfig) -> None:
        self._backend = backend
        self._config = config
        self._params = backend.apply_url_defaults(
            parse_database_url(config.database_url)
        )

    def exists(self, name: str) -> bool:
        conn = self._backend.connect_maintenance(self._params)
        try:
            cur = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (name,),
            )
            return cur.fetchone() is not None
        finally:
            conn.close()

    def create(
        self,
        target: str,
        source: str,
        branch: str,
        created_from: str,
        git_dir: Path,
    ) -> None:
        strategy = self._backend.detect_strategy(self._config)

        if strategy.name == "template":
            _create_via_template(
                self._backend, self._params, target, source, self._config
            )
        else:
            _create_via_pgdump(
                self._backend,
                self._params,
                self._backend.build_subprocess_env(self._params),
                target,
                source,
                self._config,
            )

        record_branch_db(git_dir, branch, target, created_from)

    def drop(self, name: str, branch: str, git_dir: Path) -> None:
        conn = self._backend.connect_maintenance(self._params)
        try:
            handle_active_connections(conn, name, self._config)
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )
        finally:
            conn.close()

        remove_branch_db(git_dir, branch)

    def list(self, git_dir: Path) -> list[tuple[str, BranchDbEntry, bool]]:
        state = load_state(git_dir)
        return [
            (branch, entry, self.exists(entry.db_name))
            for branch, entry in state.databases.items()
        ]


def _create_via_template(
    backend: DatabaseBackend,
    params: dict[str, str | int],
    target: str,
    source: str,
    config: GitDbConfig,
) -> None:
    """
    Create a database using CREATE DATABASE ... TEMPLATE.
    """
    conn = backend.connect_maintenance(params)
    try:
        handle_active_connections(conn, source, config)
        handle_active_connections(conn, target, config)
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(target)
            )
        )

        _create_from_template(conn, target, source)
    except psycopg.Error as e:
        raise SnapshotError(f"Template clone failed: {e}") from e
    finally:
        conn.close()


def _create_via_pgdump(
    backend: DatabaseBackend,
    params: dict[str, str | int],
    env: dict[str, str],
    target: str,
    source: str,
    config: GitDbConfig,
) -> None:
    """
    Create a database by piping pg_dump to pg_restore.
    """
    pg_dump = shutil.which("pg_dump")
    pg_restore = shutil.which("pg_restore")

    if not pg_dump:
        raise ToolNotFoundError("pg_dump not found in PATH.")
    if not pg_restore:
        raise ToolNotFoundError("pg_restore not found in PATH.")

    common_args = [
        "-h",
        str(params["host"]),
        "-p",
        str(params["port"]),
        "-U",
        str(params["user"]),
    ]

    conn = backend.connect_maintenance(params)
    try:
        handle_active_connections(conn, target, config)
        target_ident = sql.Identifier(target)
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(target_ident)
        )
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(target_ident)
        )
    except psycopg.Error as e:
        raise SnapshotError(f"Create branch database failed: {e}") from e
    finally:
        conn.close()

    dump_cmd = [
        pg_dump,
        "-Fc",
        "--no-owner",
        "--no-privileges",
        *common_args,
        source,
    ]
    restore_cmd = [
        pg_restore,
        "--no-owner",
        "--no-privileges",
        *common_args,
        "-d",
        target,
    ]

    dump_proc = subprocess.Popen(
        dump_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    restore_result = subprocess.run(
        restore_cmd,
        stdin=dump_proc.stdout,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    if dump_proc.stdout is not None:
        dump_proc.stdout.close()
    dump_proc.wait(timeout=300)

    if dump_proc.returncode != 0:
        stderr = dump_proc.stderr.read().decode() if dump_proc.stderr else ""
        raise SnapshotError(f"pg_dump failed: {stderr.strip()}")

    if restore_result.returncode != 0 and "ERROR" in restore_result.stderr:
        raise SnapshotError(f"pg_restore failed: {restore_result.stderr.strip()}")
