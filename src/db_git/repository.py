"""Git-resolved paths and worktree safety, independent of database backends."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from db_git.errors import ConfigError, DbGitError, HookError


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10
    )


def worktree_root(cwd: Path | None = None) -> Path | None:
    result = _git("rev-parse", "--show-toplevel", cwd=cwd)
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None


def git_directory(cwd: Path | None = None) -> Path | None:
    result = _git("rev-parse", "--absolute-git-dir", cwd=cwd)
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None


def common_git_directory(git_dir: Path) -> Path:
    """Ordinary repositories use git_dir; linked worktrees resolve Git's common dir."""
    if not (git_dir / "commondir").exists():
        return git_dir
    result = _git(
        f"--git-dir={git_dir}",
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    if result.returncode:
        raise DbGitError("Cannot resolve the worktree's common Git directory.")
    return Path(result.stdout.strip()).resolve()


def shared_state_directory(git_dir: Path) -> Path:
    common = common_git_directory(git_dir)
    # Older versions could write isolated ownership/journals in linked worktrees.
    # Never hide those records by silently switching to the common directory.
    for local in (common / "worktrees").glob("*/db-git"):
        if (local / "state.json").exists() or any(
            (local / "operations").glob("*.json")
        ):
            raise DbGitError(
                f"Legacy worktree-local state exists at {local}. Preserve it and "
                "reconcile it with the common db-git state before continuing."
            )
    return common / "db-git"


def operations_directory(git_dir: Path) -> Path:
    return shared_state_directory(git_dir) / "operations"


@dataclass(frozen=True)
class Worktree:
    path: Path
    bare: bool = False
    prunable: bool = False


def list_worktrees(
    cwd: Path | None = None, *, git_dir: Path | None = None
) -> list[Worktree]:
    prefix = [f"--git-dir={git_dir}"] if git_dir else []
    result = _git(*prefix, "worktree", "list", "--porcelain", "-z", cwd=cwd)
    if result.returncode:
        return []
    trees = []
    for record in result.stdout.split("\0\0"):
        fields = record.split("\0")
        if not fields[0].startswith("worktree "):
            continue
        trees.append(
            Worktree(
                Path(fields[0].removeprefix("worktree ")),
                "bare" in fields,
                any(
                    field == "prunable" or field.startswith("prunable ")
                    for field in fields
                ),
            )
        )
    return trees


def configuration_root(root: Path) -> Path:
    """Keep the repository's existing main-worktree config authoritative everywhere."""
    trees = list_worktrees(root)
    primary = trees[0].path if trees else root
    local_config, shared_config = root / ".db-git.toml", primary / ".db-git.toml"
    if (
        root.resolve() != primary.resolve()
        and local_config.exists()
        and (
            not shared_config.exists()
            or local_config.read_bytes() != shared_config.read_bytes()
        )
    ):
        raise ConfigError(
            "Conflicting worktree-local .db-git.toml. Reconcile it with the "
            f"shared configuration at {shared_config}; "
            "local overrides are not supported."
        )
    return primary


def hook_path(git_dir: Path) -> Path:
    """Resolve core.hooksPath relative to the hook's working-tree root, not cwd."""
    # Resolve the supplied Git directory's checkout even when callers run elsewhere.
    if git_directory() == git_dir.resolve():
        root = worktree_root()
        if root is None:
            raise HookError("Hooks require a Git working tree.")
    elif (git_dir / "gitdir").is_file():
        root = Path((git_dir / "gitdir").read_text().strip()).parent
    else:
        trees = list_worktrees(git_dir=git_dir)
        if not trees:
            raise HookError("Cannot resolve the Git directory's working tree.")
        root = trees[0].path
    result = _git(
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        "hooks/post-checkout",
        cwd=root,
    )
    if result.returncode:
        raise HookError("Cannot resolve Git's active hook path.")
    return Path(result.stdout.strip())


def require_safe_shared_mode(mode: str, cwd: Path | None = None) -> None:
    """A single working database cannot represent several checked-out branches."""
    if mode != "shared":
        return
    trees = [
        tree for tree in list_worktrees(cwd) if not tree.bare and not tree.prunable
    ]
    if len(trees) > 1:
        raise DbGitError(
            "Shared mode cannot switch or snapshot one database across multiple "
            "Git worktrees. Use per-branch mode, or remove the extra worktrees. "
            "Existing snapshots and recovery records are preserved."
        )
