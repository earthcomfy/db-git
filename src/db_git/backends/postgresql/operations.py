from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from psycopg import sql

from db_git.backends import DatabaseBackend, DbConnection
from db_git.backends.postgresql.connections import handle_active_connections
from db_git.config import DbGitConfig
from db_git.errors import DbGitError
from db_git.files import local_lock
from db_git.recovery import Operation, require_recovered
from db_git.resources import transition

_local = threading.local()


def server_identity(params: dict[str, str | int]) -> str:
    identity = [str(params.get(k, "")) for k in ("host", "port", "dbname")]
    endpoint = [
        params.get("hostaddr"),
        params.get("service") or os.environ.get("PGSERVICE"),
    ]
    if any(endpoint):
        identity.extend(str(value or "") for value in endpoint)
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def resource_names(params: dict[str, str | int], identifier: str) -> tuple[str, str]:
    prefix = str(params["dbname"]).encode()[:36].decode(errors="ignore")
    return (
        f"{prefix}__dbgit_s_{identifier[:16]}",
        f"{prefix}__dbgit_b_{identifier[:16]}",
    )


@contextmanager
def operation_scope(
    backend: DatabaseBackend,
    params: dict[str, str | int],
    root: Path,
    *,
    recovering: bool = False,
) -> Iterator[DbConnection]:
    """Serialize local metadata and server resources, including nested hook calls."""
    with local_lock(root):
        if not recovering:
            require_recovered(root)
        held = getattr(_local, "held", None)
        if held is None:
            held = _local.held = {}
        key = (os.getpid(), server_identity(params))
        if key in held:
            yield held[key]
            return
        conn = backend.connect_maintenance(params)
        lock_key = int.from_bytes(
            hashlib.sha256(str(params["dbname"]).encode()).digest()[:8],
            "big",
            signed=True,
        )
        try:
            row = conn.execute(
                "SELECT pg_try_advisory_lock(%s)", (lock_key,)
            ).fetchone()
            if not row or not row[0]:
                raise DbGitError(
                    "Another db-git operation is using this database; retry later."
                )
            held[key] = conn
            try:
                yield conn
            finally:
                held.pop(key, None)
        finally:
            conn.close()


class PostgresResources:
    def __init__(self, conn: DbConnection, config: DbGitConfig) -> None:
        self.conn = conn
        self.config = config

    def identity(self, name: str) -> str | None:
        row = self.conn.execute(
            "SELECT oid FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
        return str(row[0]) if row else None

    def publish(self, record: Operation) -> None:
        self._move(transition(self, record, undo=False))

    def rollback(self, record: Operation) -> None:
        self._move(transition(self, record, undo=True))

    def _move(self, pairs: list[tuple[str, str]]) -> None:
        for source, _ in pairs:
            handle_active_connections(self.conn, source, self.config)
        self.conn.execute("BEGIN")
        try:
            for source, target in pairs:
                self.conn.execute(
                    sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                        sql.Identifier(source), sql.Identifier(target)
                    )
                )
            self.conn.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(Exception):
                self.conn.execute("ROLLBACK")
            raise

    def remove(self, name: str, identity: str | None) -> None:
        actual = self.identity(name)
        if actual is None:
            return
        if identity is None or actual != identity:
            raise DbGitError(f"Resource identity changed: {name}")
        handle_active_connections(self.conn, name, self.config)
        self.conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
