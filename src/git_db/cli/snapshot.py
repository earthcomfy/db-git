from __future__ import annotations

from typing import Annotated

import typer

from git_db.backends import get_backend
from git_db.config import load_config
from git_db.errors import GitDbError
from git_db.git import get_current_branch
from git_db.storage import has_snapshot

from ._common import debug_enabled, require_init
from ._console import app, console


@app.command()
def save(
    branch: Annotated[str | None, typer.Argument()] = None,
    database_url: Annotated[
        str | None, typer.Option("--database-url", envvar="DATABASE_URL")
    ] = None,
) -> None:
    """
    Manually snapshot the current database for a branch (shared mode only).
    """
    require_init()
    try:
        config = load_config(cli_overrides={"database_url": database_url})

        if config.mode == "per-branch":
            console.print(
                "Per-branch mode: your database persists automatically. "
                "No save needed.\n"
                "Use [cyan]git-db create[/] to proactively create a branch DB."
            )
            return

        if branch is None:
            branch = get_current_branch()
            if branch is None:
                console.print("[red]Error:[/] HEAD is detached. Specify a branch name.")
                raise typer.Exit(1)

        backend = get_backend(config.database_url)
        detected = backend.detect_strategy(config)

        detected.save(config.database_url, branch, config.snapshot_dir, config)
        console.print(f"[green]Saved[/] ({detected.name}): {branch}")
    except GitDbError as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(1) from e
    except Exception as e:
        if debug_enabled():
            raise
        console.print(
            f"[red]Error:[/] Unexpected error: {e}\n"
            "[dim]Set GIT_DB_DEBUG=1 to see the full traceback.[/]"
        )
        raise typer.Exit(1) from e


@app.command()
def restore(
    branch: Annotated[str | None, typer.Argument()] = None,
    database_url: Annotated[
        str | None, typer.Option("--database-url", envvar="DATABASE_URL")
    ] = None,
) -> None:
    """
    Manually restore the snapshot for a branch (shared mode only).
    """
    require_init()
    try:
        config = load_config(cli_overrides={"database_url": database_url})

        if config.mode == "per-branch":
            console.print(
                "Per-branch mode: use [cyan]git-db reset[/] to recreate "
                "a branch database from seed."
            )
            return

        if branch is None:
            branch = get_current_branch()
            if branch is None:
                console.print("[red]Error:[/] HEAD is detached. Specify a branch name.")
                raise typer.Exit(1)

        if not has_snapshot(config.snapshot_dir, branch):
            console.print(
                f"[yellow]No snapshot found for branch '{branch}'.[/] "
                "Database left unchanged."
            )
            raise typer.Exit(1)

        backend = get_backend(config.database_url)
        detected = backend.detect_strategy(config)
        detected.restore(config.database_url, branch, config.snapshot_dir, config)
        console.print(f"[green]Restored[/] database for '{branch}'")
    except GitDbError as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(1) from e
    except Exception as e:
        if debug_enabled():
            raise
        console.print(
            f"[red]Error:[/] Unexpected error: {e}\n"
            "[dim]Set GIT_DB_DEBUG=1 to see the full traceback.[/]"
        )
        raise typer.Exit(1) from e
