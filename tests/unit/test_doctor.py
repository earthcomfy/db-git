from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, Mock

import psycopg
import pytest
from typer.testing import CliRunner

from db_git.cli import app
from db_git.config import write_config
from db_git.doctor import Report, _check_versions
from db_git.hook_script import render_hook_script
from db_git.recovery import Operation
from db_git.state import BranchDbEntry, DbGitState, save_state
from db_git.storage import make_metadata, write_metadata


@pytest.fixture
def project(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    for name in (
        "DATABASE_URL",
        "DB_GIT_DATABASE_URL",
        "DB_GIT_STRATEGY",
        "DB_GIT_SKIP",
    ):
        monkeypatch.delenv(name, raising=False)
    write_config(
        git_repo, {"database_url": "postgresql:///app", "strategy": "template"}
    )
    hook = git_repo / ".git/hooks/post-checkout"
    hook.write_text(render_hook_script())
    hook.chmod(0o755)
    return git_repo


@pytest.fixture
def online(monkeypatch):
    conn = Mock()

    def execute(query, params=None):
        cursor = Mock()
        if query == "SHOW server_version_num":
            cursor.fetchone.return_value = (140000,)
        elif "rolcreatedb" in query:
            cursor.fetchone.return_value = (True, True, True)
        elif "FROM pg_database" in query:
            cursor.fetchall.return_value = [("app", True, True, False)]
        return cursor

    conn.execute.side_effect = execute
    monkeypatch.setattr(
        "db_git.doctor.PostgresqlBackend.connect_maintenance", lambda *args: conn
    )
    connect = MagicMock()
    monkeypatch.setattr("db_git.doctor.psycopg.connect", connect)
    return conn, connect


def run_doctor(*args):
    result = CliRunner().invoke(app, ["doctor", "--json", *args])
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        result.exception
    )
    data = json.loads(result.stdout)
    assert data["exit_code"] == result.exit_code
    return result.exit_code, {item["id"]: item for item in data["checks"]}


def test_healthy_json_is_read_only(project, online):
    def files():
        return {str(p): p.read_bytes() for p in project.rglob("*") if p.is_file()}

    before = files()
    code, checks = run_doctor()
    assert code == 0
    assert checks["configuration"]["status"] == "ok"
    assert checks["state"]["status"] == "ok"
    assert checks["clients"]["status"] == "skipped"
    assert files() == before
    assert online[0].close.call_count == 1
    assert all(
        call.args[0].startswith(("SELECT", "SHOW", "SET"))
        for call in online[0].execute.call_args_list
    )


def test_warning_exit_policy(project, online):
    marker = project / ".git/db-git/disabled"
    marker.parent.mkdir()
    marker.write_text("disabled")
    assert run_doctor()[0] == 0
    assert run_doctor("--strict")[0] == 1


@pytest.mark.parametrize(
    "config",
    [
        'database_url = "secret',
        'database_url = "postgresql://user:secret@host/db?password=secret&bogus=secret"\nstrategy="template"',
        'database_url = "postgresql:///db"\nstrategy="template"\nmax_snapshots=0',
    ],
)
def test_invalid_configuration_json_never_exposes_secrets(project, config):
    (project / ".db-git.toml").write_text(config)
    result = CliRunner().invoke(app, ["doctor", "--json"])
    assert result.exit_code == 2
    assert "secret" not in result.output
    assert json.loads(result.stdout)["exit_code"] == 2


def test_failed_connections_still_inspect_local_state(project, monkeypatch):
    monkeypatch.setattr(
        "db_git.doctor.PostgresqlBackend.connect_maintenance",
        Mock(side_effect=psycopg.OperationalError("password=secret")),
    )
    monkeypatch.setattr(
        "db_git.doctor.psycopg.connect",
        Mock(side_effect=psycopg.OperationalError("password=secret")),
    )
    code, checks = run_doctor()
    assert code == 1
    assert checks["state"]["status"] == "ok"
    assert checks["state.databases"]["status"] == "skipped"
    assert "secret" not in json.dumps(checks)


