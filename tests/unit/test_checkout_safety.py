from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from db_git.config import DbGitConfig
from db_git.git import handle_post_checkout


@pytest.fixture
def checkout(monkeypatch, tmp_path):
    import db_git.git as git

    backend = Mock()
    monkeypatch.setattr(git, "operation_scope", lambda *args: nullcontext())
    for name, value in {
        "get_git_dir": tmp_path,
        "is_rebase_in_progress": False,
        "is_detached_head": False,
        "get_previous_branch": "main",
        "get_current_branch": "feature",
        "get_backend": backend,
        "has_snapshot": True,
    }.items():
        monkeypatch.setattr(git, name, Mock(return_value=value))
    return backend.detect_strategy.return_value, tmp_path


def test_failed_save_preserves_working_database_and_disables_hook(checkout):
    strategy, git_dir = checkout
    strategy.save.side_effect = RuntimeError("disk full")
    config = DbGitConfig(database_url="postgresql:///app", strategy="pgdump")

    handle_post_checkout("a" * 40, "1", config)

    strategy.restore.assert_not_called()
    assert (git_dir / "db-git" / "disabled").exists()
    strategy.save.reset_mock()
    handle_post_checkout("b" * 40, "1", config)
    strategy.save.assert_not_called()


def test_successful_save_allows_restore(checkout):
    strategy, git_dir = checkout
    config = DbGitConfig(database_url="postgresql:///app", strategy="pgdump")
    handle_post_checkout("a" * 40, "1", config)
    strategy.save.assert_called_once()
    strategy.restore.assert_called_once()
    assert not (git_dir / "db-git" / "disabled").exists()


def test_failed_restore_disables_future_automatic_saves(checkout):
    strategy, git_dir = checkout
    strategy.restore.side_effect = RuntimeError("restore failed")
    handle_post_checkout(
        "a" * 40, "1", DbGitConfig(database_url="postgresql:///app", strategy="pgdump")
    )
    assert (git_dir / "db-git" / "disabled").exists()


def test_crash_between_save_and_restore_keeps_switching_disabled(tmp_path):
    import subprocess
    import sys

    code = """
import os, sys
from pathlib import Path
from contextlib import nullcontext
from unittest.mock import Mock
import db_git.git as git
from db_git.config import DbGitConfig
git.get_git_dir = lambda: Path(sys.argv[1])
git.is_rebase_in_progress = lambda _: False
git.is_detached_head = lambda: False
git.get_previous_branch = lambda: 'main'
git.get_current_branch = lambda: 'feature'
git.get_backend = lambda _: Mock()
git.operation_scope = lambda *args: nullcontext()
git._try_save = lambda *args: True
git.has_snapshot = lambda *args: os._exit(77)
config = DbGitConfig(database_url="postgresql:///app", strategy="template")
git.handle_post_checkout("a" * 40, "1", config)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True
    )
    assert result.returncode == 77, result.stderr
    marker = tmp_path / "db-git" / "disabled"
    assert marker.exists()
    assert "from 'main' to 'feature'" in marker.read_text()
