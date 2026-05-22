from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest

from git_db.backends.postgresql.backend import PostgresqlBackend
from git_db.backends.postgresql.branch_db import PostgresBranchDbManager
from git_db.backends.postgresql.pgdump import PgDumpStrategy
from git_db.backends.postgresql.template import TemplateStrategy
from git_db.config import GitDbConfig
from tests._pg_helpers import build_url


@pytest.fixture
def make_config(pg_info: dict, tmp_path: Path) -> Callable[..., GitDbConfig]:
    """
    Factory for GitDbConfig parameterized on strategy/policy.
    """

    def _make(
        strategy: str = "template",
        policy: str = "terminate",
        *,
        mode: str = "shared",
        snapshot_dir: Path | None = None,
        force_terminate_timeout_ms: int = 2000,
    ) -> GitDbConfig:
        return GitDbConfig(
            database_url=build_url(pg_info),
            strategy=strategy,
            mode=mode,
            on_active_connections=policy,
            snapshot_dir=snapshot_dir or tmp_path / "snapshots",
            force_terminate_timeout_ms=force_terminate_timeout_ms,
        )

    return _make


@pytest.fixture
def maintenance_conn(pg_info: dict) -> Iterator[psycopg.Connection]:
    """
    Autocommit connection to the 'postgres' maintenance database.
    """
    conn = psycopg.connect(
        host=pg_info["host"],
        port=pg_info["port"],
        user=pg_info["user"],
        password=pg_info["password"] or None,
        dbname="postgres",
        autocommit=True,
    )
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def backend() -> PostgresqlBackend:
    return PostgresqlBackend()


@pytest.fixture
def pg_version(pg_info: dict, backend: PostgresqlBackend) -> int:
    return backend.get_engine_version(build_url(pg_info))


@pytest.fixture
def pgdump_strategy(backend: PostgresqlBackend, pg_version: int) -> PgDumpStrategy:
    return PgDumpStrategy(backend=backend, pg_version=pg_version)


@pytest.fixture
def template_strategy(backend: PostgresqlBackend) -> TemplateStrategy:
    return TemplateStrategy(backend=backend)


@pytest.fixture
def template_manager(
    backend: PostgresqlBackend,
    make_config: Callable[..., GitDbConfig],
) -> PostgresBranchDbManager:
    """
    Per-branch DB manager configured for template strategy.
    """
    config = make_config(strategy="template", mode="per-branch")
    return PostgresBranchDbManager(backend=backend, config=config)


@pytest.fixture
def pgdump_manager(
    backend: PostgresqlBackend,
    make_config: Callable[..., GitDbConfig],
) -> PostgresBranchDbManager:
    """
    Per-branch DB manager configured for pgdump strategy.
    """
    config = make_config(strategy="pgdump", mode="per-branch")
    return PostgresBranchDbManager(backend=backend, config=config)


@pytest.fixture
def git_dir(tmp_path: Path) -> Path:
    """
    A fake .git dir used for state tracking.
    """
    d = tmp_path / ".git"
    d.mkdir()
    return d
