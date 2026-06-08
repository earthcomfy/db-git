from __future__ import annotations

import os
import tomllib
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from db_git.backends.postgresql.backend import PgPermissions, PostgresqlBackend
from db_git.backends.postgresql.branch_db import PostgresBranchDbManager
from db_git.backends.postgresql.template import TemplateStrategy
from db_git.cli import app
from db_git.errors import DatabaseError, SnapshotError
from db_git.hook_script import render_hook_script
from db_git.state import record_branch_db
from db_git.storage import has_snapshot, make_metadata, write_metadata

runner = CliRunner()


@pytest.fixture
def setup_config() -> Callable[..., None]:
    """
    Factory: write .db-git.toml with optional mode/strategy overrides.
    """

    def _setup(
        path: Path,
        *,
        mode: str = "shared",
        strategy: str = "template",
    ) -> None:
        (path / ".git").mkdir(exist_ok=True)
        (path / ".db-git.toml").write_text(
            'database_url = "postgresql://postgres:postgres@localhost:5432/testdb"\n'
            f'strategy = "{strategy}"\n'
            f'mode = "{mode}"\n'
        )

    return _setup


@pytest.fixture
def setup_with_snapshots(
    setup_config: Callable[..., None],
) -> Callable[..., None]:
    """
    Factory: write config plus snapshot metadata for the given branches.
    """

    def _setup(
        path: Path,
        branches: list[str],
        *,
        mode: str = "shared",
        strategy: str = "template",
    ) -> None:
        setup_config(path, mode=mode, strategy=strategy)
        snapshot_dir = path / ".git" / "db-git" / "snapshots"
        for branch in branches:
            meta = make_metadata(
                branch=branch,
                database="testdb",
                strategy="pgdump",
                engine="postgresql",
                engine_version="16",
                file_size_bytes=1024,
            )
            write_metadata(snapshot_dir, meta)

    return _setup


class TestInit:
    @pytest.fixture(autouse=True)
    def mock_database_probe(self):
        with (
            patch.object(PostgresqlBackend, "get_engine_version", return_value=17),
            patch.object(PostgresqlBackend, "check_permissions") as permissions,
        ):
            permissions.return_value = PgPermissions(
                can_createdb=True,
                is_superuser=True,
                has_pg_signal_backend=True,
            )
            yield

    def test_installs_hook(self, git_repo):
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(
                app,
                ["init", "--database-url", "postgresql://localhost/testdb"],
            )
            assert result.exit_code == 0
            assert "initialized" in result.output

            hook = git_repo / ".git" / "hooks" / "post-checkout"
            assert hook.exists()
            assert ".db-git.toml" in (git_repo / ".gitignore").read_text()
        finally:
            os.chdir(old_cwd)

    def test_fails_outside_git_repo(self, tmp_path):
        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = runner.invoke(
                app,
                ["init", "--database-url", "postgresql://localhost/testdb"],
            )
            assert result.exit_code == 1
            assert "Not inside a git repository" in result.output
        finally:
            os.chdir(old_cwd)

    def test_fails_when_database_unreachable(self, git_repo):
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            with patch.object(
                PostgresqlBackend,
                "get_engine_version",
                side_effect=DatabaseError("connection refused"),
            ):
                result = runner.invoke(
                    app,
                    ["init", "--database-url", "postgresql://localhost/testdb"],
                )

            assert result.exit_code == 1
            assert "Could not connect to database" in result.output
            assert "initialized" not in result.output
            assert not (git_repo / ".git" / "hooks" / "post-checkout").exists()
            assert not (git_repo / ".gitignore").exists()
        finally:
            os.chdir(old_cwd)

    def test_writes_all_flags_to_toml(self, git_repo):
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(
                app,
                [
                    "init",
                    "--database-url",
                    "postgresql://localhost/testdb",
                    "--mode",
                    "shared",
                    "--strategy",
                    "pgdump",
                    "--on-active-connections",
                    "fail",
                ],
            )
            assert result.exit_code == 0
            assert "initialized" in result.output

            toml = tomllib.loads((git_repo / ".db-git.toml").read_text())
            assert toml["database_url"] == "postgresql://localhost/testdb"
            assert toml["mode"] == "shared"
            assert toml["strategy"] == "pgdump"
            assert toml["on_active_connections"] == "fail"
        finally:
            os.chdir(old_cwd)

    def test_per_branch_writes_default_branch(self, git_repo):
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(
                app,
                [
                    "init",
                    "--database-url",
                    "postgresql://localhost/testdb",
                    "--mode",
                    "per-branch",
                    "--strategy",
                    "template",
                    "--on-active-connections",
                    "terminate",
                ],
            )
            assert result.exit_code == 0

            toml = tomllib.loads((git_repo / ".db-git.toml").read_text())
            assert toml["mode"] == "per-branch"
            assert "default_branch" in toml
            assert toml["default_branch"]
        finally:
            os.chdir(old_cwd)

    def test_no_hook_skips_hook_install(self, git_repo):
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(
                app,
                [
                    "init",
                    "--database-url",
                    "postgresql://localhost/testdb",
                    "--mode",
                    "shared",
                    "--strategy",
                    "template",
                    "--on-active-connections",
                    "terminate",
                    "--no-hook",
                ],
            )
            assert result.exit_code == 0
            assert "skipped" in result.output

            hook = git_repo / ".git" / "hooks" / "post-checkout"
            assert not hook.exists()
        finally:
            os.chdir(old_cwd)

    def test_reinit_shows_update_message(self, git_repo, setup_config):
        setup_config(git_repo)  # Pre-existing .db-git.toml
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(
                app,
                [
                    "init",
                    "--database-url",
                    "postgresql://localhost/updated_db",
                    "--mode",
                    "shared",
                    "--strategy",
                    "pgdump",
                    "--on-active-connections",
                    "terminate",
                ],
            )
            assert result.exit_code == 0
            assert "Updating existing configuration" in result.output

            toml = tomllib.loads((git_repo / ".db-git.toml").read_text())
            assert toml["database_url"] == "postgresql://localhost/updated_db"
            assert toml["strategy"] == "pgdump"
        finally:
            os.chdir(old_cwd)


