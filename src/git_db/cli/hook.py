from __future__ import annotations

import typer

from git_db.config import load_config
from git_db.errors import GitDbError
from git_db.git import get_git_dir, handle_post_checkout, install_hook, remove_hook

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
    except GitDbError as e:
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
    except GitDbError as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(1) from e


@app.command()
def disable() -> None:
    """
    Temporarily disable git-db. Hook will skip all operations.
    """
    git_dir = get_git_dir()
    if git_dir is None:
        console.print("[red]Error:[/] Not inside a git repository.")
        raise typer.Exit(1)

    disabled_file = git_dir / "git-db" / "disabled"
    disabled_file.parent.mkdir(parents=True, exist_ok=True)
    disabled_file.touch()
    console.print(
        "[yellow]git-db disabled.[/] Run [cyan]git-db enable[/] to re-enable."
    )


@app.command()
def enable() -> None:
    """
    Re-enable git-db after it was disabled.
    """
    git_dir = get_git_dir()
    if git_dir is None:
        console.print("[red]Error:[/] Not inside a git repository.")
        raise typer.Exit(1)

    disabled_file = git_dir / "git-db" / "disabled"
    if disabled_file.exists():
        disabled_file.unlink()
        console.print("[green]git-db enabled.[/]")
    else:
        console.print("[dim]git-db is already enabled.[/]")


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
        console.print(f"[yellow]git-db warning:[/] {e}")
