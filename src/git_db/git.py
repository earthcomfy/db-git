from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

from git_db.backends import DatabaseBackend, get_backend
from git_db.db import parse_database_url
from git_db.errors import HookError
from git_db.hook_script import HOOK_IDENTIFIER, render_hook_script
from git_db.state import get_branch_db
from git_db.storage import branch_db_name, has_snapshot

if TYPE_CHECKING:
    from git_db.config import GitDbConfig

console = Console(stderr=True)

_NULL_REF = "0" * 40


def get_current_branch() -> str | None:
    """
    Return the current branch name, or None if HEAD is detached.
    """
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def get_previous_branch() -> str | None:
    """
    Return the branch that was checked out before the current one.
    """
    # Primary: @{-1}
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "@{-1}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0 and result.stdout.strip():
        branch = result.stdout.strip()
        if branch != "HEAD":
            return branch

    # Fallback: parse the reflog entry
    result = subprocess.run(
        ["git", "reflog", "--format=%gs", "-1", "HEAD@{0}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0:
        match = re.match(r"checkout: moving from (.+) to .+", result.stdout.strip())
        if match:
            name = match.group(1)
            if len(name) >= 40 and all(c in "0123456789abcdef" for c in name[:40]):
                return None
            return name

    return None


def is_detached_head() -> bool:
    """
    Return True if HEAD is detached.
    """
    return get_current_branch() is None


def is_rebase_in_progress(git_dir: Path) -> bool:
    """
    Return True if a rebase is in progress.
    """
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


def is_null_ref(ref: str) -> bool:
    """
    Return True if the ref is a null ref (40 zeros) indicating a fresh clone.
    """
    return ref == _NULL_REF


def get_git_dir() -> Path | None:
    """
    Return the .git directory path, or None if not in a git repo.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def list_branches() -> list[str]:
    """
    Return a list of all local branch names.
    """
    result = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return []
    return [b for b in result.stdout.strip().splitlines() if b]


def install_hook(git_dir: Path) -> None:
    """
    Install the post-checkout hook.
    """
    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    git_db_executable = _resolve_git_db_executable()

    try:
        _write_managed_hook(
            hooks_dir,
            "post-checkout",
            render_hook_script(git_db_executable),
        )
    except OSError as e:
        raise HookError(f"Failed to install hook: {e}") from e


def remove_hook(git_dir: Path) -> None:
    """
    Remove the git-db post-checkout hook.
    """
    hooks_dir = git_dir / "hooks"
    hook_path = hooks_dir / "post-checkout"
    legacy_path = hooks_dir / "post-checkout.legacy"

    if not hook_path.exists() or HOOK_IDENTIFIER not in hook_path.read_text():
        raise HookError("No git-db hook found in .git/hooks/post-checkout.")

    try:
        hook_path.unlink()
        if legacy_path.exists():
            legacy_path.rename(hook_path)
            console.print("Restored original post-checkout hook")
    except OSError as e:
        raise HookError(f"Failed to remove hook: {e}") from e


def handle_post_checkout(
    prev_head: str,
    is_branch: str,
    config: GitDbConfig,
) -> None:
    """
    Handle a post-checkout event. Called by the _hook-dispatch CLI command.
    """
    try:
        if is_branch != "1":
            return

        git_dir = get_git_dir()
        if git_dir is None:
            return

        if is_null_ref(prev_head):
            return

        if is_rebase_in_progress(git_dir):
            return

        if is_detached_head():
            return

        prev_branch = get_previous_branch()
        curr_branch = get_current_branch()

        if prev_branch and curr_branch and prev_branch == curr_branch:
            return

        if config.mode == "per-branch":
            _handle_per_branch_checkout(config, git_dir, prev_branch, curr_branch)
            return

        backend = get_backend(config.database_url)

        if prev_branch:
            _try_save(backend, config, prev_branch)

        if curr_branch:
            if has_snapshot(config.snapshot_dir, curr_branch):
                _try_restore(backend, config, curr_branch)
            else:
                console.print(
                    f"[dim]No snapshot for '{curr_branch}': database unchanged[/]"
                )
    except Exception as e:
        console.print(f"[yellow]git-db warning:[/] {e}")


def _try_save(backend: DatabaseBackend, config: GitDbConfig, branch: str) -> None:
    """
    Attempt to save a snapshot.
    """
    try:
        strategy = backend.detect_strategy(config)
        strategy.save(config.database_url, branch, config.snapshot_dir, config)
        console.print(f"[green]Saved[/] ({strategy.name}): {branch}")
    except Exception as e:
        console.print(f"[yellow]Warning:[/] Could not save '{branch}': {e}")
        _print_superuser_hint(e, config.database_url)


def _try_restore(backend: DatabaseBackend, config: GitDbConfig, branch: str) -> None:
    """
    Attempt to restore a snapshot.
    """
    try:
        strategy = backend.detect_strategy(config)
        strategy.restore(config.database_url, branch, config.snapshot_dir, config)
        console.print(f"[green]Restored[/] database for '{branch}'")
    except Exception as e:
        console.print(f"[yellow]Warning:[/] Could not restore '{branch}': {e}")
        _print_superuser_hint(e, config.database_url)


def _print_superuser_hint(error: Exception, db_url: str) -> None:
    """
    Print actionable hints for common permission errors.
    """
    msg = str(error)
    if "superuser" not in msg or "terminate" not in msg:
        return

    params = parse_database_url(db_url)
    username = params.get("user") or "your_user"
    dbname = params.get("dbname") or "your_db"

    console.print(
        "  [dim]A superuser connection (e.g., pgAdmin) is blocking this operation.[/]\n"
        "  [dim]Fix options:[/]\n"
        f"  [dim]  1. Terminate other sessions:[/]\n"
        f'  [dim]     psql -c "SELECT pg_terminate_backend(pid) '
        f"FROM pg_stat_activity WHERE datname = '{dbname}' "
        f'AND pid <> pg_backend_pid();"[/]\n'
        f"  [dim]  2. Grant terminate privilege (permanent fix):[/]\n"
        f'  [dim]     psql -c "GRANT pg_signal_backend TO {username};"[/]'
    )


def _handle_per_branch_checkout(
    config: GitDbConfig,
    git_dir: Path,
    prev_branch: str | None,
    curr_branch: str | None,
) -> None:
    """
    Handle post-checkout in per-branch mode.
    """
    if not curr_branch:
        return

    backend = get_backend(config.database_url)
    params = backend.apply_url_defaults(parse_database_url(config.database_url))
    dbname = str(params["dbname"])
    manager = backend.branch_db_manager(config)

    target_db = branch_db_name(
        curr_branch,
        dbname,
        config.default_branch,
        backend.max_identifier_length,
    )

    # Default branch uses the seed DB directly
    if curr_branch == config.default_branch:
        console.print(f"[dim]Branch database:[/] {target_db}")
        return

    # Check if branch DB already exists
    entry = get_branch_db(git_dir, curr_branch)
    if entry and manager.exists(entry.db_name):
        console.print(f"[dim]Branch database:[/] {target_db}")
        return

    source_db = dbname
    created_from = config.default_branch

    if prev_branch:
        prev_db = branch_db_name(
            prev_branch,
            dbname,
            config.default_branch,
            backend.max_identifier_length,
        )
        if manager.exists(prev_db):
            source_db = prev_db
            created_from = prev_branch

    try:
        manager.create(target_db, source_db, curr_branch, created_from, git_dir)
        console.print(f"[green]Created database:[/] {target_db}")
    except Exception as e:
        console.print(f"[yellow]git-db warning:[/] Could not create '{target_db}': {e}")
        return

    console.print(f"[dim]Branch database:[/] {target_db}")


def _resolve_git_db_executable() -> str:
    """
    Return the executable path the hook should call.
    """
    current = Path(sys.argv[0])
    if current.name == "git-db" and current.exists():
        return str(current.resolve())
    found = shutil.which("git-db")
    if found:
        return str(Path(found).resolve())
    return ""


def _write_managed_hook(hooks_dir: Path, hook_name: str, content: str) -> None:
    hook_path = hooks_dir / hook_name
    if hook_path.exists():
        existing = hook_path.read_text()
        if HOOK_IDENTIFIER not in existing:
            legacy_path = hooks_dir / f"{hook_name}.legacy"
            hook_path.rename(legacy_path)
            console.print(f"Existing {hook_name} hook preserved as {hook_name}.legacy")

    hook_path.write_text(content)
    os.chmod(hook_path, 0o755)
