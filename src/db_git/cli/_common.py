from __future__ import annotations

import os
from pathlib import Path

import typer

from db_git.config import find_project_root
from db_git.errors import DbGitError
from db_git.git import get_git_dir
from db_git.repository import configuration_root

from ._console import console


def check_enabled() -> str:
    """
    Return a Rich-formatted string showing enabled/disabled status.
    """
    git_dir = get_git_dir()
    if git_dir and (git_dir / "db-git" / "disabled").exists():
        return "[yellow]no[/] (run [cyan]db-git enable[/] to re-enable)"
    return "[green]yes[/]"


def require_init() -> Path:
    """
    Ensure db-git is initialized; return the shared configuration root.
    """
    root = find_project_root()
    try:
        if root is not None:
            root = configuration_root(root)
    except DbGitError as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(1) from e
    if root is None or not (root / ".db-git.toml").exists():
        console.print(
            "[red]Error:[/] db-git is not initialized. Run [cyan]db-git init[/] first."
        )
        raise typer.Exit(1)
    return root


def debug_enabled() -> bool:
    """
    Whether DB_GIT_DEBUG is set to a truthy value.
    """
    return os.environ.get("DB_GIT_DEBUG", "").lower() in ("1", "true", "yes")
