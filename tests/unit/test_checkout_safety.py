from unittest.mock import Mock

import pytest

from db_git.config import DbGitConfig
from db_git.git import handle_post_checkout


@pytest.fixture
def checkout(monkeypatch, tmp_path):
    import db_git.git as git

    backend = Mock()
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
    config = DbGitConfig(strategy="pgdump")

    handle_post_checkout("a" * 40, "1", config)

    strategy.restore.assert_not_called()
    assert (git_dir / "db-git" / "disabled").exists()
    strategy.save.reset_mock()
    handle_post_checkout("b" * 40, "1", config)
    strategy.save.assert_not_called()


def test_successful_save_allows_restore(checkout):
    strategy, git_dir = checkout
    config = DbGitConfig(strategy="pgdump")
    handle_post_checkout("a" * 40, "1", config)
    strategy.save.assert_called_once()
    strategy.restore.assert_called_once()
    assert not (git_dir / "db-git" / "disabled").exists()


def test_failed_restore_disables_future_automatic_saves(checkout):
    strategy, git_dir = checkout
    strategy.restore.side_effect = RuntimeError("restore failed")
    handle_post_checkout("a" * 40, "1", DbGitConfig(strategy="pgdump"))
    assert (git_dir / "db-git" / "disabled").exists()
