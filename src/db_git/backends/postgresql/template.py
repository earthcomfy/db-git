from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
from psycopg import sql

from db_git.backends import DatabaseBackend, DbConnection
from db_git.backends.postgresql.connections import handle_active_connections
from db_git.backends.postgresql.operations import (
    PostgresResources,
    operation_scope,
    resource_names,
    server_identity,
)
from db_git.db import parse_database_url
from db_git.errors import SnapshotError
from db_git.recovery import run_operation
from db_git.storage import make_metadata, metadata_path, read_metadata, snapshot_db_name

if TYPE_CHECKING:
    from db_git.config import DbGitConfig


class TemplateStrategy:
    name = "template"

    def __init__(self, backend: DatabaseBackend) -> None:
        self._backend = backend

    def save(
        self, db_url: str, branch: str, snapshot_dir: Path, config: DbGitConfig
    ) -> None:
        params = self._backend.apply_url_defaults(parse_database_url(db_url))
        root = snapshot_dir / ".operations"
        try:
            with operation_scope(self._backend, params, root) as conn:
                target = snapshot_db_name(
                    branch,
                    str(params["dbname"]),
                    self._backend.max_identifier_length,
                    snapshot_dir=snapshot_dir,
                )
                resources = PostgresResources(conn, config)
                if (
                    read_metadata(snapshot_dir, branch) is None
                    and resources.identity(target) is not None
                ):
                    raise SnapshotError(
                        f"Refusing to replace untracked snapshot database {target}"
                    )
                meta = make_metadata(
                    branch, str(params["dbname"]), self.name, self._backend.engine, ""
                )

                def build(stage: str) -> None:
                    handle_active_connections(conn, str(params["dbname"]), config)
                    _create_from_template(conn, stage, str(params["dbname"]))

                run_operation(
                    root,
                    resources,
                    action="save",
                    kind="database",
                    server=server_identity(params),
                    target=target,
                    names=lambda identifier: resource_names(params, identifier),
                    metadata=metadata_path(snapshot_dir, branch),
                    build=build,
                    after=lambda _: json.dumps(asdict(meta), indent=2) + "\n",
                )
        except psycopg.Error as e:
            raise SnapshotError(f"Template save failed: {e}") from e

    def restore(
        self, db_url: str, branch: str, snapshot_dir: Path, config: DbGitConfig
    ) -> None:
        params = self._backend.apply_url_defaults(parse_database_url(db_url))
        root = snapshot_dir / ".operations"
        try:
            with operation_scope(self._backend, params, root) as conn:
                source = snapshot_db_name(
                    branch,
                    str(params["dbname"]),
                    self._backend.max_identifier_length,
                    snapshot_dir=snapshot_dir,
                )

                def build(stage: str) -> None:
                    handle_active_connections(conn, source, config)
                    _create_from_template(conn, stage, source)

                run_operation(
                    root,
                    PostgresResources(conn, config),
                    action="restore",
                    kind="database",
                    server=server_identity(params),
                    target=str(params["dbname"]),
                    names=lambda identifier: resource_names(params, identifier),
                    metadata=None,
                    build=build,
                    after=lambda _: None,
                )
        except psycopg.Error as e:
            raise SnapshotError(f"Template restore failed: {e}") from e

    def cleanup(self, branch: str, snapshot_dir: Path, config: DbGitConfig) -> None:
        params = self._backend.apply_url_defaults(
            parse_database_url(config.database_url)
        )
        root = snapshot_dir / ".operations"
        with operation_scope(self._backend, params, root) as conn:
            if read_metadata(snapshot_dir, branch) is None:
                return
            target = snapshot_db_name(
                branch,
                str(params["dbname"]),
                self._backend.max_identifier_length,
                snapshot_dir=snapshot_dir,
            )
            run_operation(
                root,
                PostgresResources(conn, config),
                action="prune",
                kind="database",
                server=server_identity(params),
                target=target,
                names=lambda identifier: resource_names(params, identifier),
                metadata=metadata_path(snapshot_dir, branch),
                build=None,
                after=lambda _: None,
            )


def _create_from_template(conn: DbConnection, target: str, template: str) -> None:
    conn.execute(
        sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
            sql.Identifier(target), sql.Identifier(template)
        )
    )
