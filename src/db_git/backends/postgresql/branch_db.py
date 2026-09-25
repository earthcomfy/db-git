from __future__ import annotations

import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
from psycopg import sql

from db_git.backends import DatabaseBackend, DbConnection
from db_git.backends.postgresql.connections import client_dsn, handle_active_connections
from db_git.backends.postgresql.operations import (
    PostgresResources,
    operation_scope,
    resource_names,
    server_identity,
)
from db_git.backends.postgresql.template import _create_from_template
from db_git.db import parse_database_url
from db_git.errors import SnapshotError, ToolNotFoundError
from db_git.recovery import run_operation
from db_git.repository import operations_directory
from db_git.state import (
    BranchDbEntry,
    get_branch_db,
    load_state,
    state_path,
    state_text,
)

if TYPE_CHECKING:
    from db_git.config import DbGitConfig


class PostgresBranchDbManager:
    def __init__(self, backend: DatabaseBackend, config: DbGitConfig) -> None:
        self._backend = backend
        self._config = config
        self._params = backend.apply_url_defaults(
            parse_database_url(config.database_url)
        )

    def exists(self, name: str) -> bool:
        conn = self._backend.connect_maintenance(self._params)
        try:
            return PostgresResources(conn, self._config).identity(name) is not None
        finally:
            conn.close()

    def create(
        self, target: str, source: str, branch: str, created_from: str, git_dir: Path
    ) -> None:
        self._replace(target, source, branch, created_from, git_dir, reset=False)

    def reset(
        self, target: str, source: str, branch: str, created_from: str, git_dir: Path
    ) -> None:
        self._replace(target, source, branch, created_from, git_dir, reset=True)

    def _replace(
        self,
        target: str,
        source: str,
        branch: str,
        created_from: str,
        git_dir: Path,
        *,
        reset: bool,
    ) -> None:
        if target == source or target == self._params["dbname"]:
            raise SnapshotError(
                "Cannot clone a database onto itself or replace the seed"
            )
        root = operations_directory(git_dir)
        try:
            with operation_scope(self._backend, self._params, root) as conn:
                resources = PostgresResources(conn, self._config)
                if reset and resources.identity(target) is not None:
                    self._require_owner(target, branch, git_dir)
                strategy = self._backend.detect_strategy(self._config)

                def build(stage: str) -> None:
                    if strategy.name == "template":
                        handle_active_connections(conn, source, self._config)
                        _create_from_template(conn, stage, source)
                    else:
                        _create_via_pgdump(
                            conn, self._backend, self._params, stage, source
                        )

                state = load_state(git_dir)
                state.databases[branch] = BranchDbEntry(
                    target, datetime.now(UTC).isoformat(), created_from
                )
                run_operation(
                    root,
                    resources,
                    action="reset" if reset else "create",
                    kind="database",
                    server=server_identity(self._params),
                    target=target,
                    names=lambda identifier: resource_names(self._params, identifier),
                    metadata=state_path(git_dir),
                    build=build,
                    after=lambda _: state_text(state),
                    replace=reset,
                )
        except psycopg.Error as e:
            raise SnapshotError(f"Branch database operation failed: {e}") from e

    def _require_owner(self, name: str, branch: str, git_dir: Path) -> None:
        entry = get_branch_db(git_dir, branch)
        if entry is None or entry.db_name != name or name == self._params["dbname"]:
            raise SnapshotError(
                f"Refusing to drop '{name}': it is not owned by branch '{branch}'"
            )

    def drop(self, name: str, branch: str, git_dir: Path) -> None:
        # Validate before connecting, then again while holding the operation lock.
        self._require_owner(name, branch, git_dir)
        root = operations_directory(git_dir)
        with operation_scope(self._backend, self._params, root) as conn:
            self._require_owner(name, branch, git_dir)
            state = load_state(git_dir)
            state.databases.pop(branch, None)
            run_operation(
                root,
                PostgresResources(conn, self._config),
                action="prune",
                kind="database",
                server=server_identity(self._params),
                target=name,
                names=lambda identifier: resource_names(self._params, identifier),
                metadata=state_path(git_dir),
                build=None,
                after=lambda _: state_text(state),
            )

    def list(self, git_dir: Path) -> list[tuple[str, BranchDbEntry, bool]]:
        return [
            (branch, entry, self.exists(entry.db_name))
            for branch, entry in load_state(git_dir).databases.items()
        ]


def _create_via_pgdump(
    conn: DbConnection,
    backend: DatabaseBackend,
    params: dict[str, str | int],
    target: str,
    source: str,
) -> None:
    pg_dump, pg_restore = shutil.which("pg_dump"), shutil.which("pg_restore")
    if not pg_dump or not pg_restore:
        raise ToolNotFoundError("pg_dump and pg_restore must be installed in PATH.")
    env = backend.build_subprocess_env(params)
    # An anonymous temporary file avoids pipe deadlocks and is removed on process exit.
    with tempfile.TemporaryFile() as dump:
        result = subprocess.run(
            [
                pg_dump,
                "-Fc",
                "--no-owner",
                "--no-privileges",
                "-d",
                client_dsn(params, source),
            ],
            stdout=dump,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            timeout=300,
        )
        if result.returncode:
            raise SnapshotError(f"pg_dump failed: {result.stderr.strip()}")
        dump.seek(0)
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(target)
            )
        )
        result = subprocess.run(
            [
                pg_restore,
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "-d",
                client_dsn(params, target),
            ],
            stdin=dump,
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
        if result.returncode:
            raise SnapshotError(f"pg_restore failed: {result.stderr.strip()}")