class TestSave:
    def test_with_mocked_backend(self, git_repo, setup_config):
        setup_config(git_repo)
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            with patch.object(TemplateStrategy, "save") as mock_save:
                result = runner.invoke(app, ["save"])

            assert result.exit_code == 0
            assert "Saved" in result.output
            assert "template" in result.output
            mock_save.assert_called_once()
        finally:
            os.chdir(old_cwd)

    def test_reports_error_on_failure(self, git_repo, setup_config):
        setup_config(git_repo)
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            with patch.object(
                TemplateStrategy,
                "save",
                side_effect=SnapshotError("connections exist"),
            ):
                result = runner.invoke(app, ["save"])

            assert result.exit_code == 1
            assert "connections exist" in result.output
        finally:
            os.chdir(old_cwd)

    def test_fails_without_init(self, git_repo):
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(app, ["save"])
            assert result.exit_code == 1
            assert "not initialized" in result.output
            assert "db-git init" in result.output
        finally:
            os.chdir(old_cwd)


class TestRestore:
    def test_with_mocked_backend(self, git_repo, setup_with_snapshots):
        setup_with_snapshots(git_repo, ["main"])
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            with patch.object(TemplateStrategy, "restore") as mock_restore:
                result = runner.invoke(app, ["restore", "main"])

            assert result.exit_code == 0
            assert "Restored" in result.output
            mock_restore.assert_called_once()
        finally:
            os.chdir(old_cwd)

    def test_no_snapshot(self, tmp_path, setup_config):
        setup_config(tmp_path)
        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = runner.invoke(app, ["restore", "nonexistent"])
            assert result.exit_code == 1
            assert "No snapshot found" in result.output
        finally:
            os.chdir(old_cwd)


