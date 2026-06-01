from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from db_git.config import (
    DbGitConfig,
    _validate_config,
    ensure_config_ignored,
    load_config,
    write_config,
)
from db_git.errors import ConfigError


class TestConfig:
    def test_default_values(self):
        config = DbGitConfig(database_url="postgresql://localhost/db")
        assert config.mode == "shared"
        assert config.default_branch == "main"
        assert config.strategy == ""
        assert config.on_active_connections == "terminate"
        assert config.snapshot_dir == Path(".git/db-git/snapshots")
        assert config.max_snapshots == 20
        assert config.force_terminate_timeout_ms == 5000

    def test_missing_database_url(self):
        config = DbGitConfig()
        with pytest.raises(ConfigError, match="No database URL"):
            _validate_config(config)

    def test_empty_strategy_raises(self):
        config = DbGitConfig(
            database_url="postgresql://localhost/db",
        )
        with pytest.raises(ConfigError, match="Strategy not configured"):
            _validate_config(config)

    def test_invalid_on_active_connections(self):
        config = DbGitConfig(
            database_url="postgresql://localhost/db",
            strategy="template",
            on_active_connections="invalid",
        )
        with pytest.raises(ConfigError, match="Invalid on_active_connections"):
            _validate_config(config)

    def test_loads_from_db_git_toml(self, tmp_path: Path):
        dotfile = tmp_path / ".db-git.toml"
        dotfile.write_text(
            'database_url = "postgresql://localhost/fromdotfile"\n'
            'strategy = "template"\n'
            'on_active_connections = "terminate"\n'
        )
        (tmp_path / ".git").mkdir()

        config = load_config(project_root=tmp_path)
        assert config.database_url == "postgresql://localhost/fromdotfile"
        assert config.on_active_connections == "terminate"

    def test_database_url_env(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        env = {
            "DATABASE_URL": "postgresql://localhost/fromenv",
            "DB_GIT_STRATEGY": "template",
        }
        with patch.dict(os.environ, env, clear=False):
            config = load_config(project_root=tmp_path)
        assert config.database_url == "postgresql://localhost/fromenv"

    def test_db_git_prefixed_env_takes_precedence(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        env = {
            "DATABASE_URL": "postgresql://localhost/generic",
            "DB_GIT_DATABASE_URL": "postgresql://localhost/specific",
            "DB_GIT_STRATEGY": "template",
        }
        with patch.dict(os.environ, env, clear=False):
            config = load_config(project_root=tmp_path)
        assert config.database_url == "postgresql://localhost/specific"

    def test_env_overrides_file(self, tmp_path: Path):
        dotfile = tmp_path / ".db-git.toml"
        dotfile.write_text(
            'database_url = "postgresql://localhost/fromfile"\nstrategy = "template"\n'
        )
        (tmp_path / ".git").mkdir()
        env = {"DB_GIT_STRATEGY": "pgdump"}
        with patch.dict(os.environ, env, clear=False):
            config = load_config(project_root=tmp_path)
        assert config.strategy == "pgdump"
        assert config.database_url == "postgresql://localhost/fromfile"

    def test_cli_overrides_everything(self, tmp_path: Path):
        dotfile = tmp_path / ".db-git.toml"
        dotfile.write_text(
            'database_url = "postgresql://localhost/fromfile"\nstrategy = "template"\n'
        )
        (tmp_path / ".git").mkdir()
        env = {"DB_GIT_STRATEGY": "pgdump"}
        with patch.dict(os.environ, env, clear=False):
            config = load_config(
                cli_overrides={
                    "database_url": "postgresql://localhost/fromcli",
                    "strategy": "template",
                },
                project_root=tmp_path,
            )
        assert config.database_url == "postgresql://localhost/fromcli"
        assert config.strategy == "template"

    def test_none_cli_overrides_ignored(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        dotfile = tmp_path / ".db-git.toml"
        dotfile.write_text(
            'database_url = "postgresql://localhost/db"\nstrategy = "template"\n'
        )
        config = load_config(
            cli_overrides={"strategy": None},
            project_root=tmp_path,
        )
        assert config.strategy == "template"

    def test_invalid_mode_raises(self):
        config = DbGitConfig(database_url="postgresql://localhost/db", mode="invalid")
        with pytest.raises(ConfigError, match="Invalid mode"):
            _validate_config(config)

    def test_mode_from_env(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        dotfile = tmp_path / ".db-git.toml"
        dotfile.write_text(
            'database_url = "postgresql://localhost/db"\nstrategy = "template"\n'
        )
        with patch.dict(os.environ, {"DB_GIT_MODE": "per-branch"}, clear=False):
            config = load_config(project_root=tmp_path)
        assert config.mode == "per-branch"

    def test_mode_from_dotfile(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        dotfile = tmp_path / ".db-git.toml"
        dotfile.write_text(
            'database_url = "postgresql://localhost/db"\n'
            'strategy = "template"\n'
            'mode = "per-branch"\n'
        )
        config = load_config(project_root=tmp_path)
        assert config.mode == "per-branch"

    def test_write_config_creates_new_file(self, tmp_path: Path):
        write_config(tmp_path, {"mode": "per-branch", "strategy": "template"})
        dotfile = tmp_path / ".db-git.toml"
        assert dotfile.exists()
        content = dotfile.read_text()
        assert 'mode = "per-branch"' in content
        assert 'strategy = "template"' in content

    def test_write_config_preserves_existing_keys(self, tmp_path: Path):
        dotfile = tmp_path / ".db-git.toml"
        dotfile.write_text('database_url = "postgresql://localhost/db"\n')
        write_config(tmp_path, {"mode": "shared"})
        content = dotfile.read_text()
        assert "database_url" in content
        assert 'mode = "shared"' in content

    def test_write_config_preserves_comments(self, tmp_path: Path):
        dotfile = tmp_path / ".db-git.toml"
        dotfile.write_text(
            '# My custom comment\ndatabase_url = "postgresql://localhost/db"\n'
        )
        write_config(tmp_path, {"mode": "per-branch"})
        content = dotfile.read_text()
        assert "# My custom comment" in content

    def test_write_config_adds_standard_comments_for_new_keys(self, tmp_path: Path):
        write_config(tmp_path, {"mode": "per-branch"})
        content = (tmp_path / ".db-git.toml").read_text()
        assert "# How db-git manages databases" in content

    def test_ensure_config_ignored_creates_gitignore(self, tmp_path: Path):
        changed = ensure_config_ignored(tmp_path)

        assert changed is True
        assert (tmp_path / ".gitignore").read_text() == (
            "# Local db-git configuration\n.db-git.toml\n"
        )

    def test_ensure_config_ignored_appends_entry(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text(".venv\n")

        changed = ensure_config_ignored(tmp_path)

        assert changed is True
        assert (tmp_path / ".gitignore").read_text() == (
            ".venv\n\n# Local db-git configuration\n.db-git.toml\n"
        )

    def test_ensure_config_ignored_does_not_duplicate_entry(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text(".venv\n.db-git.toml\n")

        changed = ensure_config_ignored(tmp_path)

        assert changed is False
        assert (tmp_path / ".gitignore").read_text().count(".db-git.toml") == 1
