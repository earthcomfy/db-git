from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from filelock import FileLock
from testcontainers.postgres import PostgresContainer

from tests._pg_helpers import admin_exec, drop_db_and_branches

# Disable testcontainers' Ryuk reaper before importing the library. Ryuk binds
# a fixed port per Docker host; under xdist each worker that tries to start a
# container would attempt to spin up its own Ryuk, causing port-8080 races.
# Our filelock-shared container pattern doesn't need Ryuk because the fixture's own
# teardown handles cleanup.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """
    Initialize a git repo with an initial commit.
    """
    subprocess.run(
        ["git", "init"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    readme = tmp_path / "README.md"
    readme.write_text("# test repo\n")
    subprocess.run(
        ["git", "add", "."],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Postgres via testcontainers: shared by integration/ and e2e/ layers.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PgConn:
    """
    Attachment info for a testcontainers Postgres, sharable across xdist workers.
    """

    host: str
    port: int
    username: str
    password: str


@pytest.fixture(scope="session")
def postgres_image() -> str:
    """
    PG image to test against. Precedence:
    1. GITDB_TEST_PG_IMAGE env var (set by CI matrix)
    2. Match the host pg_dump major version so local tests don't hit a
       server-version-newer-than-client mismatch
    3. Fall back to postgres:16
    """
    if env_image := os.environ.get("GITDB_TEST_PG_IMAGE"):
        return env_image
    if host_major := _detect_host_pg_dump_major():
        return f"postgres:{host_major}"
    return "postgres:16"


@pytest.fixture(scope="session")
def postgres_container(
    postgres_image: str,
    worker_id: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[PgConn]:
    """
    Postgres testcontainer shared across xdist workers.

    The first worker to acquire the filelock starts the container and
    records attachment info to a shared JSON file; other workers read the
    file and attach. In non-xdist runs (worker_id == "master"), this
    simplifies to a normal session-scoped container.
    """
    if worker_id == "master":
        with PostgresContainer(postgres_image, driver=None) as container:
            yield _attach(container)
        return

    root = tmp_path_factory.getbasetemp().parent
    info_file = root / "gitdb_pg_container.json"
    lock_file = str(info_file) + ".lock"

    with FileLock(lock_file):
        if info_file.is_file():
            data = json.loads(info_file.read_text())
            yield PgConn(**data)
            return

        with PostgresContainer(postgres_image, driver=None) as container:
            conn = _attach(container)
            info_file.write_text(json.dumps(conn.__dict__))
            try:
                yield conn
            finally:
                info_file.unlink(missing_ok=True)


@pytest.fixture
def pg_info(postgres_container: PgConn, worker_id: str) -> Iterator[dict]:
    """
    Per-test database, namespaced by xdist worker_id + random suffix.

    Creates the DB via a transient maintenance connection, yields connection
    params as a dict, then drops the DB and any branch DBs the test may have
    left behind (pattern: {dbname}__%).
    """
    host = postgres_container.host
    port = postgres_container.port
    user = postgres_container.username
    password = postgres_container.password
    dbname = f"gitdb_test_{worker_id}_{uuid.uuid4().hex[:8]}"

    admin_exec(host, port, user, password, f'CREATE DATABASE "{dbname}"')

    try:
        yield {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "dbname": dbname,
        }
    finally:
        drop_db_and_branches(host, port, user, password, dbname)


def _attach(container: PostgresContainer) -> PgConn:
    return PgConn(
        host=container.get_container_host_ip(),
        port=int(container.get_exposed_port(5432)),
        username=container.username,
        password=container.password or "",
    )


def _detect_host_pg_dump_major() -> int | None:
    """
    Return the host pg_dump major version, or None if not found.
    """
    if not shutil.which("pg_dump"):
        return None
    result = subprocess.run(
        ["pg_dump", "--version"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        return None
    match = re.search(r"(\d+)", result.stdout)
    return int(match.group(1)) if match else None
