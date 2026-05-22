from __future__ import annotations

import os

import pytest

from git_db.errors import HookError
from git_db.git import install_hook, remove_hook
from git_db.hook_script import HOOK_IDENTIFIER


class TestGit:
    def test_install_hook_creates_file(self, git_repo):
        git_dir = git_repo / ".git"
        install_hook(git_dir)

        hook_path = git_dir / "hooks" / "post-checkout"
        assert hook_path.exists()
        assert os.access(hook_path, os.X_OK)
        assert HOOK_IDENTIFIER in hook_path.read_text()

    def test_install_hook_preserves_legacy(self, git_repo):
        git_dir = git_repo / ".git"
        hooks_dir = git_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        existing_hook = hooks_dir / "post-checkout"
        existing_hook.write_text("#!/bin/sh\necho 'my custom hook'\n")

        install_hook(git_dir)

        legacy = hooks_dir / "post-checkout.legacy"
        assert legacy.exists()
        assert "my custom hook" in legacy.read_text()

        hook = hooks_dir / "post-checkout"
        assert HOOK_IDENTIFIER in hook.read_text()

    def test_install_hook_overwrites_managed_hook(self, git_repo):
        git_dir = git_repo / ".git"
        install_hook(git_dir)
        install_hook(git_dir)

        hooks_dir = git_dir / "hooks"
        assert not (hooks_dir / "post-checkout.legacy").exists()
        assert HOOK_IDENTIFIER in (hooks_dir / "post-checkout").read_text()

    def test_remove_hook(self, git_repo):
        git_dir = git_repo / ".git"
        install_hook(git_dir)
        remove_hook(git_dir)

        hook_path = git_dir / "hooks" / "post-checkout"
        assert not hook_path.exists()

    def test_remove_hook_restores_legacy(self, git_repo):
        git_dir = git_repo / ".git"
        hooks_dir = git_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        existing = hooks_dir / "post-checkout"
        existing.write_text("#!/bin/sh\necho 'original'\n")
        install_hook(git_dir)

        remove_hook(git_dir)

        hook = hooks_dir / "post-checkout"
        assert hook.exists()
        assert "original" in hook.read_text()
        assert not (hooks_dir / "post-checkout.legacy").exists()

    def test_remove_hook_raises_when_no_managed_hook(self, git_repo):
        git_dir = git_repo / ".git"
        with pytest.raises(HookError, match="No git-db hook found"):
            remove_hook(git_dir)
