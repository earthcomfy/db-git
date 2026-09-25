from __future__ import annotations

from pathlib import Path

import typer

from db_git.backends.mysql.operations import (
    MySQLResources,
    operation_scope,
    server_identity,
    validate_generation,
)
from db_git.backends.mysql.shared import active_path
from db_git.backends.mysql.urls import parse_url
from db_git.config import DbGitConfig
from db_git.errors import DbGitError
from db_git.recovery import discard, finish, operations, rollback
from db_git.resources import FileResources
from db_git.state import state_path


def recover_operation(
    config: DbGitConfig,
    git_dir: Path,
    root: Path,
    identifier: str,
    action: str,
    yes: bool,
) -> None:
    params = {k: v for k, v in parse_url(config.database_url).items() if v is not None}
    seed = str(params["dbname"])
    with operation_scope(params, root, recovering=True) as conn:
        records = operations(root)
        record = next((record for record in records if record.id == identifier), None)
        if record is None:
            raise DbGitError(f"Unknown recovery operation: {identifier}")
        pending = [r.id for r in records if r.phase not in {"complete", "rolled_back"}]
        if pending and identifier not in pending:
            raise DbGitError(f"Resolve interrupted operation {pending[0]} first")
        if record.kind not in {"mysql", "file"} or record.server != server_identity(
            conn, seed
        ):
            raise DbGitError(
                "Recovery record belongs to another MySQL server, seed, or engine."
            )
        if record.kind == "file":
            if config.mode != "shared":
                raise DbGitError("MySQL snapshot recovery requires shared mode.")
            if Path(record.target).parent.resolve() != config.snapshot_dir.resolve():
                raise DbGitError("Snapshot recovery target is outside its directory.")
            for name in (record.stage, record.backup):
                if Path(name).parent.resolve() != root.resolve():
                    raise DbGitError(
                        "Snapshot recovery resources are outside their directory."
                    )
            if record.metadata_path != str(
                Path(record.target).with_suffix(".meta.json").resolve()
            ):
                raise DbGitError(
                    "Snapshot recovery metadata does not match its target."
                )
        else:
            expected = (
                active_path(config.snapshot_dir)
                if config.mode == "shared"
                else state_path(git_dir)
            )
            if record.metadata_path != str(expected.resolve()):
                raise DbGitError(
                    "MySQL recovery metadata path does not match this repository."
                )
            for name in (record.target, record.stage, record.backup):
                if name == seed and name == record.target and config.mode == "shared":
                    continue
                validate_generation(name, seed)
        if not yes and not typer.confirm(
            f"{action.capitalize()} MySQL {record.action} on {record.target}?"
        ):
            return
        resource = FileResources() if record.kind == "file" else MySQLResources(conn)
        if action == "finish":
            finish(root, resource, record)
        elif action == "rollback":
            rollback(root, resource, record)
        else:
            discard(root, resource, record)
