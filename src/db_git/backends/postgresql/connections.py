from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING, NamedTuple

from psycopg import sql

from db_git.backends import DbConnection
from db_git.errors import ActiveConnectionsError, TerminationTimeout

if TYPE_CHECKING:
    from db_git.config import DbGitConfig


class ConnectionInfo(NamedTuple):
    pid: int
    application_name: str
    state: str
    query_start: str | None


def check_connections(conn: DbConnection, dbname: str) -> list[ConnectionInfo]:
    """
    Return list of active connections to the given database,
    excluding our own connection.
    """
    cur = conn.execute(
        "SELECT pid, application_name, state, query_start::text "
        "FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid()",
        (dbname,),
    )
    return [ConnectionInfo(*row) for row in cur.fetchall()]


def handle_active_connections(
    conn: DbConnection,
    dbname: str,
    config: DbGitConfig,
) -> bool:
    """
    Handle active connections based on config policy.
    """
    active = check_connections(conn, dbname)
    if not active:
        return True

    if config.on_active_connections == "fail":
        raise ActiveConnectionsError(
            f"{len(active)} active connection(s) to '{dbname}'. "
            "Stop your dev server or set on_active_connections = 'terminate'."
        )

    _terminate_all(conn, dbname, config.force_terminate_timeout_ms)
    return True


def _terminate_all(conn: DbConnection, dbname: str, timeout_ms: int = 5000) -> None:
    """
    Block new connections, terminate existing ones, then restore access.
    """
    db_ident = sql.Identifier(dbname)
    conn.execute(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(db_ident))
    try:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            remaining = check_connections(conn, dbname)
            if not remaining:
                break
            for c in remaining:
                conn.execute("SELECT pg_terminate_backend(%s)", (c.pid,))
            time.sleep(0.1)
        else:
            raise TerminationTimeout(
                f"Could not terminate all connections within {timeout_ms}ms"
            )
    finally:
        with contextlib.suppress(Exception):
            conn.execute(
                sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(db_ident)
            )
