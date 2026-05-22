from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
from psycopg import sql

from git_db.backends import DatabaseBackend, DbConnection
from git_db.backends.postgresql.connections import handle_active_connections
from git_db.db import parse_database_url
from git_db.errors import DatabaseError, SnapshotError, TerminationTimeout
from git_db.storage import (
    make_metadata,
    metadata_path,
    snapshot_db_name,
    write_metadata,
)

if TYPE_CHECKING:
    from git_db.config import GitDbConfig


class TemplateStrategy:
    """
    Snapshot strategy using PostgreSQL template databases.
    """

    name = "template"

    def __init__(self, backend: DatabaseBackend) -> None:
        self._backend = backend

    def save(
        self,
        db_url: str,
        branch: str,
        snapshot_dir: Path,
        config: GitDbConfig,
    ) -> None:
        params = self._backend.apply_url_defaults(parse_database_url(db_url))
        dbname = str(params["dbname"])
        snapshot_name = snapshot_db_name(
            branch,
            dbname,
            self._backend.max_identifier_length,
        )

        conn = self._backend.connect_maintenance(params)
        try:
            handle_active_connections(conn, dbname, config)
            handle_active_connections(conn, snapshot_name, config)

            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(snapshot_name)
                )
            )

            _create_from_template(conn, snapshot_name, dbname)
        except (psycopg.Error, DatabaseError) as e:
            raise SnapshotError(f"Template save failed: {e}") from e
        finally:
            conn.close()

        write_metadata(
            snapshot_dir,
            make_metadata(
                branch=branch,
                database=dbname,
                strategy=self.name,
                engine=self._backend.engine,
                engine_version="",
                file_size_bytes=None,
            ),
        )

    def restore(
        self,
        db_url: str,
        branch: str,
        snapshot_dir: Path,
        config: GitDbConfig,
    ) -> None:
        params = self._backend.apply_url_defaults(parse_database_url(db_url))
        dbname = str(params["dbname"])
        snapshot_name = snapshot_db_name(
            branch,
            dbname,
            self._backend.max_identifier_length,
        )

        conn = self._backend.connect_maintenance(params)
        try:
            handle_active_connections(conn, dbname, config)

            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(dbname)
                )
            )

            _create_from_template(conn, dbname, snapshot_name)
        except (psycopg.Error, DatabaseError, TerminationTimeout) as e:
            raise SnapshotError(f"Template restore failed: {e}") from e
        finally:
            conn.close()

    def cleanup(
        self,
        branch: str,
        snapshot_dir: Path,
        config: GitDbConfig,
    ) -> None:
        """
        Drop the snapshot database and remove the metadata file.
        """
        params = self._backend.apply_url_defaults(
            parse_database_url(config.database_url)
        )
        dbname = str(params["dbname"])
        snapshot_name = snapshot_db_name(
            branch,
            dbname,
            self._backend.max_identifier_length,
        )

        try:
            conn = self._backend.connect_maintenance(params)
            try:
                handle_active_connections(conn, snapshot_name, config)
                conn.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(snapshot_name)
                    )
                )
            finally:
                conn.close()
        except DatabaseError:
            pass

        meta = metadata_path(snapshot_dir, branch)
        if meta.exists():
            meta.unlink()


def _create_from_template(
    conn: DbConnection,
    target: str,
    template: str,
) -> None:
    """
    Issue CREATE DATABASE using PostgreSQL's default copy strategy.
    """
    conn.execute(
        sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
            sql.Identifier(target),
            sql.Identifier(template),
        )
    )
