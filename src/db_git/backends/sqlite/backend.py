from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from db_git.backends import (
    BackendCapabilities,
    BranchDbManager,
    DbConnection,
    SnapshotStrategy,
    register_backend,
)
from db_git.backends.sqlite.branch_db import SQLiteBranchDbManager
from db_git.backends.sqlite.files import check_database
from db_git.backends.sqlite.urls import database_path
from db_git.errors import ConfigError

if TYPE_CHECKING:
    from db_git.config import DbGitConfig
    from db_git.storage import Checkpoint


class BackupStrategy:
    name = "backup"

    def save(
        self,
        db_url: str,
        branch: str,
        snapshot_dir: Path,
        config: DbGitConfig,
        *,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        raise ConfigError(
            "SQLite supports per-branch mode; use create/reset "
            "for backup-based branch files."
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
        raise ConfigError(
            "SQLite shared-mode restore is not supported; use per-branch reset."
        )

    def cleanup(
        self,
        branch: str,
        snapshot_dir: Path,
        config: DbGitConfig,
        *,
        checkpoint_id: str | None = None,
    ) -> None:
        raise ConfigError(
            "SQLite file cleanup is manual; close applications "
            "before removing retained files."
        )


class SQLiteBackend:
    engine = "sqlite"
    max_identifier_length = 63
    capabilities = BackendCapabilities(
        modes=frozenset({"per-branch"}),
        strategies=frozenset({"backup"}),
        can_terminate_connections=False,
        automatic_file_cleanup=False,
    )

    def apply_url_defaults(
        self, params: dict[str, str | int | None]
    ) -> dict[str, str | int]:
        name = params.get("dbname")
        if not name or not Path(str(name)).is_absolute():
            raise ConfigError("SQLite requires an absolute database file path.")
        return {"dbname": str(name)}

    def get_engine_version(self, url: str) -> str:
        check_database(database_path(url))
        return sqlite3.sqlite_version

    def detect_strategy(self, config: DbGitConfig) -> SnapshotStrategy:
        if config.mode not in self.capabilities.modes or config.strategy != "backup":
            raise ConfigError(
                "SQLite supports per-branch mode with the backup strategy."
            )
        return BackupStrategy()

    def branch_db_manager(self, config: DbGitConfig) -> BranchDbManager:
        self.detect_strategy(config)
        return SQLiteBranchDbManager(config)

    def database_exists(self, url: str, name: str) -> bool:
        if not Path(name).is_file():
            return False
        check_database(Path(name))
        return True

    def check_permissions(self, url: str) -> object:
        check_database(database_path(url))
        return None

    def connect_maintenance(self, params: dict[str, str | int]) -> DbConnection:
        raise ConfigError("SQLite has no maintenance server connection.")

    def build_subprocess_env(self, params: dict[str, str | int]) -> dict[str, str]:
        return dict(os.environ)


register_backend("sqlite", SQLiteBackend)
