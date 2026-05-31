from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

import psycopg

from git_db.backends import (
    BranchDbManager,
    DbConnection,
    SnapshotStrategy,
    register_backend,
)
from git_db.backends.postgresql.branch_db import PostgresBranchDbManager
from git_db.backends.postgresql.pgdump import PgDumpStrategy
from git_db.backends.postgresql.template import TemplateStrategy
from git_db.db import parse_database_url
from git_db.errors import ConfigError, DatabaseError

if TYPE_CHECKING:
    from git_db.config import GitDbConfig


@dataclass
class PgPermissions:
    """
    PostgreSQL role permissions relevant to git-db operations.
    """

    can_createdb: bool
    is_superuser: bool
    has_pg_signal_backend: bool


class PostgresqlBackend:
    """
    PostgreSQL backend implementing the DatabaseBackend protocol.
    """

    engine = "postgresql"
    max_identifier_length = 63
    _VALID_STRATEGIES: ClassVar[set[str]] = {"template", "pgdump"}

    def apply_url_defaults(
        self, params: dict[str, str | int | None]
    ) -> dict[str, str | int]:
        """
        Apply PostgreSQL-specific defaults to parsed URL parameters.
        """
        dbname = params.get("dbname")
        if not dbname:
            raise ConfigError("Database name is required in the connection URL.")

        port = params.get("port")
        return {
            "user": params.get("user") or "postgres",
            "password": params.get("password") or "",
            "host": params.get("host") or "localhost",
            "port": port if port is not None else 5432,
            "dbname": dbname,
        }

    def get_engine_version(self, url: str) -> int:
        """
        Return the major PostgreSQL version as an integer.
        """
        params = self.apply_url_defaults(parse_database_url(url))
        conn = self.connect_maintenance(params)
        try:
            cur = conn.execute("SHOW server_version_num")
            row = cur.fetchone()
            if not row:
                raise DatabaseError("SHOW server_version_num returned no rows")
            try:
                return int(row[0]) // 10000
            except (TypeError, ValueError) as e:
                raise DatabaseError(
                    f"Could not parse server_version_num: {row[0]!r}"
                ) from e
        finally:
            conn.close()

    def connect_maintenance(self, params: dict[str, str | int]) -> DbConnection:
        """
        Connect to the 'postgres' maintenance database for admin operations.
        """
        conninfo = (
            f"host={params['host']} port={params['port']} "
            f"user={params['user']} dbname=postgres"
        )
        password = str(params.get("password", ""))
        try:
            return psycopg.connect(conninfo, autocommit=True, password=password or None)
        except psycopg.Error as e:
            raise DatabaseError(
                f"Could not connect to PostgreSQL at "
                f"{params['host']}:{params['port']}: {e}"
            ) from e

    def build_subprocess_env(self, params: dict[str, str | int]) -> dict[str, str]:
        """
        Build environment dict with PGPASSWORD set for subprocess calls.
        """
        env = os.environ.copy()
        password = str(params.get("password", ""))
        if password:
            env["PGPASSWORD"] = password
        return env

    def check_permissions(self, url: str) -> PgPermissions:
        """
        Check the current user's PostgreSQL permissions.
        """
        params = self.apply_url_defaults(parse_database_url(url))
        try:
            conn = self.connect_maintenance(params)
        except DatabaseError:
            return PgPermissions(False, False, False)
        try:
            cur = conn.execute(
                "SELECT rolcreatedb, rolsuper, "
                "pg_has_role(current_user, 'pg_signal_backend', 'MEMBER') "
                "FROM pg_roles WHERE rolname = current_user"
            )
            row = cur.fetchone()
            if not row:
                return PgPermissions(False, False, False)
            return PgPermissions(
                can_createdb=bool(row[0]),
                is_superuser=bool(row[1]),
                has_pg_signal_backend=bool(row[2]),
            )
        except psycopg.Error:
            return PgPermissions(False, False, False)
        finally:
            conn.close()

    def database_exists(self, url: str, name: str) -> bool:
        """
        Return whether a PostgreSQL database exists.
        """
        params = self.apply_url_defaults(parse_database_url(url))
        conn = self.connect_maintenance(params)
        try:
            cur = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            return cur.fetchone() is not None
        except psycopg.Error as e:
            raise DatabaseError(f"Could not inspect database '{name}': {e}") from e
        finally:
            conn.close()

    def detect_strategy(self, config: GitDbConfig) -> SnapshotStrategy:
        """
        Return the configured snapshot strategy for this PostgreSQL instance.
        """
        if config.strategy not in self._VALID_STRATEGIES:
            raise ConfigError(
                f"Unknown strategy '{config.strategy}'. "
                f"Valid strategies for PostgreSQL: "
                f"{', '.join(sorted(self._VALID_STRATEGIES))}."
            )

        if config.strategy == "pgdump":
            pg_version = self.get_engine_version(config.database_url)
            return PgDumpStrategy(backend=self, pg_version=pg_version)
        return TemplateStrategy(backend=self)

    def branch_db_manager(self, config: GitDbConfig) -> BranchDbManager:
        """
        Return the per-branch database manager for this PostgreSQL instance.
        """
        return PostgresBranchDbManager(backend=self, config=config)


register_backend("postgresql", PostgresqlBackend)
