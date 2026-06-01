from __future__ import annotations

import contextlib
from collections.abc import Callable

import psycopg
import pytest

from db_git.backends.postgresql.connections import (
    check_connections,
    handle_active_connections,
)
from db_git.config import DbGitConfig
from db_git.errors import ActiveConnectionsError, TerminationTimeout
from tests._pg_helpers import build_url, reconnect


@pytest.mark.integration
class TestConnections:
    def test_check_connections_finds_blocker_and_excludes_self(
        self,
        pg_info: dict,
        maintenance_conn: psycopg.Connection,
    ) -> None:
        """
        check_connections lists blockers but never includes the admin conn itself.
        """
        blocker = reconnect(build_url(pg_info))
        try:
            active = check_connections(maintenance_conn, pg_info["dbname"])
            assert len(active) >= 1
            pids = {c.pid for c in active}
            row = maintenance_conn.execute("SELECT pg_backend_pid()").fetchone()
            assert row is not None
            assert row[0] not in pids
        finally:
            blocker.close()

    def test_check_connections_empty_when_no_other_clients(
        self,
        pg_info: dict,
        maintenance_conn: psycopg.Connection,
    ) -> None:
        active = check_connections(maintenance_conn, pg_info["dbname"])
        assert active == []

    def test_fail_policy_raises_and_preserves_blocker(
        self,
        pg_info: dict,
        maintenance_conn: psycopg.Connection,
        make_config: Callable[..., DbGitConfig],
    ) -> None:
        blocker = reconnect(build_url(pg_info))
        try:
            with pytest.raises(ActiveConnectionsError):
                handle_active_connections(
                    maintenance_conn,
                    pg_info["dbname"],
                    make_config(policy="fail"),
                )
            # Blocker must still be alive (fail policy doesn't terminate).
            assert not blocker.closed
            cur = blocker.execute("SELECT 1")
            assert cur.fetchone() == (1,)
        finally:
            if not blocker.closed:
                blocker.close()

    def test_terminate_policy_kills_blocker_and_returns_true(
        self,
        pg_info: dict,
        maintenance_conn: psycopg.Connection,
        make_config: Callable[..., DbGitConfig],
    ) -> None:
        blocker = reconnect(build_url(pg_info))
        try:
            result = handle_active_connections(
                maintenance_conn,
                pg_info["dbname"],
                make_config(policy="terminate"),
            )
            assert result is True
            assert check_connections(maintenance_conn, pg_info["dbname"]) == []
        finally:
            self._close_quietly(blocker)

    def test_terminates_multiple_concurrent_blockers(
        self,
        pg_info: dict,
        maintenance_conn: psycopg.Connection,
        make_config: Callable[..., DbGitConfig],
    ) -> None:
        """
        terminate policy must kill all connections, not just one.
        """
        url = build_url(pg_info)
        blockers = [reconnect(url), reconnect(url), reconnect(url)]
        try:
            assert len(check_connections(maintenance_conn, pg_info["dbname"])) == 3
            handle_active_connections(
                maintenance_conn,
                pg_info["dbname"],
                make_config(policy="terminate"),
            )
            assert check_connections(maintenance_conn, pg_info["dbname"]) == []
        finally:
            for b in blockers:
                self._close_quietly(b)

    def test_terminate_timeout_raises_when_deadline_is_tiny(
        self,
        pg_info: dict,
        maintenance_conn: psycopg.Connection,
        make_config: Callable[..., DbGitConfig],
    ) -> None:
        """
        With force_terminate_timeout_ms=1, the deadline expires after the
        first sleep(0.1), so _terminate_all hits its while-else and raises
        TerminationTimeout even though pg_terminate_backend has already
        killed the blocker.
        """
        blocker = reconnect(build_url(pg_info))
        try:
            with pytest.raises(TerminationTimeout):
                handle_active_connections(
                    maintenance_conn,
                    pg_info["dbname"],
                    make_config(policy="terminate", force_terminate_timeout_ms=1),
                )
        finally:
            self._close_quietly(blocker)

    def test_allow_connections_restored_after_successful_terminate(
        self,
        pg_info: dict,
        maintenance_conn: psycopg.Connection,
        make_config: Callable[..., DbGitConfig],
    ) -> None:
        """
        After terminate succeeds, ALLOW_CONNECTIONS must be back to true.
        """
        blocker = reconnect(build_url(pg_info))
        try:
            handle_active_connections(
                maintenance_conn,
                pg_info["dbname"],
                make_config(policy="terminate"),
            )
        finally:
            self._close_quietly(blocker)

        # A brand-new connection must succeed.
        fresh = reconnect(build_url(pg_info))
        try:
            assert fresh.execute("SELECT 1").fetchone() == (1,)
        finally:
            fresh.close()

    def test_allow_connections_restored_after_timeout_error(
        self,
        pg_info: dict,
        maintenance_conn: psycopg.Connection,
        make_config: Callable[..., DbGitConfig],
    ) -> None:
        """
        Even when TerminationTimeout propagates, the `finally` in
        _terminate_all must re-enable ALLOW_CONNECTIONS. Otherwise the DB
        would be locked out for the rest of the session.
        """
        blocker = reconnect(build_url(pg_info))
        try:
            with pytest.raises(TerminationTimeout):
                handle_active_connections(
                    maintenance_conn,
                    pg_info["dbname"],
                    make_config(policy="terminate", force_terminate_timeout_ms=1),
                )
        finally:
            self._close_quietly(blocker)

        fresh = reconnect(build_url(pg_info))
        try:
            assert fresh.execute("SELECT 1").fetchone() == (1,)
        finally:
            fresh.close()

    @staticmethod
    def _close_quietly(conn: psycopg.Connection) -> None:
        if not conn.closed:
            with contextlib.suppress(psycopg.Error):
                conn.close()
