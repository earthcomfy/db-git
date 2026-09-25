from __future__ import annotations

import os
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from db_git.cli import app
from db_git.config import load_config, write_config
from db_git.errors import ConfigError, DbGitError, HookError
from db_git.files import local_lock
from db_git.git import install_hook, remove_hook
from db_git.repository import (
    common_git_directory,
    configuration_root,
    git_directory,
    hook_path,
    operations_directory,
    require_safe_shared_mode,
)
from db_git.state import load_state, record_branch_db, state_path


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def worktrees(git_repo):
    linked = git_repo / "linked checkout"
    git(git_repo, "worktree", "add", "-b", "feature", str(linked))
    write_config(
        git_repo,
        {
            "database_url": "postgresql:///seed",
            "mode": "per-branch",
            "strategy": "template",
        },
    )
    return git_repo, linked


def test_config_and_paths_from_nested_linked_worktree(worktrees, monkeypatch):
    main, linked = worktrees
    nested = linked / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    config = load_config()
    assert config.database_url == "postgresql:///seed"
    assert config.snapshot_dir == main / ".git/db-git/snapshots"
    assert configuration_root(linked) == main
    local = git_directory(nested)
    assert local != main / ".git"
    assert common_git_directory(local) == main / ".git"
    write_config(linked, {"snapshot_dir": "custom-snapshots"})
    assert not (linked / ".db-git.toml").exists()
    assert load_config().snapshot_dir == main / "custom-snapshots"


def test_conflicting_config_is_not_silently_ignored(worktrees):
    main, linked = worktrees
    config = linked / ".db-git.toml"
    config.write_bytes((main / ".db-git.toml").read_bytes())
    assert configuration_root(linked) == main
    config.write_text('database_url = "postgresql:///other"\n')
    with pytest.raises(ConfigError, match="Conflicting worktree-local"):
        load_config(project_root=linked)


def test_shared_ownership_and_lock(worktrees):
    main, linked = worktrees
    primary, local = git_directory(main), git_directory(linked)
    record_branch_db(primary, "old", "legacy_db_name", "main")
    record_branch_db(local, "feature", "feature_db", "old")
    assert state_path(primary) == state_path(local)
    assert load_state(primary) == load_state(local)
    assert load_state(local).databases["old"].db_name == "legacy_db_name"
    with local_lock(operations_directory(primary)):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; from db_git.state import record_branch_db; "
                "import sys; record_branch_db(Path(sys.argv[1]), "
                "'other', 'other_db', 'main')",
                str(local),
            ],
            capture_output=True,
            text=True,
        )
    assert result.returncode != 0
    assert "Another db-git operation is running" in result.stderr
    assert set(load_state(primary).databases) == {"old", "feature"}


@pytest.mark.parametrize("resource", ["state.json", "operations/pending.json"])
def test_legacy_local_state_is_preserved_and_blocks_both_worktrees(worktrees, resource):
    main, linked = worktrees
    legacy = git_directory(linked) / "db-git" / resource
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("legacy")
    for root in (main, linked):
        with pytest.raises(DbGitError, match="Legacy worktree-local state"):
            load_state(git_directory(root))
    assert legacy.read_text() == "legacy"


def test_disable_and_enable_are_worktree_local(worktrees, monkeypatch):
    main, linked = worktrees
    runner = CliRunner()
    monkeypatch.chdir(main)
    assert runner.invoke(app, ["disable"]).exit_code == 0
    monkeypatch.chdir(linked)
    assert runner.invoke(app, ["enable"]).exit_code == 0
    assert (git_directory(main) / "db-git/disabled").exists()
    assert not (git_directory(linked) / "db-git/disabled").exists()
    assert runner.invoke(app, ["disable"]).exit_code == 0
    assert runner.invoke(app, ["enable"]).exit_code == 0
    assert (git_directory(main) / "db-git/disabled").exists()


@pytest.mark.parametrize("custom", ["default", "relative", "absolute"])
def test_hooks_use_git_path_and_preserve_legacy(worktrees, monkeypatch, custom):
    main, linked = worktrees
    if custom != "default":
        location = (
            ".custom hooks" if custom == "relative" else str(main / "absolute hooks")
        )
        git(main, "config", "core.hooksPath", location)
    local = git_directory(linked)
    active = hook_path(local)
    expected = (
        linked / ".custom hooks/post-checkout"
        if custom == "relative"
        else main / "absolute hooks/post-checkout"
        if custom == "absolute"
        else main / ".git/hooks/post-checkout"
    )
    assert active == expected
    active.parent.mkdir(parents=True, exist_ok=True)
    legacy = '#!/bin/sh\nprintf "%s\\n" "$3" >> legacy-events\n'
    active.write_text(legacy)
    active.chmod(0o755)
    monkeypatch.chdir(main)
    install_hook(local)
    install_hook(local)
    # Legacy hooks still receive file checkouts and disabled db-git checkouts.
    for is_branch in ("0", "1"):
        subprocess.run(
            [str(active), "old", "new", is_branch],
            cwd=linked,
            env={**os.environ, "DB_GIT_SKIP": "1"},
            check=True,
        )
    assert (linked / "legacy-events").read_text() == "0\n1\n"
    remove_hook(local)
    assert active.read_text() == legacy
    assert not active.with_name("post-checkout.legacy").exists()


def test_existing_legacy_backup_is_not_overwritten(git_repo):
    directory = git_directory(git_repo)
    hook = hook_path(directory)
    hook.write_text("existing hook")
    backup = hook.with_name("post-checkout.legacy")
    backup.write_text("existing backup")
    with pytest.raises(HookError, match="already exists"):
        install_hook(directory)
    assert hook.read_text() == "existing hook"
    assert backup.read_text() == "existing backup"


def test_separate_git_directory(tmp_path, monkeypatch):
    root, metadata = tmp_path / "checkout", tmp_path / "metadata"
    root.mkdir()
    git(root, "init", "--separate-git-dir", str(metadata))
    git(root, "config", "core.hooksPath", "custom-hooks")
    write_config(root, {"database_url": "postgresql:///seed", "strategy": "template"})
    assert load_config(project_root=root).snapshot_dir == metadata / "db-git/snapshots"
    monkeypatch.chdir(root)
    assert hook_path(metadata) == root / "custom-hooks/post-checkout"
    install_hook(metadata)
    assert (root / "custom-hooks/post-checkout").is_file()


def test_shared_mode_refuses_multiple_worktrees(worktrees):
    main, linked = worktrees
    for root in (main, linked):
        require_safe_shared_mode("per-branch", root)
        with pytest.raises(DbGitError, match="multiple Git worktrees"):
            require_safe_shared_mode("shared", root)
    git(main, "worktree", "remove", "--force", str(linked))
    require_safe_shared_mode("shared", main)
