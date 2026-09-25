from __future__ import annotations

from typing import Annotated

import psycopg
import typer

from db_git.backends import get_backend
from db_git.backends.postgresql.operations import (
    PostgresResources,
    operation_scope,
    server_identity,
)
from db_git.config import load_config
from db_git.db import parse_database_url
from db_git.errors import DbGitError
from db_git.git import get_git_dir
from db_git.recovery import discard, finish, operations, rollback
from db_git.repository import operations_directory
from db_git.resources import FileResources

from ._common import require_init
from ._console import app, console


@app.command()
def recover(
    operation: Annotated[
        str | None, typer.Argument(help="Operation ID from db-git recover.")
    ] = None,
    finish_operation: Annotated[
        bool,
        typer.Option(
            "--finish", help="Publish a fully built replacement and its metadata."
        ),
    ] = False,
    rollback_operation: Annotated[
        bool,
        typer.Option(
            "--rollback", help="Restore the previous copy, retaining the replaced data."
        ),
    ] = False,
    discard_operation: Annotated[
        bool,
        typer.Option(
            "--discard",
            help="Permanently remove retained backups for a resolved operation.",
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip confirmation.")
    ] = False,
) -> None:
    """List recovery records, or finish, roll back, or discard one operation."""
    require_init()
    try:
        config = load_config()
        git_dir = get_git_dir()
        if git_dir is None:
            raise DbGitError("Not inside a Git repository")
        root = (
            (config.snapshot_dir / ".operations")
            if config.mode == "shared"
            else (operations_directory(git_dir))
        )
        actions = sum((finish_operation, rollback_operation, discard_operation))
        if operation is None:
            if actions:
                raise DbGitError("Specify an operation ID with a recovery action")
            records = operations(root)
            if not records:
                console.print("No recovery records.")
            for entry in records:
                console.print(
                    f"{entry.id}  {entry.action}  {entry.phase}", markup=False
                )
                console.print(
                    f"  Created: {entry.created_at}\n"
                    f"  Target: {entry.target}\n  Staged: {entry.stage}\n"
                    f"  Backup: {entry.backup}",
                    markup=False,
                )
            if records:
                console.print(
                    "Use an ID with --finish, --rollback, or --discard. "
                    "Backups are retained until discarded."
                )
            return
        if actions != 1:
            raise DbGitError("Choose exactly one of --finish, --rollback, or --discard")
        backend = get_backend(config.database_url)
        params = backend.apply_url_defaults(parse_database_url(config.database_url))
        with operation_scope(backend, params, root, recovering=True) as conn:
            records = operations(root)
            record = next((r for r in records if r.id == operation), None)
            if record is None:
                raise DbGitError(f"Unknown recovery operation: {operation}")
            pending = [
                r.id for r in records if r.phase not in {"complete", "rolled_back"}
            ]
            if pending and operation not in pending:
                raise DbGitError(f"Resolve interrupted operation {pending[0]} first")
            if record.server != server_identity(params):
                raise DbGitError(
                    "Recovery record belongs to a different database connection"
                )
            if record.kind not in {"file", "database"}:
                raise DbGitError("Unknown recovery resource kind")
            action = (
                "finish"
                if finish_operation
                else "roll back"
                if rollback_operation
                else "discard backups for"
            )
            if not yes and not typer.confirm(
                f"{action.capitalize()} {record.action} on {record.target}?"
            ):
                return
            resources = (
                FileResources()
                if record.kind == "file"
                else PostgresResources(conn, config)
            )
            if finish_operation:
                finish(root, resources, record)
            elif rollback_operation:
                rollback(root, resources, record)
            else:
                discard(root, resources, record)
        console.print(f"Recovery action completed for {operation}.")
    except (DbGitError, OSError, psycopg.Error) as e:
        console.print(f"Error: {e}", markup=False)
        raise typer.Exit(1) from e
