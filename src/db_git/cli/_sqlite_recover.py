from __future__ import annotations

import json
from pathlib import Path

import typer

from db_git.backends.sqlite.branch_db import validate_branch_path
from db_git.backends.sqlite.files import check_database
from db_git.backends.sqlite.operations import (
    SQLiteResources,
    operation_scope,
    server_identity,
)
from db_git.backends.sqlite.urls import database_path
from db_git.config import DbGitConfig
from db_git.errors import DbGitError
from db_git.recovery import discard, finish, operations, rollback
from db_git.state import state_path


def recover_operation(
    config: DbGitConfig,
    git_dir: Path,
    root: Path,
    identifier: str,
    action: str,
    yes: bool,
) -> None:
    with operation_scope(root, recovering=True):
        records = operations(root)
        record = next((record for record in records if record.id == identifier), None)
        if record is None:
            raise DbGitError(f"Unknown recovery operation: {identifier}")
        pending = [r.id for r in records if r.phase not in {"complete", "rolled_back"}]
        if pending and identifier not in pending:
            raise DbGitError(f"Resolve interrupted operation {pending[0]} first")
        if record.kind != "sqlite" or record.server != server_identity(
            str(database_path(config.database_url))
        ):
            raise DbGitError("Recovery record belongs to another database or engine.")
        if record.metadata_path != str(state_path(git_dir).resolve()):
            raise DbGitError(
                "SQLite recovery metadata path does not match this repository."
            )
        for name in (record.target, record.stage, record.backup):
            validate_branch_path(name, git_dir)
        if not yes and not typer.confirm(
            f"{action.capitalize()} SQLite {record.action} on {record.target}?"
        ):
            return
        resource = SQLiteResources()
        if action == "rollback" and record.before:
            try:
                previous = json.loads(record.before)["databases"]
                after = json.loads(record.after)["databases"] if record.after else {}
                for branch, entry in previous.items():
                    if entry != after.get(branch):
                        path = validate_branch_path(entry["db_name"], git_dir)
                        check_database(path)
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                raise DbGitError(
                    "Invalid previous SQLite ownership; preserve the journal."
                ) from e
        if action == "finish":
            finish(root, resource, record)
        elif action == "rollback":
            rollback(root, resource, record)
        else:
            discard(root, resource, record)
