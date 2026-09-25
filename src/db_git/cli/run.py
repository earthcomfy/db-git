from __future__ import annotations

import os
from typing import Annotated

import typer

from db_git.backends import get_backend
from db_git.config import load_config
from db_git.errors import DbGitError
from db_git.git import get_current_branch, get_git_dir
from db_git.workflow import application_url

from ._common import require_init
from ._console import app, console


@app.command(context_settings={"allow_interspersed_args": False})
def run(
    command: Annotated[
        list[str], typer.Argument(help="Command and arguments after --.")
    ],
    database_url: Annotated[
        str | None,
        typer.Option("--database-url", help="Override the seed connection URL."),
    ] = None,
) -> None:
    """Run a command with DATABASE_URL set to the current branch's database."""
    require_init()
    try:
        config = load_config({"database_url": database_url})
        git_dir = get_git_dir()
        if git_dir is None:
            raise DbGitError("Not inside a Git repository.")
        url = application_url(
            config, get_backend(config.database_url), git_dir, get_current_branch()
        )
        env = {
            **os.environ,
            "DATABASE_URL": url,
            "DB_GIT_DATABASE_URL": config.database_url,
        }
    except (DbGitError, OSError) as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(1) from e
    except (ValueError, TypeError) as e:
        console.print(
            "[red]Error:[/] Invalid connection URL or local state. Run db-git doctor."
        )
        raise typer.Exit(1) from e
    # On POSIX, replace this process so the child owns its exit status and signals.
    # Do not use a shell or print the command/environment (which may hold secrets).
    try:
        os.execvpe(command[0], command, env)
    except FileNotFoundError as e:
        console.print("[red]Error:[/] Command not found in PATH.")
        raise typer.Exit(127) from e
    except OSError as e:
        console.print(
            "[red]Error:[/] Could not execute command. "
            "Check its permissions and format."
        )
        raise typer.Exit(126) from e
