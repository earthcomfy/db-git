from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from db_git.backends.sqlite.files import backup, check_database
from db_git.backends.sqlite.operations import (
    SQLiteResources,
    operation_scope,
    server_identity,
)
from db_git.backends.sqlite.urls import database_path
from db_git.errors import DbGitError
from db_git.recovery import run_operation
from db_git.repository import operations_directory, shared_state_directory
from db_git.state import (
    BranchDbEntry,
    get_branch_db,
    load_state,
    state_path,
    state_text,
)
from db_git.storage import sanitize_branch_name

if TYPE_CHECKING:
    from db_git.config import DbGitConfig


def branch_directory(git_dir: Path) -> Path:
    return shared_state_directory(git_dir) / "sqlite" / "branches"


def validate_branch_path(name: str, git_dir: Path) -> Path:
    path = Path(name)
    directory = branch_directory(git_dir).resolve()
    if (
        not path.is_absolute()
        or path.parent.resolve() != directory
        or path.is_symlink()
    ):
        raise DbGitError(
            "SQLite ownership points outside its managed branch directory."
        )
    return path


def new_path(branch: str, git_dir: Path) -> Path:
    return (
        branch_directory(git_dir)
        / f"{sanitize_branch_name(branch, 40)}-{uuid.uuid4().hex}.sqlite3"
    )


def branch_name(
    branch: str, seed: str, default_branch: str, git_dir: Path | None
) -> str:
    if branch == default_branch:
        return seed
    if git_dir is None:
        raise DbGitError("SQLite branch files require a Git repository.")
    entry = get_branch_db(git_dir, branch)
    if entry is not None:
        path = validate_branch_path(entry.db_name, git_dir)
        if path.resolve() == Path(seed).resolve():
            raise DbGitError("SQLite branch points to the seed database.")
        return str(path)
    return str(new_path(branch, git_dir))


class SQLiteBranchDbManager:
    def __init__(self, config: DbGitConfig) -> None:
        self.config = config
        self.seed = database_path(config.database_url)

    def exists(self, name: str) -> bool:
        return Path(name).is_file()

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
        with operation_scope(root):
            if (
                branch == self.config.default_branch
                or Path(target).resolve() == self.seed
            ):
                raise DbGitError("Cannot replace the SQLite seed database.")
            state = load_state(git_dir)
            entry = get_branch_db(git_dir, branch)
            if reset:
                if entry is None or entry.db_name != target:
                    raise DbGitError(
                        "No owned SQLite branch file to reset; use create first."
                    )
                validate_branch_path(target, git_dir)
                target = str(new_path(branch, git_dir))
            elif entry is not None:
                raise DbGitError(
                    "A database is already recorded for this branch; "
                    "use reset or recover."
                )
            target_path = validate_branch_path(target, git_dir)
            source_path = Path(source)
            if source_path != self.seed:
                validate_branch_path(source, git_dir)
            check_database(source_path)
            state.databases[branch] = BranchDbEntry(
                target, datetime.now(UTC).isoformat(), created_from
            )
            run_operation(
                root,
                SQLiteResources(),
                action="reset" if reset else "create",
                kind="sqlite",
                server=server_identity(str(self.seed)),
                target=target,
                names=lambda identifier: (
                    str(target_path.with_suffix(f".{identifier}.stage.sqlite3")),
                    str(target_path.with_suffix(f".{identifier}.unused")),
                ),
                metadata=state_path(git_dir),
                build=lambda stage: backup(
                    source_path, Path(stage), self.config.backup_timeout_ms
                ),
                after=lambda _: state_text(state),
                replace=False,
            )

    def drop(self, name: str, branch: str, git_dir: Path) -> None:
        root = operations_directory(git_dir)
        with operation_scope(root):
            entry = get_branch_db(git_dir, branch)
            if (
                entry is None
                or entry.db_name != name
                or Path(name).resolve() == self.seed
            ):
                raise DbGitError("Refusing to remove unowned SQLite branch state.")
            path = validate_branch_path(name, git_dir)
            state = load_state(git_dir)
            del state.databases[branch]
            run_operation(
                root,
                SQLiteResources(),
                action="drop",
                kind="sqlite",
                server=server_identity(str(self.seed)),
                target=name,
                names=lambda identifier: (
                    str(path.with_suffix(f".{identifier}.stage")),
                    str(path.with_suffix(f".{identifier}.unused")),
                ),
                metadata=state_path(git_dir),
                build=None,
                after=lambda _: state_text(state),
            )

    def list(self, git_dir: Path) -> list[tuple[str, BranchDbEntry, bool]]:
        state = load_state(git_dir)
        result = []
        for branch, entry in state.databases.items():
            validate_branch_path(entry.db_name, git_dir)
            result.append((branch, entry, self.exists(entry.db_name)))
        return result
