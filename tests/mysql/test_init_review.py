from __future__ import annotations

import sqlite3

import pytest
from tests.mysql.test_mysql import command

from db_git.backends.sqlite.urls import database_url
from db_git.config import load_config


@pytest.mark.parametrize("engine", ["postgresql", "sqlite"])
@pytest.mark.parametrize("resource", ["mysql-active.json", ".operations/pending.json"])
def test_shared_resources_prevent_engine_change(project, engine, resource):
    command(project, "init", "--mode", "shared")
    root = project[0]
    path = load_config().snapshot_dir / resource
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("retained resource")
    before = (root / ".db-git.toml").read_bytes()
    url = "postgresql:///unused"
    if engine == "sqlite":
        seed = root / "other.sqlite3"
        with sqlite3.connect(seed) as conn:
            conn.execute("CREATE TABLE example (id INTEGER)")
        url = database_url(seed)
    result = command(project, "init", "--database-url", url, check=False)
    assert result.returncode == 1
    assert "separate repository" in result.stderr
    assert (root / ".db-git.toml").read_bytes() == before
    assert path.read_text() == "retained resource"


def test_mysql_reinit_can_change_connection_options_with_owned_resources(project):
    command(project, "create", "feature")
    url = project[2]
    separator = "&" if "?" in url else "?"
    updated = url + separator + "connect_timeout=10"
    command(project, "init", "--database-url", updated)
    assert load_config().database_url == updated
    assert command(project, "url", "feature").returncode == 0
