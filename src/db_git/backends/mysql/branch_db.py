from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from db_git.backends.mysql.connections import Connection
from db_git.backends.mysql.dump import clone
from db_git.backends.mysql.operations import (
    MySQLResources,
    generation,
    operation_scope,
    server_identity,
    validate_generation,
)
from db_git.backends.mysql.urls import parse_url
from db_git.errors import DbGitError
from db_git.recovery import run_operation
from db_git.repository import operations_directory
from db_git.state import (
    BranchDbEntry,
    get_branch_db,
    load_state,
    state_path,
    state_text,
)

if TYPE_CHECKING:
    from db_git.config import DbGitConfig


def branch_name(
    branch: str, seed: str, default_branch: str, git_dir: Path | None
) -> str:
    if branch == default_branch:
        return seed
    if git_dir is None:
        raise DbGitError("MySQL generations require a Git repository.")
    entry = get_branch_db(git_dir, branch)
    if entry is not None:
        validate_generation(entry.db_name, seed)
        return entry.db_name
    return generation(seed, branch)


class MySQLBranchDbManager:
    def __init__(self, config: DbGitConfig) -> None:
        self.config = config
        self.params = {
            k: v for k, v in parse_url(config.database_url).items() if v is not None
        }
        self.seed = str(self.params["dbname"])

    def exists(self, name: str) -> bool:
        conn = Connection(self.params)
        try:
            if name == self.seed:
                return MySQLResources(conn).identity(name) is not None
            validate_generation(name, self.seed)
            return MySQLResources(conn).identity(name) not in {None, "unmanaged"}
        finally:
            conn.close()

    def create(
        self, target: str, source: str, branch: str, created_from: str, git_dir: Path
    ) -> None:
        self._replace(target, source, branch, created_from, git_dir, reset=False)

    def reset(
        self, target: str, source: str, branch: str, created_from: str, git_dir: Path
    ) -> None:
        self._replace(target, source, branch, created_from, git_dir, reset=True)

    def _replace(
        self,
        target: str,
        source: str,
        branch: str,
        created_from: str,
        git_dir: Path,
        *,
        reset: bool,
    ) -> None:
        root = operations_directory(git_dir)
        with operation_scope(self.params, root) as conn:
            validate_generation(target, self.seed)
            if branch == self.config.default_branch:
                raise DbGitError("Cannot replace the MySQL seed database.")
            state = load_state(git_dir)
            entry = get_branch_db(git_dir, branch)
            if reset:
                if entry is None or entry.db_name != target:
                    raise DbGitError(
                        "No owned MySQL database to reset; use create first."
                    )
                if MySQLResources(conn).identity(target) in {None, "unmanaged"}:
                    raise DbGitError("Previous MySQL generation is missing or unowned.")
            elif entry is not None:
                raise DbGitError(
                    "A database is already recorded for this branch; use reset or "
                    "recover."
                )
            if source != self.seed and not any(
                e.db_name == source for e in state.databases.values()
            ):
                raise DbGitError(
                    "Source MySQL database is not recorded in this repository."
                )

            def after(stage: str) -> str:
                state.databases[branch] = BranchDbEntry(
                    stage, datetime.now(UTC).isoformat(), created_from
                )
                return state_text(state)

            run_operation(
                root,
                MySQLResources(conn),
                action="reset" if reset else "create",
                kind="mysql",
                server=server_identity(conn, self.seed),
                target=target,
                names=lambda token: (
                    generation(self.seed, branch, token),
                    generation(self.seed, "retained"),
                ),
                metadata=state_path(git_dir),
                build=lambda stage: clone(conn, self.params, source, stage),
                after=after,
                replace=reset,
            )

    def drop(self, name: str, branch: str, git_dir: Path) -> None:
        root = operations_directory(git_dir)
        with operation_scope(self.params, root) as conn:
            validate_generation(name, self.seed)
            entry = get_branch_db(git_dir, branch)
            if (
                branch == self.config.default_branch
                or entry is None
                or entry.db_name != name
            ):
                raise DbGitError("Refusing to remove unowned MySQL branch state.")
            state = load_state(git_dir)
            del state.databases[branch]
            run_operation(
                root,
                MySQLResources(conn),
                action="drop",
                kind="mysql",
                server=server_identity(conn, self.seed),
                target=name,
                names=lambda token: (
                    generation(self.seed, "unused", token),
                    generation(self.seed, "retained"),
                ),
                metadata=state_path(git_dir),
                build=None,
                after=lambda _: state_text(state),
            )

    def list(self, git_dir: Path) -> list[tuple[str, BranchDbEntry, bool]]:
        return [
            (
                branch,
                entry,
                self.exists(
                    branch_name(branch, self.seed, self.config.default_branch, git_dir)
                ),
            )
            for branch, entry in load_state(git_dir).databases.items()
        ]
