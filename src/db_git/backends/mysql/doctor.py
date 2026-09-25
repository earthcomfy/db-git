from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from db_git.backends.mysql.backend import MySQLBackend
from db_git.backends.mysql.connections import Connection, check_clients, server_version
from db_git.backends.mysql.dump import check_permissions, validate_source
from db_git.backends.mysql.objects import capture_objects
from db_git.backends.mysql.operations import (
    MySQLResources,
    generation,
    validate_generation,
)
from db_git.backends.mysql.urls import parse_url
from db_git.errors import DbGitError
from db_git.state import get_branch_db, load_state

if TYPE_CHECKING:
    from db_git.config import DbGitConfig
    from db_git.doctor import Report


def check(report: Report, config: DbGitConfig, git_dir: Path | None) -> None:
    try:
        check_clients()
        report.add("clients", "ok", "MySQL dump and restore clients are available.")
    except DbGitError as e:
        report.add("clients", "error", str(e))
    params = MySQLBackend().apply_url_defaults(parse_url(config.database_url))
    seed = str(params["dbname"])
    try:
        conn = Connection(params)
        try:
            version = server_version(conn)
            report.add(
                "engine", "ok", f"MySQL {version}; {config.mode} generation support."
            )
            check_permissions(
                conn, str(params["dbname"]), generation(str(params["dbname"]), "check")
            )
            report.add(
                "permissions",
                "ok",
                "Required effective schema and backup privileges are present.",
            )
            validate_source(conn, seed, seed)
            capture_objects(conn, seed)
            resources = MySQLResources(conn)
            if resources.identity(seed) is None:
                raise DbGitError("MySQL seed database is missing.")
            report.add(
                "database.seed", "ok", "Seed exists and uses supported schema objects."
            )
            if config.mode == "shared":
                from db_git.backends.mysql.shared import active_database
                from db_git.repository import require_safe_shared_mode
                from db_git.storage import list_snapshots, snapshot_dump_path

                require_safe_shared_mode(config.mode)
                current = active_database(config.snapshot_dir, seed, conn)
                report.add("database.active", "ok", f"Active MySQL database: {current}")
                for snapshot in list_snapshots(config.snapshot_dir):
                    if (
                        snapshot.engine != "mysql"
                        or snapshot.database != seed
                        or not snapshot_dump_path(
                            config.snapshot_dir, snapshot.branch
                        ).is_file()
                    ):
                        raise DbGitError(
                            "MySQL snapshot metadata or storage "
                            "is missing or mismatched."
                        )
            if git_dir is not None and config.mode == "per-branch":
                state = load_state(git_dir)
                if state.mode != config.mode:
                    raise DbGitError("Recorded mode differs from MySQL configuration.")
                for branch, entry in state.databases.items():
                    get_branch_db(git_dir, branch)
                    validate_generation(entry.db_name, seed)
                    if branch == config.default_branch or resources.identity(
                        entry.db_name
                    ) in {None, "unmanaged"}:
                        raise DbGitError(
                            "MySQL branch ownership is missing, invalid, or conflicts "
                            "with the seed."
                        )
                report.add(
                    "state",
                    "ok",
                    f"{len(state.databases)} MySQL generation(s) recorded.",
                )
            report.add(
                "capabilities",
                "ok",
                "Shared restore and per-branch reset select a new database URL. "
                "Retained generations require manual cleanup after applications "
                "disconnect. Copied events remain disabled.",
            )
        finally:
            conn.close()
    except DbGitError as e:
        report.add(
            "mysql",
            "error",
            str(e),
            "Check configuration and server grants; preserve generations "
            "and recovery journals.",
        )
