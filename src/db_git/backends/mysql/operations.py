"""Publish MySQL generations through ownership metadata, retaining every schema."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from db_git.backends.mysql.connections import Connection, server_version
from db_git.errors import DbGitError
from db_git.files import local_lock
from db_git.recovery import Operation, require_recovered
from db_git.storage import sanitize_branch_name

MARKER = "__dbgit_generation"
_local = threading.local()


def prefix(seed: str) -> str:
    return "_dbgit_" + hashlib.sha256(seed.encode()).hexdigest()[:12] + "_"


def generation(seed: str, branch: str, token: str | None = None) -> str:
    return (
        prefix(seed)
        + sanitize_branch_name(branch, 8).lower()
        + "_"
        + (token or uuid.uuid4().hex)
    )


def validate_generation(name: str, seed: str) -> None:
    if not re.fullmatch(re.escape(prefix(seed)) + r"[a-z0-9_]{1,8}_[a-f0-9]{32}", name):
        raise DbGitError(
            "MySQL ownership points outside this seed's managed generations."
        )


def server_identity(conn: Connection, seed: str) -> str:
    server = str(conn.execute("SELECT @@server_uuid").fetchone()[0])
    return hashlib.sha256(f"mysql:{server}:{seed}".encode()).hexdigest()


@contextmanager
def operation_scope(
    params: dict[str, str | int], root: Path, *, recovering: bool = False
) -> Iterator[Connection]:
    with local_lock(root):
        if not recovering:
            require_recovered(root)
        held = getattr(_local, "held", None)
        if held is None:
            held = _local.held = {}
        key = (
            os.getpid(),
            str(params["host"]),
            str(params["port"]),
            str(params["dbname"]),
        )
        if key in held:
            yield held[key]
            return
        conn = Connection(params)
        try:
            server_version(conn)
            # The server lock also serializes repositories using the same seed.
            lock = (
                "dbgit:"
                + hashlib.sha256(str(params["dbname"]).encode()).hexdigest()[:56]
            )
            if conn.execute("SELECT GET_LOCK(%s, 5)", (lock,)).fetchone() != (1,):
                raise DbGitError("Another db-git operation holds the MySQL seed lock.")
            held[key] = conn
            try:
                yield conn
            finally:
                held.pop(key, None)
        finally:
            # Closing releases named locks, including after failed operations.
            conn.close()


class MySQLResources:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    def identity(self, name: str) -> str | None:
        row = self.conn.execute(
            "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s",
            (name,),
        ).fetchone()
        if row is None:
            return None
        row = self.conn.execute(
            "SELECT TABLE_COMMENT FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
            (name, MARKER),
        ).fetchone()
        if (
            row is None
            or not re.fullmatch(r"dbgit:[a-f0-9]{32}", str(row[0]))
            or str(row[0]) != "dbgit:" + name.rsplit("_", 1)[-1]
        ):
            return "unmanaged"
        return str(row[0])

    def publish(self, record: Operation) -> None:
        if self.identity(record.target) != record.original_id:
            raise DbGitError("Previous MySQL generation changed; refusing publication.")
        if record.action != "drop" and (
            record.stage_id in {None, "unmanaged"}
            or self.identity(record.stage) != record.stage_id
        ):
            raise DbGitError(
                "MySQL generation changed or is missing; refusing publication."
            )
        # No schema rename is necessary: record.after selects the fully restored stage.

    def rollback(self, record: Operation) -> None:
        if self.identity(record.target) != record.original_id:
            raise DbGitError(
                "Previous MySQL generation is missing or changed; preserve the journal."
            )
        stage = self.identity(record.stage)
        if stage is not None and stage != record.stage_id:
            raise DbGitError("Staged MySQL generation changed; preserve the journal.")
        # Restore only ownership metadata; existing clients may still use either copy.

    def remove(self, name: str, identity: str | None) -> None:
        # Discard never drops a database that an application might still be using.
        return
