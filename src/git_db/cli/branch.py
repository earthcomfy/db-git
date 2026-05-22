from __future__ import annotations

from typing import Annotated

import typer

from git_db.backends import get_backend
from git_db.config import load_config
from git_db.db import parse_database_url
from git_db.errors import GitDbError
from git_db.git import get_current_branch, get_git_dir
from git_db.storage import branch_db_name

from ._common import debug_enabled, require_init
from ._console import app, console


@app.command()
def create(
    branch: Annotated[str | None, typer.Argument()] = None,
    database_url: Annotated[
        str | None, typer.Option("--database-url", envvar="DATABASE_URL")
    ] = None,
) -> None:
    """
    Proactively create a per-branch database before checkout.

    Per-branch mode only.
    """
    require_init()
    try:
        config = load_config(cli_overrides={"database_url": database_url})

        if config.mode != "per-branch":
            console.print(
                "Shared mode: use [cyan]git-db save[/] to snapshot the database."
            )
            return

        git_dir = get_git_dir()
        if git_dir is None:
            console.print("[red]Error:[/] Not inside a git repository.")
            raise typer.Exit(1)

        if branch is None:
            branch = get_current_branch()
            if branch is None:
                console.print("[red]Error:[/] HEAD is detached. Specify a branch name.")
                raise typer.Exit(1)

        backend = get_backend(config.database_url)
        params = backend.apply_url_defaults(parse_database_url(config.database_url))
        dbname = str(params["dbname"])
        manager = backend.branch_db_manager(config)
        target_db = branch_db_name(
            branch,
            dbname,
            config.default_branch,
            backend.max_identifier_length,
        )

        if manager.exists(target_db):
            console.print(
                f"[yellow]Branch database '{target_db}' already exists.[/] "
                "Use [cyan]git-db reset[/] to recreate from seed."
            )
            raise typer.Exit(1)

        source_db = dbname
        created_from = config.default_branch

        current = get_current_branch()
        if current:
            candidate = branch_db_name(
                current,
                dbname,
                config.default_branch,
                backend.max_identifier_length,
            )
            if manager.exists(candidate):
                source_db = candidate
                created_from = current

        manager.create(target_db, source_db, branch, created_from, git_dir)
        console.print(f"[green]Created[/] database: {target_db}")
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
def reset(
    branch: Annotated[str | None, typer.Argument()] = None,
    database_url: Annotated[
        str | None, typer.Option("--database-url", envvar="DATABASE_URL")
    ] = None,
) -> None:
    """
    Drop and recreate a branch database from seed (per-branch mode only).
    """
    require_init()
    try:
        config = load_config(cli_overrides={"database_url": database_url})

        if config.mode != "per-branch":
            console.print(
                "Shared mode: use [cyan]git-db restore[/] to restore a snapshot."
            )
            return

        git_dir = get_git_dir()
        if git_dir is None:
            console.print("[red]Error:[/] Not inside a git repository.")
            raise typer.Exit(1)

        if branch is None:
            branch = get_current_branch()
            if branch is None:
                console.print("[red]Error:[/] HEAD is detached. Specify a branch name.")
                raise typer.Exit(1)

        backend = get_backend(config.database_url)
        params = backend.apply_url_defaults(parse_database_url(config.database_url))
        dbname = str(params["dbname"])
        manager = backend.branch_db_manager(config)
        target_db = branch_db_name(
            branch,
            dbname,
            config.default_branch,
            backend.max_identifier_length,
        )
        seed_db = dbname

        if branch == config.default_branch:
            console.print(
                "[yellow]Cannot reset the default branch database.[/] "
                "It is the seed for all other branches."
            )
            raise typer.Exit(1)

        if manager.exists(target_db):
            manager.drop(target_db, branch, git_dir)

        manager.create(target_db, seed_db, branch, config.default_branch, git_dir)
        console.print(f"[green]Reset[/] database '{target_db}' from seed '{seed_db}'")
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
