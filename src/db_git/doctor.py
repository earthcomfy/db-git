"""Read-only diagnostics. Reports intentionally omit URLs and raw driver errors."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import psycopg
from psycopg.conninfo import make_conninfo

from db_git.backends.postgresql.backend import PostgresqlBackend
from db_git.backends.postgresql.connections import client_dsn
from db_git.config import DbGitConfig, find_project_root, load_config
from db_git.db import parse_database_url
from db_git.errors import DbGitError
from db_git.git import get_git_dir
from db_git.hook_script import HOOK_IDENTIFIER
from db_git.recovery import operations
from db_git.repository import (
    common_git_directory,
    configuration_root,
    hook_path,
    operations_directory,
    require_safe_shared_mode,
)
from db_git.state import load_state, state_path
from db_git.storage import (
    SnapshotMetadata,
    metadata_path,
    snapshot_db_name,
    snapshot_dump_path,
)


@dataclass
class Check:
    id: str
    status: Literal["ok", "warning", "error", "skipped"]
    message: str
    remedy: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    configuration_error: bool = False

    def add(
        self,
        id: str,
        status: Literal["ok", "warning", "error", "skipped"],
        message: str,
        remedy: str = "",
    ) -> None:
        self.checks.append(Check(id, status, message, remedy))

    def exit_code(self, strict: bool = False) -> int:
        if self.configuration_error:
            return 2
        return int(
            any(
                c.status == "error" or (strict and c.status == "warning")
                for c in self.checks
            )
        )

    def to_dict(self, strict: bool = False) -> dict[str, object]:
        return {
            "schema_version": 1,
            "ok": self.exit_code(strict) == 0,
            "exit_code": self.exit_code(strict),
            "checks": [asdict(check) for check in self.checks],
        }


def diagnose(database_url: str | None = None) -> Report:
    report = Report()
    try:
        git_dir = get_git_dir()
        root = find_project_root()
        if git_dir is None or root is None:
            report.add(
                "repository",
                "error",
                "No Git working tree found.",
                "Run db-git doctor inside your project repository.",
            )
        else:
            report.add("repository", "ok", "Git working tree found.")
            _check_hooks(report, git_dir)
            if not (configuration_root(root) / ".db-git.toml").is_file():
                report.add(
                    "configuration.file",
                    "error",
                    "db-git has not been initialized.",
                    "Run db-git init to create the local project configuration.",
                )
    except (DbGitError, OSError, subprocess.SubprocessError):
        git_dir = None
        root = None
        report.add(
            "repository",
            "error",
            "Git could not inspect the repository.",
            "Install Git and run this command inside the project.",
        )

    try:
        config = load_config({"database_url": database_url}, project_root=root)
        backend = PostgresqlBackend()
        params = backend.apply_url_defaults(parse_database_url(config.database_url))
        if "connect_timeout" in params:
            # Validate before applying the diagnostic timeout.
            int(params["connect_timeout"])
        report.add(
            "configuration",
            "ok",
            f"Valid {config.mode}/{config.strategy} configuration.",
        )
    except (DbGitError, OSError, ValueError):
        report.configuration_error = True
        report.add(
            "configuration",
            "error",
            "Configuration or PostgreSQL URL is invalid.",
            "Check .db-git.toml syntax, database_url (including a database name), "
            "mode, strategy, positive limits, and DB_GIT_* overrides; "
            "run db-git init if not configured.",
        )
        report.add(
            "connection", "skipped", "Database checks require valid configuration."
        )
        return report

    try:
        require_safe_shared_mode(config.mode, root)
        report.add("worktrees", "ok", "Worktree layout is compatible with this mode.")
    except DbGitError as e:
        report.add("worktrees", "error", str(e), "Use per-branch mode with worktrees.")
    _check_storage(report, config, git_dir)
    versions = _check_clients(report, config, params)
    if (params.get("service") or os.environ.get("PGSERVICE")) and any(
        os.environ.get(key) for key in ("PGHOST", "PGHOSTADDR", "PGPORT")
    ):
        report.add(
            "connection.service_environment",
            "warning",
            "Inherited host/port variables may interfere with service resolution.",
            "Unset PGHOST, PGHOSTADDR, and PGPORT when using a service file; "
            "psycopg may resolve those defaults before libpq reads the service.",
        )
    # Keep diagnostics bounded even when the application's URL has no timeout.
    params = {**params, "connect_timeout": 5}
    databases: dict[str, tuple[bool, bool, bool]] | None = None
    catalog_started = False
    try:
        conn = backend.connect_maintenance(params)
        try:
            conn.execute("SET statement_timeout = '5s'")
            conn.execute("SET default_transaction_read_only = on")
            row = conn.execute("SHOW server_version_num").fetchone()
            if row is None:
                raise ValueError("missing server version")
            major = int(row[0]) // 10000
            report.add(
                "connection.maintenance",
                "ok",
                f"Connected to PostgreSQL {major} maintenance database.",
            )
            catalog_started = True
            _check_versions(report, versions, major)
            row = conn.execute(
                "SELECT rolcreatedb, rolsuper, "
                "pg_has_role(current_user, 'pg_signal_backend', 'MEMBER') "
                "FROM pg_roles WHERE rolname = current_user"
            ).fetchone()
            if row is None:
                raise ValueError("missing role")
            createdb, superuser, signal = map(bool, row)
            report.add(
                "permissions.createdb",
                "ok" if createdb or superuser else "error",
                "Role can create staging databases."
                if createdb or superuser
                else (
                    "Role lacks CREATEDB, required for cloning and staged restores "
                    "with either strategy."
                ),
                ""
                if createdb or superuser
                else "Ask an administrator to grant CREATEDB to this role.",
            )
            if config.on_active_connections == "terminate" and not (
                superuser or signal
            ):
                report.add(
                    "permissions.terminate",
                    "warning",
                    "Role may not terminate other roles' sessions.",
                    "Stop application connections first, use "
                    "on_active_connections='fail', "
                    "or request pg_signal_backend. Only superusers can terminate "
                    "superuser sessions.",
                )
            else:
                report.add(
                    "permissions.terminate",
                    "ok",
                    "Connection policy is compatible with the role's permissions.",
                )
            databases = {
                str(name): (bool(owner), bool(allow), bool(template))
                for name, owner, allow, template in conn.execute(
                    "SELECT datname, pg_has_role(datdba, 'USAGE'), datallowconn, "
                    "datistemplate "
                    "FROM pg_database"
                ).fetchall()
            }
            base = databases.get(str(params["dbname"]))
            if base is None:
                report.add(
                    "database.base",
                    "error",
                    "Configured database does not exist.",
                    "Correct database_url or create the seed database before using "
                    "db-git.",
                )
            elif (
                not base[0]
                and not superuser
                and (
                    config.mode == "shared"
                    or (config.strategy == "template" and not base[2])
                )
            ):
                report.add(
                    "permissions.owner",
                    "error",
                    "Role does not own the configured database.",
                    "Use its owner (or an inheriting owner role) for shared "
                    "replacement/template cloning.",
                )
            else:
                report.add(
                    "database.base",
                    "ok",
                    "Configured database exists; catalog ownership checks passed.",
                )
        finally:
            conn.close()
    except (DbGitError, psycopg.Error, OSError, ValueError):
        report.add(
            "connection.catalog" if catalog_started else "connection.maintenance",
            "error",
            "Maintenance connection or catalog inspection failed.",
            "Check host/port, credentials, SSL/service options, and CONNECT "
            "permission on postgres.",
        )

    try:
        with psycopg.connect(make_conninfo("", **params), autocommit=True) as conn:
            conn.execute("SET statement_timeout = '5s'")
            conn.execute("SET default_transaction_read_only = on")
            conn.execute("SELECT 1")
            if config.strategy == "pgdump":
                unreadable = conn.execute(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid = c.relnamespace "
                    "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                    "AND n.nspname NOT LIKE 'pg_toast%' AND ("
                    "(c.relkind IN ('r', 'p', 'm') AND NOT "
                    "has_table_privilege(c.oid, 'SELECT')) OR "
                    "(c.relkind = 'S' AND NOT has_sequence_privilege(c.oid, 'SELECT')))"
                ).fetchone()
                missing = int(unreadable[0]) if unreadable else 0
                report.add(
                    "permissions.read",
                    "error" if missing else "ok",
                    f"{missing} table/sequence resource(s) lack SELECT permission.",
                    "Grant SELECT on source tables and sequences to the dump role."
                    if missing
                    else "",
                )
        report.add(
            "connection.database", "ok", "Configured database accepts connections."
        )
    except (psycopg.Error, OSError):
        report.add(
            "connection.database",
            "error",
            "Cannot connect to the configured database.",
            "Check database existence, CONNECT permission, credentials, SSL, and URL "
            "session options.",
        )
    _check_state(report, config, git_dir, str(params["dbname"]), databases)
    return report


def _check_hooks(report: Report, git_dir: Path) -> None:
    try:
        hook = hook_path(git_dir)
        managed = hook.is_file() and HOOK_IDENTIFIER in hook.read_text()
        executable = os.name == "nt" or os.access(hook, os.X_OK)
        if not managed or not executable:
            report.add(
                "hooks.installed",
                "error",
                "Git's active post-checkout hook is missing or not executable.",
                "Run db-git hook install; if core.hooksPath is set, ensure it points "
                "to the managed hook.",
            )
        else:
            report.add(
                "hooks.installed",
                "ok",
                "Git's active post-checkout hook contains db-git and is executable.",
            )
        if (git_dir / "db-git" / "disabled").exists() or os.environ.get(
            "DB_GIT_SKIP"
        ) == "1":
            report.add(
                "hooks.enabled",
                "warning",
                "Automatic database switching is disabled.",
                "Inspect db-git recover and the disabled marker; resolve recovery "
                "before db-git enable. "
                "Unset DB_GIT_SKIP if set.",
            )
        else:
            report.add("hooks.enabled", "ok", "Automatic switching is enabled.")
    except (DbGitError, OSError, UnicodeError):
        report.add(
            "hooks.installed",
            "error",
            "Cannot read Git's active hook.",
            "Check hook file permissions.",
        )


def _check_storage(report: Report, config: DbGitConfig, git_dir: Path | None) -> None:
    paths = [config.snapshot_dir] if config.mode == "shared" else []
    if git_dir:
        paths.append(common_git_directory(git_dir) / "db-git")
        if git_dir != common_git_directory(git_dir):
            paths.append(git_dir / "db-git")
    for index, path in enumerate(paths):
        ancestor = path
        while not ancestor.exists() and ancestor.parent != ancestor:
            ancestor = ancestor.parent
        writable = ancestor.is_dir() and os.access(ancestor, os.W_OK | os.X_OK)
        report.add(
            f"storage.{index}",
            "ok" if writable else "error",
            "Storage directory (or existing parent) is writable."
            if writable
            else "Storage directory is not writable.",
            ""
            if writable
            else "Check permissions on snapshot_dir and Git's db-git directory.",
        )
    roots = [config.snapshot_dir / ".operations"]
    try:
        if git_dir:
            roots.append(operations_directory(git_dir))
        records = [op for root in roots for op in operations(root)]
        pending = [op for op in records if op.phase not in {"complete", "rolled_back"}]
        report.add(
            "recovery",
            "error" if pending else "ok",
            f"{len(pending)} interrupted operation(s); {len(records)} retained "
            "recovery record(s).",
            "Run db-git recover and finish or roll back interrupted operations."
            if pending
            else "",
        )
    except (DbGitError, OSError, UnicodeError):
        report.add(
            "recovery",
            "error",
            "Recovery journal is unreadable or malformed.",
            "Preserve the journal files for repair; do not delete them to bypass "
            "recovery.",
        )


def _check_clients(
    report: Report, config: DbGitConfig, params: dict[str, str | int]
) -> dict[str, int]:
    versions: dict[str, int] = {}
    if config.strategy != "pgdump":
        report.add(
            "clients", "skipped", "Template strategy does not need pg_dump/pg_restore."
        )
        return versions
    try:
        client_dsn(params)
    except DbGitError as e:
        report.add(
            "clients.options",
            "error",
            str(e),
            "Keep service credentials in the service file and reference it with "
            "?service=NAME.",
        )
    for tool in ("pg_dump", "pg_restore"):
        executable = shutil.which(tool)
        try:
            if not executable:
                raise OSError("missing tool")
            result = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            match = re.search(r"PostgreSQL\) (\d+)(?:\.(\d+))?", result.stdout)
            if not match:
                raise ValueError("unknown version")
            versions[tool] = int(match.group(1))
            report.add(
                f"clients.{tool}", "ok", f"{tool} {versions[tool]} found in PATH."
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            report.add(
                f"clients.{tool}",
                "error",
                f"{tool} is missing or its version cannot be determined.",
                "Install matching PostgreSQL client tools and place them in PATH.",
            )
    return versions


def _check_versions(report: Report, versions: dict[str, int], server: int) -> None:
    if not versions:
        return
    dump, restore = versions.get("pg_dump"), versions.get("pg_restore")
    if dump is not None and dump < server:
        report.add(
            "clients.compatibility",
            "error",
            "pg_dump is older than the server.",
            "Install client tools matching the PostgreSQL server major version.",
        )
    elif dump is not None and restore is not None and restore < dump:
        report.add(
            "clients.compatibility",
            "error",
            "pg_restore is older than pg_dump and may reject its archives.",
            "Use pg_dump and pg_restore from the same PostgreSQL installation.",
        )
    elif dump != server or restore != server:
        report.add(
            "clients.compatibility",
            "warning",
            "Client tools differ from the server major version.",
            "Prefer matching versions; output from newer pg_dump is not guaranteed to "
            "restore to older servers.",
        )
    else:
        report.add(
            "clients.compatibility",
            "ok",
            "Client tools match the server major version.",
        )


class _StateProblem(ValueError):
    """A credential-free explanation of an inconsistent local resource."""


def _check_state(
    report: Report,
    config: DbGitConfig,
    git_dir: Path | None,
    base: str,
    databases: dict[str, tuple[bool, bool, bool]] | None,
) -> None:
    try:
        expected: list[str] = []
        if git_dir and state_path(git_dir).exists():
            state = load_state(git_dir)
            if state.mode != config.mode:
                report.add(
                    "state.mode",
                    "error",
                    "Recorded state mode differs from configuration.",
                    "Reconcile the configuration and recorded state before switching "
                    "databases.",
                )
            for branch, entry in state.databases.items():
                if not isinstance(entry.db_name, str) or not entry.db_name:
                    raise _StateProblem("A branch database entry has an invalid name.")
                if (
                    branch == config.default_branch
                    or entry.db_name == base
                    or entry.db_name in expected
                ):
                    raise _StateProblem(
                        "Branch database ownership is duplicated or points to the seed."
                    )
                expected.append(entry.db_name)
        seen: set[str] = set()
        for path in sorted(config.snapshot_dir.glob("*.meta.json")):
            meta = SnapshotMetadata(**json.loads(path.read_text()))
            if not isinstance(meta.branch, str) or meta.branch in seen:
                raise _StateProblem(
                    "Snapshot metadata has an invalid or duplicated branch."
                )
            seen.add(meta.branch)
            if (
                meta.database != base
                or meta.engine != "postgresql"
                or meta.strategy != config.strategy
            ):
                raise _StateProblem(
                    "Snapshot database, engine, or strategy differs from configuration."
                )
            if path != metadata_path(config.snapshot_dir, meta.branch):
                raise _StateProblem(
                    "Snapshot metadata filename does not match its recorded branch."
                )
            if meta.strategy == "pgdump":
                dump = snapshot_dump_path(config.snapshot_dir, meta.branch)
                if not dump.is_file() or dump.stat().st_size == 0:
                    raise _StateProblem("A recorded snapshot dump is missing or empty.")
            elif meta.strategy == "template":
                expected.append(
                    snapshot_db_name(
                        meta.branch, base, snapshot_dir=config.snapshot_dir
                    )
                )
            else:
                raise _StateProblem("Snapshot metadata uses an unknown strategy.")
        if databases is not None and any(name not in databases for name in expected):
            raise _StateProblem(
                "A recorded branch database or template snapshot "
                "is missing on the server."
            )
        if databases is not None and any(not databases[name][0] for name in expected):
            raise _StateProblem(
                "The connected role no longer owns a recorded database."
            )
        report.add("state", "ok", "Local state and snapshot metadata are consistent.")
        if databases is None:
            report.add(
                "state.databases",
                "skipped",
                "Database existence checks require a maintenance connection.",
            )
        else:
            report.add(
                "state.databases", "ok", "All recorded database resources exist."
            )
    except _StateProblem as e:
        report.add(
            "state",
            "error",
            str(e),
            "Inspect db-git list, db-git recover, and local metadata; "
            "preserve the files and reconcile storage/ownership before proceeding.",
        )
    except (DbGitError, OSError, ValueError, TypeError, KeyError, AttributeError):
        report.add(
            "state",
            "error",
            "State/snapshot metadata is malformed, conflicts with configuration, or "
            "refers to missing resources.",
            "Inspect db-git list, db-git recover, state.json, and snapshot metadata. "
            "Preserve these files and reconcile ownership/storage before further "
            "operations.",
        )
