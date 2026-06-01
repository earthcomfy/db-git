from __future__ import annotations

import typer

from db_git.config import load_config
from db_git.errors import DbGitError
from db_git.git import get_git_dir, handle_post_checkout, install_hook, remove_hook

from ._console import app, console, hook_app


@hook_app.command("install")
def hook_install() -> None:
    """
    Install the post-checkout git hook.
    """
    git_dir = get_git_dir()
    if git_dir is None:
        console.print("[red]Error:[/] Not inside a git repository.")
        raise typer.Exit(1)

    try:
        install_hook(git_dir)
        console.print("[green]Hook installed.[/]")
    except DbGitError as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(1) from e


@hook_app.command("remove")
def hook_remove() -> None:
    """
    Remove the post-checkout git hook.
    """
    git_dir = get_git_dir()
    if git_dir is None:
        console.print("[red]Error:[/] Not inside a git repository.")
        raise typer.Exit(1)

    try:
        remove_hook(git_dir)
        console.print("[green]Hook removed.[/]")
    except DbGitError as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(1) from e


@app.command()
def disable() -> None:
    """
    Temporarily disable db-git. Hook will skip all operations.
    """
    git_dir = get_git_dir()
    if git_dir is None:
        console.print("[red]Error:[/] Not inside a git repository.")
        raise typer.Exit(1)

    disabled_file = git_dir / "db-git" / "disabled"
    disabled_file.parent.mkdir(parents=True, exist_ok=True)
    disabled_file.touch()
    console.print(
        "[yellow]db-git disabled.[/] Run [cyan]db-git enable[/] to re-enable."
    )


@app.command()
def enable() -> None:
    """
    Re-enable db-git after it was disabled.
    """
    git_dir = get_git_dir()
    if git_dir is None:
        console.print("[red]Error:[/] Not inside a git repository.")
        raise typer.Exit(1)

    disabled_file = git_dir / "db-git" / "disabled"
    if disabled_file.exists():
        disabled_file.unlink()
        console.print("[green]db-git enabled.[/]")
    else:
        console.print("[dim]db-git is already enabled.[/]")


@app.command("_hook-dispatch", hidden=True)
def hook_dispatch(
    prev_head: str,
    new_head: str,
    is_branch: str,
) -> None:
    """
    Internal command called by the post-checkout hook.
    """
    try:
        git_dir = get_git_dir()
        if git_dir is None:
            return

        config = load_config()
        handle_post_checkout(prev_head, is_branch, config)

    except Exception as e:
        console.print(f"[yellow]db-git warning:[/] {e}")
