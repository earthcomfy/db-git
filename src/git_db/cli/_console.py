from __future__ import annotations

import typer
from rich.console import Console

app = typer.Typer(
    name="git-db",
    help=(
        "Keep your database in sync with your git branches. "
        "Currently supports PostgreSQL."
    ),
    rich_markup_mode="rich",
)

hook_app = typer.Typer(name="hook", help="Manage the post-checkout git hook.")
app.add_typer(hook_app)

console = Console(stderr=True)
