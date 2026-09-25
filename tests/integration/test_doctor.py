from __future__ import annotations

import json
import uuid

import psycopg
import pytest
from psycopg import sql
from typer.testing import CliRunner

from db_git.cli import app
from db_git.config import write_config
from db_git.db import parse_database_url
from db_git.hook_script import render_hook_script
from tests._pg_helpers import build_url, get_names, seed_users

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("strategy", ["template", "pgdump"])
def test_doctor_checks_real_server_without_changing_data(
    pg_info, git_repo, monkeypatch, strategy
):
    monkeypatch.chdir(git_repo)
    url = build_url(pg_info)
    seed_users(url)
    write_config(git_repo, {"database_url": url, "strategy": strategy})
    hook = git_repo / ".git/hooks/post-checkout"
    hook.write_text(render_hook_script())
    hook.chmod(0o755)
    result = CliRunner().invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["ok"]
    assert not (git_repo / ".git/db-git").exists()
    assert get_names(url) == ["Alice", "Bob", "Charlie"]
    assert any(
        c["id"] == "permissions.createdb" and c["status"] == "ok"
        for c in report["checks"]
    )


def test_doctor_reports_restricted_role(
    pg_info, git_repo, maintenance_conn, monkeypatch
):
    seed_users(build_url(pg_info))
    role = "doctor_" + uuid.uuid4().hex[:12]
    maintenance_conn.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'test-only'").format(
            sql.Identifier(role)
        )
    )
    try:
        monkeypatch.chdir(git_repo)
        info = {**pg_info, "user": role, "password": "test-only"}
        write_config(
            git_repo,
            {
                "database_url": build_url(info),
                "strategy": "pgdump",
                "mode": "per-branch",
            },
        )
        result = CliRunner().invoke(app, ["doctor", "--json"])
        assert result.exit_code == 1, result.output
        checks = {c["id"]: c for c in json.loads(result.stdout)["checks"]}
        assert checks["connection.database"]["status"] == "ok"
        assert checks["permissions.createdb"]["status"] == "error"
        assert checks["permissions.terminate"]["status"] == "warning"
        assert checks["permissions.read"]["status"] == "error"
        assert "test-only" not in result.output
        with psycopg.connect(**parse_database_url(build_url(pg_info))) as conn:
            assert conn.execute(
                "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
            ).fetchone() == (1,)
    finally:
        maintenance_conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
