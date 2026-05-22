from __future__ import annotations

import os
from pathlib import Path

import typer

from git_db.config import find_project_root
from git_db.git import get_git_dir

from ._console import console


def check_enabled() -> str:
    """
    Return a Rich-formatted string showing enabled/disabled status.
    """
    git_dir = get_git_dir()
    if git_dir and (git_dir / "git-db" / "disabled").exists():
        return "[yellow]no[/] (run [cyan]git-db enable[/] to re-enable)"
    return "[green]yes[/]"


def require_init() -> Path:
    """
    Ensure git-db has been initialized. Returns project root.
    """
    root = find_project_root()
    if root is None or not (root / ".git-db.toml").exists():
        console.print(
            "[red]Error:[/] git-db is not initialized. Run [cyan]git-db init[/] first."
        )
        raise typer.Exit(1)
    return root


def debug_enabled() -> bool:
    """
    Whether GIT_DB_DEBUG is set to a truthy value.
    """
    return os.environ.get("GIT_DB_DEBUG", "").lower() in ("1", "true", "yes")
