from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from db_git.errors import SnapshotError
from db_git.files import atomic_write, local_lock
from db_git.repository import operations_directory, shared_state_directory


@dataclass
class BranchDbEntry:
    """
    Record of a per-branch database created by db-git.
    """

    db_name: str
    created_at: str
    created_from: str


@dataclass
class DbGitState:
    """
    Branch ownership shared in the common Git directory under db-git/state.json.
    """

    mode: str = "per-branch"
    databases: dict[str, BranchDbEntry] = field(default_factory=dict)


_STATE_FILE = "state.json"


def state_path(git_dir: Path) -> Path:
    return shared_state_directory(git_dir) / _STATE_FILE


def load_state(git_dir: Path) -> DbGitState:
    """
    Read repository-wide branch ownership from the common Git directory.
    """
    path = state_path(git_dir)
    if not path.exists():
        return DbGitState()
    try:
        data = json.loads(path.read_text())
        databases: dict[str, BranchDbEntry] = {}
        for branch, entry in data.get("databases", {}).items():
            databases[branch] = BranchDbEntry(**entry)
        return DbGitState(
            mode=data.get("mode", "per-branch"),
            databases=databases,
        )
    except (json.JSONDecodeError, TypeError, KeyError, AttributeError) as e:
        raise SnapshotError(f"Invalid state at {path}; preserve it for repair") from e


def save_state(git_dir: Path, state: DbGitState) -> None:
    """
    Write repository-wide branch ownership under the common operation lock.
    """
    with local_lock(operations_directory(git_dir)):
        atomic_write(state_path(git_dir), state_text(state))


def state_text(state: DbGitState) -> str:
    data = {
        "mode": state.mode,
        "databases": {
            branch: asdict(entry) for branch, entry in state.databases.items()
        },
    }
    return json.dumps(data, indent=2) + "\n"


def record_branch_db(
    git_dir: Path,
    branch: str,
    db_name: str,
    created_from: str,
) -> None:
    """
    Add or update a branch database entry in the state file.
    """
    with local_lock(operations_directory(git_dir)):
        state = load_state(git_dir)
        state.databases[branch] = BranchDbEntry(
            db_name=db_name,
            created_at=datetime.now(UTC).isoformat(),
            created_from=created_from,
        )
        save_state(git_dir, state)


def remove_branch_db(git_dir: Path, branch: str) -> None:
    """
    Remove a branch database entry from the state file.
    """
    with local_lock(operations_directory(git_dir)):
        state = load_state(git_dir)
        state.databases.pop(branch, None)
        save_state(git_dir, state)


def get_branch_db(git_dir: Path, branch: str) -> BranchDbEntry | None:
    """
    Look up a branch database entry. Returns None if not found.
    """
    state = load_state(git_dir)
    entry = state.databases.get(branch)
    if entry is not None:
        owners = [b for b, e in state.databases.items() if e.db_name == entry.db_name]
        if len(owners) > 1:
            raise SnapshotError(
                f"Database '{entry.db_name}' is recorded for multiple branches: "
                f"{', '.join(owners)}. Resolve the conflicting state before proceeding."
            )
    return entry
