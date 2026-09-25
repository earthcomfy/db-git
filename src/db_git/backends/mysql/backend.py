from __future__ import annotations

from typing import TYPE_CHECKING

from db_git.backends import (
    BackendCapabilities,
    BranchDbManager,
    SnapshotStrategy,
    register_backend,
)
from db_git.backends.mysql.branch_db import MySQLBranchDbManager
from db_git.backends.mysql.connections import Connection, server_version, subprocess_env
from db_git.backends.mysql.dump import check_permissions, validate_source
from db_git.backends.mysql.objects import capture_objects
from db_git.backends.mysql.operations import generation
from db_git.backends.mysql.shared import MySQLDumpStrategy
from db_git.backends.mysql.urls import parse_url
from db_git.errors import ConfigError

if TYPE_CHECKING:
    from db_git.config import DbGitConfig


class MySQLBackend:
    engine = "mysql"
    max_identifier_length = 64
    capabilities = BackendCapabilities(
        modes=frozenset({"per-branch", "shared"}),
        strategies=frozenset({"mysqldump"}),
        can_terminate_connections=False,
        automatic_file_cleanup=False,
    )

    def apply_url_defaults(
        self, params: dict[str, str | int | None]
    ) -> dict[str, str | int]:
        return {k: v for k, v in params.items() if v is not None}

    def connect_maintenance(self, params: dict[str, str | int]) -> Connection:
        return Connection(params)

    def get_engine_version(self, url: str) -> str:
        conn = Connection(self.apply_url_defaults(parse_url(url)))
        try:
            return server_version(conn)
        finally:
            conn.close()

    def check_permissions(self, url: str) -> object:
        params = self.apply_url_defaults(parse_url(url))
        conn = Connection(params)
        try:
            check_permissions(
                conn, str(params["dbname"]), generation(str(params["dbname"]), "check")
            )
            validate_source(conn, str(params["dbname"]), str(params["dbname"]))
            capture_objects(conn, str(params["dbname"]))
        finally:
            conn.close()
        return None

    def detect_strategy(self, config: DbGitConfig) -> SnapshotStrategy:
        if (
            config.mode not in self.capabilities.modes
            or config.strategy != "mysqldump"
            or config.on_active_connections != "fail"
        ):
            raise ConfigError(
                "MySQL requires mysqldump strategy and fail connection policy."
            )
        return MySQLDumpStrategy()

    def branch_db_manager(self, config: DbGitConfig) -> BranchDbManager:
        self.detect_strategy(config)
        return MySQLBranchDbManager(config)

    def database_exists(self, url: str, name: str) -> bool:
        from db_git.config import DbGitConfig

        return MySQLBranchDbManager(DbGitConfig(database_url=url)).exists(name)

    def build_subprocess_env(self, params: dict[str, str | int]) -> dict[str, str]:
        return subprocess_env()


register_backend("mysql", MySQLBackend)
