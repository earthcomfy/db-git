from __future__ import annotations

from typing import Annotated

import typer

from db_git.backends import get_backend
from db_git.config import load_config
from db_git.db import parse_database_url
from db_git.errors import DbGitError
from db_git.git import get_current_branch, get_git_dir
from db_git.state import get_branch_db
from db_git.storage import branch_db_name
from db_git.workflow import owned_database

from ._common import debug_enabled, require_init
from ._console import app, console


@app.command()
def create(
    branch: Annotated[str | None, typer.Argument()] = None,
    from_branch: Annotated[
        str | None,
        typer.Option("--from", help="Clone this branch's existing database."),
    ] = None,
    database_url: Annotated[
        str | None,
        typer.Option("--database-url"),
    ] = None,
) -> None:
    """
    Create a per-branch database, optionally choosing its source with --from.
    """
    require_init()
    try:
        config = load_config(cli_overrides={"database_url": database_url})

        if config.mode != "per-branch":
            if from_branch is not None:
                raise DbGitError("create --from requires per-branch mode.")
            console.print(
                "Shared mode: use [cyan]db-git save[/] to snapshot the database."
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
            git_dir=git_dir,
            engine=backend.engine,
        )

        if manager.exists(target_db):
            console.print(
                f"[yellow]Branch database '{target_db}' already exists.[/]\n"
                "Use [cyan]db-git reset[/] to recreate from seed."
            )
            raise typer.Exit(1)

        source_db = dbname
        created_from = config.default_branch

        current = get_current_branch()
        if from_branch is not None:
            source_db = owned_database(from_branch, config, backend, git_dir)
            if not manager.exists(source_db):
                raise DbGitError(
                    f"Source database for '{from_branch}' is missing. "
                    "Recover or create it before cloning."
                )
            created_from = from_branch
        elif current:
            # Use the current branch's recorded database when available;
            # otherwise keep the seed as the source.
            if current == config.default_branch or get_branch_db(git_dir, current):
                candidate = owned_database(current, config, backend, git_dir)
                if manager.exists(candidate):
                    source_db = candidate
                    created_from = current

        if source_db == target_db:
            raise DbGitError("Cannot clone a database onto itself.")

        manager.create(target_db, source_db, branch, created_from, git_dir)
        console.print(f"[green]Created[/] database: {target_db}")
    except DbGitError as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(1) from e
    except typer.Exit:
        raise
    except Exception as e:
        if debug_enabled():
            raise
        console.print(
            f"[red]Error:[/] Unexpected error: {e}\n"
            "[dim]Set DB_GIT_DEBUG=1 to see the full traceback.[/]"
        )
        raise typer.Exit(1) from e


@app.command()
def reset(
    branch: Annotated[str | None, typer.Argument()] = None,
    database_url: Annotated[
        str | None,
        typer.Option("--database-url"),
    ] = None,
) -> None:
    """
    Replace a branch database from seed, retaining a recovery copy.
    """
    require_init()
    try:
        config = load_config(cli_overrides={"database_url": database_url})

        if config.mode != "per-branch":
            console.print(
                "Shared mode: use [cyan]db-git restore[/] to restore a snapshot."
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
            git_dir=git_dir,
            engine=backend.engine,
        )
        seed_db = dbname

        if branch == config.default_branch:
            console.print(
                "[yellow]Cannot reset the default branch database.[/] "
                "It is the seed for all other branches."
            )
            raise typer.Exit(1)

        manager.reset(target_db, seed_db, branch, config.default_branch, git_dir)
        if backend.engine == "sqlite":
            entry = get_branch_db(git_dir, branch)
            assert entry is not None
            target_db = entry.db_name
            console.print(
                "[dim]Previous SQLite file retained. Restart applications "
                "through db-git run to use the new file.[/]"
            )
        console.print(f"[green]Reset[/] database '{target_db}' from seed '{seed_db}'")
    except DbGitError as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(1) from e
    except typer.Exit:
        raise
    except Exception as e:
        if debug_enabled():
            raise
        console.print(
            f"[red]Error:[/] Unexpected error: {e}\n"
            "[dim]Set DB_GIT_DEBUG=1 to see the full traceback.[/]"
        )
        raise typer.Exit(1) from e
