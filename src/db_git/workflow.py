"""Resolve owned branch resources for application and cloning workflows."""

from __future__ import annotations

from pathlib import Path

from db_git.backends import DatabaseBackend
from db_git.config import DbGitConfig
from db_git.db import parse_database_url, with_connection_defaults, with_database_name
from db_git.errors import DbGitError
from db_git.recovery import require_recovered
from db_git.repository import operations_directory, require_safe_shared_mode
from db_git.state import get_branch_db
from db_git.storage import branch_db_name


def owned_database(
    branch: str, config: DbGitConfig, backend: DatabaseBackend, git_dir: Path
) -> str:
    """Resolve seed or recorded ownership, without guessing an untracked database."""
    params = backend.apply_url_defaults(parse_database_url(config.database_url))
    seed = str(params["dbname"])
    if branch != config.default_branch and get_branch_db(git_dir, branch) is None:
        raise DbGitError(
            f"No database recorded for branch '{branch}'. "
            "Use db-git create to create its database first."
        )
    name = branch_db_name(
        branch,
        seed,
        config.default_branch,
        backend.max_identifier_length,
        git_dir=git_dir,
        engine=backend.engine,
    )
    if not isinstance(name, str) or not name:
        raise DbGitError("Invalid database name in local state. Run db-git doctor.")
    return name


def application_url(
    config: DbGitConfig, backend: DatabaseBackend, git_dir: Path, branch: str | None
) -> str:
    """Resolve an existing application database, refusing incomplete operations."""
    require_safe_shared_mode(config.mode)
    require_recovered(operations_directory(git_dir))
    require_recovered(config.snapshot_dir / ".operations")
    params = backend.apply_url_defaults(parse_database_url(config.database_url))
    base_url = with_connection_defaults(config.database_url, params)
    if config.mode == "shared":
        if (git_dir / "db-git" / "disabled").exists():
            raise DbGitError(
                "Shared database switching is disabled. Verify/restore the database "
                "and run db-git enable before launching the application."
            )
        database = str(params["dbname"])
        url = base_url
    else:
        if branch is None:
            raise DbGitError("HEAD is detached. Check out a branch before db-git run.")
        database = owned_database(branch, config, backend, git_dir)
        url = with_database_name(base_url, database)
    if not backend.database_exists(config.database_url, database):
        raise DbGitError(
            "Application database is missing. Inspect db-git doctor and "
            "create or recover the branch database before launching the application."
        )
    return url