class TestList:
    def test_with_no_snapshots(self, tmp_path, setup_config):
        setup_config(tmp_path)
        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = runner.invoke(app, ["list"])
            assert result.exit_code == 0
            assert "No snapshots found" in result.output
        finally:
            os.chdir(old_cwd)

    def test_shows_snapshots(self, tmp_path, setup_with_snapshots):
        setup_with_snapshots(tmp_path, ["main", "feature/auth"])
        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = runner.invoke(app, ["list"])
            assert result.exit_code == 0
            assert "main" in result.output
            assert "feature/auth" in result.output
        finally:
            os.chdir(old_cwd)

    def test_per_branch_empty(self, git_repo, setup_config):
        setup_config(git_repo, mode="per-branch")
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(app, ["list"])
            assert result.exit_code == 0
            assert "No branch databases found" in result.output
        finally:
            os.chdir(old_cwd)

    def test_per_branch_shows_entries(self, git_repo, setup_config):
        setup_config(git_repo, mode="per-branch")
        record_branch_db(git_repo / ".git", "feature", "testdb__feature", "main")
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            with patch.object(PostgresBranchDbManager, "exists", return_value=True):
                result = runner.invoke(app, ["list"])

            assert result.exit_code == 0
            assert "feature" in result.output
            assert "testdb__feature" in result.output
        finally:
            os.chdir(old_cwd)

    def test_fails_without_init(self, git_repo):
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(app, ["list"])
            assert result.exit_code == 1
            assert "not initialized" in result.output
        finally:
            os.chdir(old_cwd)


class TestStatus:
    def test_shared_mode(self, git_repo, setup_config):
        setup_config(git_repo)
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            with patch.object(PostgresqlBackend, "get_engine_version", return_value=16):
                result = runner.invoke(app, ["status"])

            assert result.exit_code == 0
            assert "Branch:" in result.output
            assert "Database:" in result.output
            assert "Strategy:" in result.output
            assert "template" in result.output
        finally:
            os.chdir(old_cwd)

    def test_shared_mode_current_snapshot_exists(self, git_repo, setup_with_snapshots):
        setup_with_snapshots(git_repo, ["main"])
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            with (
                patch.object(PostgresqlBackend, "get_engine_version", return_value=16),
                patch.object(PostgresqlBackend, "database_exists", return_value=True),
            ):
                result = runner.invoke(app, ["status"])

            assert result.exit_code == 0
            assert "Current:" in result.output
            assert "yes" in result.output
        finally:
            os.chdir(old_cwd)

    def test_shared_mode_current_snapshot_missing(self, git_repo, setup_with_snapshots):
        setup_with_snapshots(git_repo, ["main"])
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            with (
                patch.object(PostgresqlBackend, "get_engine_version", return_value=16),
                patch.object(PostgresqlBackend, "database_exists", return_value=False),
            ):
                result = runner.invoke(app, ["status"])

            assert result.exit_code == 0
            assert "Current:" in result.output
            assert "missing" in result.output
        finally:
            os.chdir(old_cwd)

    def test_shared_mode_current_snapshot_absent(self, git_repo, setup_config):
        setup_config(git_repo)
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            with patch.object(PostgresqlBackend, "get_engine_version", return_value=16):
                result = runner.invoke(app, ["status"])

            assert result.exit_code == 0
            assert "Current:" in result.output
            assert "no snapshot" in result.output
        finally:
            os.chdir(old_cwd)

    def test_per_branch_mode(self, git_repo, setup_config):
        setup_config(git_repo, mode="per-branch")
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            with (
                patch.object(PostgresqlBackend, "get_engine_version", return_value=16),
                patch.object(PostgresBranchDbManager, "exists", return_value=False),
            ):
                result = runner.invoke(app, ["status"])

            assert result.exit_code == 0
            assert "Mode:" in result.output
            assert "per-branch" in result.output
            assert "Seed:" in result.output
            assert "Default:" in result.output
            assert "Databases:" in result.output
        finally:
            os.chdir(old_cwd)

    def test_fails_without_init(self, git_repo):
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(app, ["status"])
            assert result.exit_code == 1
            assert "not initialized" in result.output
        finally:
            os.chdir(old_cwd)


