from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

from db_git.backends.sqlite.branch_db import branch_directory, validate_branch_path
from db_git.backends.sqlite.files import connect
from db_git.backends.sqlite.urls import database_path
from db_git.errors import DbGitError
from db_git.state import get_branch_db, load_state

if TYPE_CHECKING:
    from db_git.config import DbGitConfig
    from db_git.doctor import Report


def check(report: Report, config: DbGitConfig, git_dir: Path | None) -> None:
    report.add(
        "engine", "ok", f"SQLite {sqlite3.sqlite_version}; online backup strategy."
    )
    report.add(
        "capabilities",
        "ok",
        "Per-branch files with generation-based reset/recovery. "
        "Shared mode, connection termination, and automatic file deletion "
        "are unsupported.",
    )
    seed = database_path(config.database_url)
    _check_file(report, "database.seed", seed)
    if git_dir is None:
        return
    try:
        state = load_state(git_dir)
        if state.mode != config.mode:
            raise DbGitError("Recorded mode differs from SQLite configuration.")
        for index, (branch, entry) in enumerate(state.databases.items()):
            get_branch_db(git_dir, branch)  # Detect duplicate ownership.
            path = validate_branch_path(entry.db_name, git_dir)
            if branch == config.default_branch or path.resolve() == seed:
                raise DbGitError("SQLite branch ownership conflicts with the seed.")
            _check_file(report, f"database.branch.{index}", path)
        report.add(
            "state", "ok", f"{len(state.databases)} SQLite branch file(s) recorded."
        )
        active = {entry.db_name for entry in state.databases.values()}
        retained = sum(
            str(path) not in active
            for path in branch_directory(git_dir).glob("*.sqlite3")
        )
        report.add(
            "storage.retained",
            "ok",
            f"{retained} unselected SQLite file(s) retained. "
            "Close applications before manual cleanup; "
            "preserve files referenced by recovery records.",
        )
    except (DbGitError, OSError, ValueError):
        report.add(
            "state",
            "error",
            "SQLite ownership is malformed, duplicated, "
            "or outside the managed directory.",
            "Preserve state and database files; reconcile ownership before continuing.",
        )


def _check_file(report: Report, identifier: str, path: Path) -> None:
    try:
        with closing(connect(path)) as conn:
            if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise DbGitError("integrity check failed")
        report.add(
            identifier, "ok", f"SQLite database readable; quick_check passed: {path}"
        )
    except (DbGitError, sqlite3.Error, OSError):
        report.add(
            identifier,
            "error",
            f"SQLite database missing, locked, unreadable, or corrupt: {path}",
            "Check the path and permissions, stop writers, "
            "and preserve the file for recovery.",
        )
