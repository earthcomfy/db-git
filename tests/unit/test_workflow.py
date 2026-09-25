from __future__ import annotations

from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from db_git.backends.postgresql.backend import PostgresqlBackend
from db_git.cli import app
from db_git.config import write_config
from db_git.db import parse_database_url
from db_git.recovery import Operation
from db_git.state import record_branch_db


@pytest.fixture
def project(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    monkeypatch.delenv("DB_GIT_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql:///application_not_seed")
    write_config(
        git_repo,
        {
            "database_url": "postgresql:///seed?application_name=app",
            "strategy": "template",
            "mode": "per-branch",
        },
    )
    return git_repo


@pytest.fixture
def backend(monkeypatch):
    backend = Mock(wraps=PostgresqlBackend())
    backend.max_identifier_length = 63
    backend.database_exists.return_value = True
    manager = Mock()
    manager.exists.return_value = False
    backend.branch_db_manager.return_value = manager
    monkeypatch.setattr("db_git.cli.branch.get_backend", lambda _: backend)
    monkeypatch.setattr("db_git.cli.run.get_backend", lambda _: backend)
    return backend


def test_run_passes_arguments_environment_and_keeps_seed(project, backend, monkeypatch):
    record_branch_db(project / ".git", "feature", "legacy_db", "main")
    monkeypatch.setattr("db_git.cli.run.get_current_branch", lambda: "feature")
    execute = Mock()
    monkeypatch.setattr("db_git.cli.run.os.execvpe", execute)
    result = CliRunner().invoke(
        app, ["run", "--", "app", "--help", "a b", "--", "$(literal)"]
    )
    assert result.exit_code == 0, result.output
    name, args, env = execute.call_args.args
    assert name == "app"
    assert args == ["app", "--help", "a b", "--", "$(literal)"]
    actual = parse_database_url(env["DATABASE_URL"])
    assert actual["dbname"] == "legacy_db"
    assert actual["application_name"] == "app"
    assert actual["user"] == "postgres"
    assert actual["host"] == "localhost"
    assert actual["port"] == 5432
    assert env["DB_GIT_DATABASE_URL"] == "postgresql:///seed?application_name=app"


@pytest.mark.parametrize(
    "problem", ["untracked", "missing", "detached", "pending", "duplicate"]
)
def test_run_refuses_unsafe_database(project, backend, monkeypatch, problem):
    monkeypatch.setattr("db_git.cli.run.get_current_branch", lambda: "feature")
    if problem != "untracked":
        record_branch_db(project / ".git", "feature", "branch_db", "main")
    if problem == "missing":
        backend.database_exists.return_value = False
    elif problem == "detached":
        monkeypatch.setattr("db_git.cli.run.get_current_branch", lambda: None)
    elif problem == "duplicate":
        record_branch_db(project / ".git", "other", "branch_db", "main")
    elif problem == "pending":
        Operation(
            "a" * 32,
            "reset",
            "database",
            "server",
            "target",
            "stage",
            "backup",
            None,
            None,
            None,
            None,
        ).write(project / ".git/db-git/operations")
    execute = Mock()
    monkeypatch.setattr("db_git.cli.run.os.execvpe", execute)
    result = CliRunner().invoke(app, ["run", "--", "app"])
    assert result.exit_code == 1, result.output
    execute.assert_not_called()
    backend.branch_db_manager.return_value.create.assert_not_called()


def test_shared_disabled_run_does_not_start_application(project, backend, monkeypatch):
    write_config(project, {"mode": "shared"})
    marker = project / ".git/db-git/disabled"
    marker.parent.mkdir()
    marker.write_text("failed switch")
    execute = Mock()
    monkeypatch.setattr("db_git.cli.run.os.execvpe", execute)
    result = CliRunner().invoke(app, ["run", "--", "app"])
    assert result.exit_code == 1
    assert "disabled" in result.output
    execute.assert_not_called()


@pytest.mark.parametrize(
    "error,code", [(FileNotFoundError(), 127), (PermissionError(), 126)]
)
def test_run_exec_errors(project, backend, monkeypatch, error, code):
    monkeypatch.setattr("db_git.cli.run.os.execvpe", Mock(side_effect=error))
    result = CliRunner().invoke(app, ["run", "--", "some-command"])
    assert result.exit_code == code, result.output


def test_run_requires_command(project):
    assert CliRunner().invoke(app, ["run"]).exit_code == 2


def test_create_from_recorded_source_not_current_branch(project, backend):
    record_branch_db(project / ".git", "source", "legacy_source", "main")
    manager = backend.branch_db_manager.return_value
    manager.exists.side_effect = lambda name: name == "legacy_source"
    result = CliRunner().invoke(app, ["create", "target", "--from", "source"])
    assert result.exit_code == 0, result.output
    args = manager.create.call_args.args
    assert args[1:4] == ("legacy_source", "target", "source")


@pytest.mark.parametrize("source", ["unknown", "missing", "duplicate"])
def test_explicit_source_never_falls_back(project, backend, source):
    if source != "unknown":
        record_branch_db(project / ".git", source, "source_db", "main")
    if source == "duplicate":
        record_branch_db(project / ".git", "other", "source_db", "main")
    manager = backend.branch_db_manager.return_value
    result = CliRunner().invoke(app, ["create", "target", "--from", source])
    assert result.exit_code == 1, result.output
    manager.create.assert_not_called()


def test_create_from_default_branch_explicitly_uses_seed(project, backend, monkeypatch):
    record_branch_db(project / ".git", "current", "current_db", "main")
    monkeypatch.setattr("db_git.cli.branch.get_current_branch", lambda: "current")
    manager = backend.branch_db_manager.return_value
    manager.exists.side_effect = lambda name: name in {"seed", "current_db"}
    result = CliRunner().invoke(app, ["create", "target", "--from", "main"])
    assert result.exit_code == 0, result.output
    assert manager.create.call_args.args[1:4] == ("seed", "target", "main")


def test_create_from_rejects_shared_mode(project, backend):
    write_config(project, {"mode": "shared"})
    result = CliRunner().invoke(app, ["create", "target", "--from", "main"])
    assert result.exit_code == 1
    backend.branch_db_manager.return_value.create.assert_not_called()


def test_configured_seed_is_preserved_by_cli(project, backend, monkeypatch):
    result = CliRunner().invoke(app, ["url"])
    assert result.exit_code == 0, result.output
    assert "application_not_seed" not in result.output
    assert "seed" in result.output
    # Dedicated override still deliberately selects a different seed.
    monkeypatch.setenv("DB_GIT_DATABASE_URL", "postgresql:///override")
    result = CliRunner().invoke(app, ["url"])
    assert result.exit_code == 0
    assert "override" in result.output


def test_run_invalid_url_does_not_expose_credentials(project, monkeypatch):
    write_config(project, {"database_url": "postgresql://user:secret@[invalid/db"})
    execute = Mock()
    monkeypatch.setattr("db_git.cli.run.os.execvpe", execute)
    result = CliRunner().invoke(app, ["run", "--", "app"])
    assert result.exit_code == 1
    assert "secret" not in result.output
    assert "db-git doctor" in result.output
    execute.assert_not_called()


def test_run_rejects_invalid_recorded_name(project, backend, monkeypatch):
    import json

    record_branch_db(project / ".git", "feature", "valid_db", "main")
    path = project / ".git/db-git/state.json"
    data = json.loads(path.read_text())
    data["databases"]["feature"]["db_name"] = 123
    path.write_text(json.dumps(data))
    monkeypatch.setattr("db_git.cli.run.get_current_branch", lambda: "feature")
    execute = Mock()
    monkeypatch.setattr("db_git.cli.run.os.execvpe", execute)
    result = CliRunner().invoke(app, ["run", "--", "app"])
    assert result.exit_code == 1
    assert "Invalid database name" in result.output
    execute.assert_not_called()