class TestUrl:
    def test_per_branch_explicit_branch(self, git_repo, setup_config):
        setup_config(git_repo, mode="per-branch")
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(app, ["url", "feature/auth"])
            assert result.exit_code == 0
            assert (
                "postgresql://postgres:postgres@localhost:5432/testdb__feature__auth"
                in result.output
            )
        finally:
            os.chdir(old_cwd)

    def test_per_branch_default_branch_uses_seed(self, git_repo, setup_config):
        setup_config(git_repo, mode="per-branch")
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(app, ["url", "main"])
            assert result.exit_code == 0
            assert (
                "postgresql://postgres:postgres@localhost:5432/testdb" in result.output
            )
            assert "testdb__" not in result.output
        finally:
            os.chdir(old_cwd)

    def test_shared_mode_emits_configured_url(self, git_repo, setup_config):
        setup_config(git_repo)
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(app, ["url"])
            assert result.exit_code == 0
            assert (
                "postgresql://postgres:postgres@localhost:5432/testdb" in result.output
            )
        finally:
            os.chdir(old_cwd)

    def test_detached_head_without_branch_fails(self, git_repo, setup_config):
        setup_config(git_repo, mode="per-branch")
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            with patch("db_git.cli.inspect.get_current_branch", return_value=None):
                result = runner.invoke(app, ["url"])
            assert result.exit_code == 1
            assert "detached" in result.output
        finally:
            os.chdir(old_cwd)

    def test_fails_without_init(self, git_repo):
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(app, ["url"])
            assert result.exit_code == 1
            assert "not initialized" in result.output
        finally:
            os.chdir(old_cwd)


class TestPrune:
    def test_dry_run(self, git_repo, setup_with_snapshots):
        setup_with_snapshots(git_repo, ["main", "stale-branch"])
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(app, ["prune", "--dry-run"])
            assert result.exit_code == 0
            assert "Would remove" in result.output
            assert "stale-branch" in result.output

            snapshot_dir = git_repo / ".git" / "db-git" / "snapshots"
            assert has_snapshot(snapshot_dir, "stale-branch")
        finally:
            os.chdir(old_cwd)

    def test_removes_stale(self, git_repo, setup_with_snapshots):
        setup_with_snapshots(git_repo, ["main", "stale-branch"])
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = []
            with patch.object(
                PostgresqlBackend, "connect_maintenance", return_value=mock_conn
            ):
                result = runner.invoke(app, ["prune", "--yes"])
            assert result.exit_code == 0
            assert "Pruned" in result.output

            snapshot_dir = git_repo / ".git" / "db-git" / "snapshots"
            assert not has_snapshot(snapshot_dir, "stale-branch")
            assert has_snapshot(snapshot_dir, "main")
        finally:
            os.chdir(old_cwd)

    def test_refuses_without_yes_in_non_interactive(
        self, git_repo, setup_with_snapshots
    ):
        setup_with_snapshots(git_repo, ["main", "stale-branch"])
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(app, ["prune"])
            assert result.exit_code == 1
            assert "non-interactive" in result.output
            assert "--yes" in result.output

            snapshot_dir = git_repo / ".git" / "db-git" / "snapshots"
            assert has_snapshot(snapshot_dir, "stale-branch")
        finally:
            os.chdir(old_cwd)

    def test_interactive_abort_keeps_snapshots(self, git_repo, setup_with_snapshots):
        setup_with_snapshots(git_repo, ["main", "stale-branch"])
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(app, ["prune"], input="n\n")
            snapshot_dir = git_repo / ".git" / "db-git" / "snapshots"
            assert has_snapshot(snapshot_dir, "stale-branch")
            assert "Pruned" not in result.output
        finally:
            os.chdir(old_cwd)

    def test_per_branch_nothing_to_prune(self, git_repo, setup_config):
        setup_config(git_repo, mode="per-branch")
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(app, ["prune", "--yes"])
            assert result.exit_code == 0
            assert "No branch databases" in result.output
        finally:
            os.chdir(old_cwd)

    def test_per_branch_dry_run(self, git_repo, setup_config):
        setup_config(git_repo, mode="per-branch")
        record_branch_db(git_repo / ".git", "stale-branch", "testdb__stale", "main")
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(app, ["prune", "--dry-run"])
            assert result.exit_code == 0
            assert "Would drop" in result.output
            assert "testdb__stale" in result.output
        finally:
            os.chdir(old_cwd)

    def test_per_branch_removes_stale(self, git_repo, setup_config):
        setup_config(git_repo, mode="per-branch")
        record_branch_db(git_repo / ".git", "stale-branch", "testdb__stale", "main")
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            with patch.object(PostgresBranchDbManager, "drop") as mock_drop:
                result = runner.invoke(app, ["prune", "--yes"])

            assert result.exit_code == 0
            assert "Dropped" in result.output
            mock_drop.assert_called_once()
        finally:
            os.chdir(old_cwd)


