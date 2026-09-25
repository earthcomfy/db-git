from __future__ import annotations

import json
from typing import Annotated

import typer

from db_git.doctor import diagnose

from ._console import app


@app.command()
def doctor(
    json_output: Annotated[
        bool, typer.Option("--json", help="Print a machine-readable report.")
    ] = False,
    strict: Annotated[
        bool, typer.Option("--strict", help="Treat warnings as failures.")
    ] = False,
    database_url: Annotated[str | None, typer.Option("--database-url")] = None,
) -> None:
    """Check configuration, connections, permissions, hooks, and state (read-only)."""
    report = diagnose(database_url)
    if json_output:
        typer.echo(json.dumps(report.to_dict(strict), indent=2))
    else:
        for check in report.checks:
            typer.echo(f"[{check.status.upper()}] {check.id}: {check.message}")
            if check.remedy:
                typer.echo(f"  Fix: {check.remedy}")
    raise typer.Exit(report.exit_code(strict))
