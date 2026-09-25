from __future__ import annotations

import os
import time
import uuid
from contextlib import closing

import pytest
from testcontainers.core.container import DockerContainer
from tests._pg_helpers import run_db_git

from db_git.backends.mysql.connections import Connection, identifier
from db_git.backends.mysql.operations import prefix
from db_git.errors import DatabaseError


@pytest.fixture(scope="session")
def mysql_server():
    pytest.importorskip("pymysql")
    image = os.environ.get("DB_GIT_TEST_MYSQL_IMAGE", "mysql:8.4")
    with (
        DockerContainer(image)
        .with_env("MYSQL_ROOT_PASSWORD", "dbgit-test-only")
        .with_env("MYSQL_ROOT_HOST", "%")
        .with_exposed_ports(3306)
    ) as container:
        deadline = time.monotonic() + 30
        while True:
            try:
                port = int(container.get_exposed_port(3306))
                break
            except ConnectionError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
        params = {
            "host": container.get_container_host_ip(),
            "port": port,
            "user": "root",
            "password": "dbgit-test-only",
            "connect_timeout": 2,
            "ssl_mode": "REQUIRED",
        }
        deadline = time.monotonic() + 120
        while True:
            try:
                with closing(Connection(params)) as conn:
                    conn.execute("SELECT 1")
                break
            except DatabaseError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
        yield params


@pytest.fixture
def project(mysql_server, git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    for key in os.environ:
        if key.startswith("DB_GIT_") or key == "DATABASE_URL":
            monkeypatch.delenv(key)
    params = {**mysql_server, "dbname": "seed_" + uuid.uuid4().hex[:12]}
    seed = params["dbname"]
    url = f"mysql://root:dbgit-test-only@{params['host']}:{params['port']}/{seed}"
    with closing(Connection(params)) as conn:
        conn.execute(
            f"CREATE DATABASE {identifier(seed)} "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        conn.execute(
            f"CREATE TABLE {identifier(seed)}.users "
            "(id INT PRIMARY KEY AUTO_INCREMENT, name VARCHAR(100), "
            "payload BLOB) ENGINE=InnoDB"
        )
        conn.execute(
            f"INSERT INTO {identifier(seed)}.users (name, payload) VALUES (%s, %s)",
            ("seed é 🐈", b"\x00\xff\x27"),
        )
        env = dict(os.environ)
        run_db_git("init", "--database-url", url, cwd=git_repo, env=env)
        try:
            yield git_repo, params, url, env, conn
        finally:
            databases = conn.execute("SHOW DATABASES").fetchall()
            for (name,) in databases:
                if name == seed or name.startswith(prefix(seed)):
                    conn.execute(f"DROP DATABASE {identifier(name)}")
