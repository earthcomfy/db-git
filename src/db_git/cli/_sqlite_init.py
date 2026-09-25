from __future__ import annotations

import subprocess
from pathlib import Path

from db_git.backends.sqlite.files import check_database
from db_git.backends.sqlite.urls import database_path, database_url
from db_git.config import ensure_config_ignored, load_config, write_config
from db_git.errors import ConfigError
from db_git.git import install_hook
from db_git.repository import operations_directory, shared_state_directory
from db_git.state import load_state

from ._console import console
from ._prompts import detect_default_branch


def initialize(
    root: Path,
    git_dir: Path,
    url: str,
    existing: dict[str, object],
    *,
    mode: str | None,
    strategy: str | None,
    policy: str | None,
    no_hook: bool,
) -> None:
    if mode is not None and mode != "per-branch":
        raise ConfigError(
            "SQLite supports per-branch mode; shared mode is not supported."
        )
    if strategy is not None and strategy != "backup":
        raise ConfigError("SQLite requires --strategy backup.")
    if policy is not None and policy != "fail":
        raise ConfigError(
            "SQLite cannot terminate connections; use --on-active-connections fail."
        )
    path = database_path(url, root)
    check_database(path)
    if path.is_relative_to(root):
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", str(path.relative_to(root))],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if tracked.returncode == 0:
            raise ConfigError(
                "The SQLite seed is tracked by Git. Use an untracked local database "
                "so checkout cannot replace a file an application has open."
            )
    # Detect legacy worktree metadata before writing a new configuration.
    common = shared_state_directory(git_dir)
    previous_url = str(existing.get("database_url") or "")
    if previous_url and previous_url.startswith("sqlite:"):
        previous_url = database_url(database_path(previous_url, root))
    snapshots = Path(str(existing.get("snapshot_dir") or common / "snapshots"))
    if not snapshots.is_absolute():
        snapshots = root / snapshots
    snapshot_roots = {snapshots, common / "snapshots"}
    if previous_url != database_url(path) and (
        load_state(git_dir).databases
        or any(operations_directory(git_dir).glob("*.json"))
        or any(
            any(location.glob("*.meta.json"))
            or any((location / ".operations").glob("*.json"))
            for location in snapshot_roots
        )
    ):
        raise ConfigError(
            "Existing db-git resources belong to another seed or engine. "
            "Keep their configuration for recovery; "
            "initialize SQLite in a separate repository."
        )
    updates: dict[str, object] = {
        "database_url": database_url(path),
        "mode": "per-branch",
        "strategy": "backup",
        "on_active_connections": "fail",
        "default_branch": existing.get("default_branch") or detect_default_branch(root),
    }
    load_config(updates, project_root=root)
    directory = common / "sqlite" / "branches"
    directory.mkdir(parents=True, exist_ok=True)
    write_config(root, updates)
    ensure_config_ignored(root)
    if not no_hook:
        install_hook(git_dir)
    console.print(
        "[green]Initialized SQLite per-branch databases.[/] "
        f"Configuration: {root / '.db-git.toml'}"
    )
    console.print(f"Seed: {path}\nBranch files: {directory}", markup=False)
    console.print(
        "Uses SQLite online backups. Reset/rollback select a file generation; "
        "restart applications through db-git run afterward. "
        "Published files are retained for manual cleanup."
    )