class TestCreate:
    def test_shared_mode_shows_save_hint(self, git_repo, setup_config):
        setup_config(git_repo)
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(app, ["create", "feature"])
            assert result.exit_code == 0
            assert "db-git save" in result.output
        finally:
            os.chdir(old_cwd)

    def test_errors_when_branch_db_exists(self, git_repo, setup_config):
        setup_config(git_repo, mode="per-branch")
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            with patch.object(PostgresBranchDbManager, "exists", return_value=True):
                result = runner.invoke(app, ["create", "feature"])

            assert result.exit_code == 1
            assert "already exists" in result.output
            assert "db-git reset" in result.output
        finally:
            os.chdir(old_cwd)

    def test_calls_manager_on_success(self, git_repo, setup_config):
        setup_config(git_repo, mode="per-branch")
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            with (
                patch.object(PostgresBranchDbManager, "exists", return_value=False),
                patch.object(PostgresBranchDbManager, "create") as mock_create,
            ):
                result = runner.invoke(app, ["create", "feature"])

            assert result.exit_code == 0
            assert "Created" in result.output
            mock_create.assert_called_once()
        finally:
            os.chdir(old_cwd)


class TestReset:
    def test_shared_mode_shows_restore_hint(self, git_repo, setup_config):
        setup_config(git_repo)
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(app, ["reset", "feature"])
            assert result.exit_code == 0
            assert "db-git restore" in result.output
        finally:
            os.chdir(old_cwd)

    def test_refuses_default_branch(self, git_repo, setup_config):
        setup_config(git_repo, mode="per-branch")
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(app, ["reset", "main"])

            assert result.exit_code == 1
            assert "Cannot reset the default branch" in result.output
        finally:
            os.chdir(old_cwd)

    def test_drops_and_recreates(self, git_repo, setup_config):
        setup_config(git_repo, mode="per-branch")
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            with (
                patch.object(PostgresBranchDbManager, "exists", return_value=True),
                patch.object(PostgresBranchDbManager, "drop") as mock_drop,
                patch.object(PostgresBranchDbManager, "create") as mock_create,
            ):
                result = runner.invoke(app, ["reset", "feature"])

            assert result.exit_code == 0
            assert "Reset" in result.output
            mock_drop.assert_called_once()
            mock_create.assert_called_once()
        finally:
            os.chdir(old_cwd)


class TestHook:
    def test_install_and_remove(self, git_repo):
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(app, ["hook", "install"])
            assert result.exit_code == 0
            assert "installed" in result.output

            result = runner.invoke(app, ["hook", "remove"])
            assert result.exit_code == 0
            assert "removed" in result.output
        finally:
            os.chdir(old_cwd)

    def test_dispatch_catches_all_exceptions(self, git_repo):
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(
                app,
                ["_hook-dispatch", "abc123", "def456", "1"],
            )
            assert result.exit_code == 0
        finally:
            os.chdir(old_cwd)

    def test_script_contains_skip_checks(self):
        script = render_hook_script()
        assert "DB_GIT_SKIP" in script
        assert "db-git/disabled" in script


class TestDisable:
    def test_creates_flag_file(self, git_repo):
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(app, ["disable"])
            assert result.exit_code == 0
            assert "disabled" in result.output
            assert (git_repo / ".git" / "db-git" / "disabled").exists()
        finally:
            os.chdir(old_cwd)


class TestEnable:
    def test_removes_flag_file(self, git_repo):
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            runner.invoke(app, ["disable"])
            result = runner.invoke(app, ["enable"])
            assert result.exit_code == 0
            assert "enabled" in result.output
            assert not (git_repo / ".git" / "db-git" / "disabled").exists()
        finally:
            os.chdir(old_cwd)

    def test_when_already_enabled(self, git_repo):
        old_cwd = os.getcwd()
        os.chdir(git_repo)
        try:
            result = runner.invoke(app, ["enable"])
            assert result.exit_code == 0
            assert "already enabled" in result.output
        finally:
            os.chdir(old_cwd)