def test_custom_hook_location_is_checked(project, online):
    subprocess.run(["git", "config", "core.hooksPath", ".custom-hooks"], check=True)
    code, checks = run_doctor()
    assert code == 1
    assert checks["hooks.installed"]["status"] == "error"
    directory = project / ".custom-hooks"
    directory.mkdir()
    hook = directory / "post-checkout"
    hook.write_text(render_hook_script())
    hook.chmod(0o755)
    assert run_doctor()[0] == 0


def test_pending_recovery_is_failure(project, online):
    root = project / ".git/db-git/snapshots/.operations"
    root.mkdir(parents=True)
    Operation(
        "a" * 32,
        "save",
        "database",
        "server",
        "target",
        "stage",
        "backup",
        None,
        None,
        None,
        None,
    ).write(root)
    code, checks = run_doctor()
    assert code == 1
    assert checks["recovery"]["status"] == "error"
    assert "db-git recover" in checks["recovery"]["remedy"]


@pytest.mark.parametrize(
    "problem", ["missing", "duplicate", "seed", "malformed", "mode"]
)
def test_state_mismatches(project, online, problem):
    write_config(project, {"mode": "per-branch"})
    state = DbGitState()
    state.databases["feature"] = BranchDbEntry("missing_db", "now", "main")
    if problem == "duplicate":
        state.databases["other"] = state.databases["feature"]
    if problem == "seed":
        state.databases["feature"].db_name = "app"
    if problem == "mode":
        state.mode = "shared"
    save_state(project / ".git", state)
    if problem == "malformed":
        (project / ".git/db-git/state.json").write_text("[]")
    code, checks = run_doctor()
    assert code == 1
    assert any(
        item["status"] == "error"
        for key, item in checks.items()
        if key.startswith("state")
    )


def test_missing_dump(project, online):
    write_config(project, {"strategy": "pgdump"})
    directory = project / ".git/db-git/snapshots"
    write_metadata(
        directory, make_metadata("main", "app", "pgdump", "postgresql", "14")
    )
    code, checks = run_doctor()
    assert code == 1
    assert checks["state"]["status"] == "error"


@pytest.mark.parametrize(
    "dump,restore,server,status",
    [
        (14, 14, 14, "ok"),
        (13, 14, 14, "error"),
        (16, 14, 14, "error"),
        (16, 16, 14, "warning"),
    ],
)
def test_client_version_compatibility(dump, restore, server, status):
    report = Report()
    _check_versions(report, {"pg_dump": dump, "pg_restore": restore}, server)
    assert report.checks[0].status == status


def test_missing_clients(project, online, monkeypatch):
    write_config(project, {"strategy": "pgdump"})
    monkeypatch.setattr("db_git.doctor.shutil.which", lambda name: None)
    code, checks = run_doctor()
    assert code == 1
    assert checks["clients.pg_dump"]["status"] == "error"
    assert checks["clients.pg_restore"]["status"] == "error"


def test_cli_url_honors_prefixed_environment_and_explicit_override(
    project, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", "postgresql:///generic")
    monkeypatch.setenv("DB_GIT_DATABASE_URL", "postgresql:///specific")
    runner = CliRunner()
    result = runner.invoke(app, ["url"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip().startswith("postgresql:///specific?")
    result = runner.invoke(app, ["url", "--database-url", "postgresql:///explicit"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip().startswith("postgresql:///explicit?")


def test_environment_only_setup_still_needs_init(project, online, monkeypatch):
    (project / ".db-git.toml").unlink()
    monkeypatch.setenv("DB_GIT_DATABASE_URL", "postgresql:///app")
    monkeypatch.setenv("DB_GIT_STRATEGY", "template")
    code, checks = run_doctor()
    assert code == 1
    assert checks["configuration.file"]["status"] == "error"
    assert checks["connection.database"]["status"] == "ok"
