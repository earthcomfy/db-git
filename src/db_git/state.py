from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path


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
    Persistent state tracked in .git/db-git/state.json.
    """

    mode: str = "per-branch"
    databases: dict[str, BranchDbEntry] = field(default_factory=dict)


_STATE_DIR = "db-git"
_STATE_FILE = "state.json"


def _state_path(git_dir: Path) -> Path:
    return git_dir / _STATE_DIR / _STATE_FILE


def load_state(git_dir: Path) -> DbGitState:
    """
    Read state from .git/db-git/state.json.
    """
    path = _state_path(git_dir)
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
    except (json.JSONDecodeError, TypeError, KeyError):
        return DbGitState()


def save_state(git_dir: Path, state: DbGitState) -> None:
    """
    Write state to .git/db-git/state.json.
    """
    path = _state_path(git_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "mode": state.mode,
        "databases": {
            branch: asdict(entry) for branch, entry in state.databases.items()
        },
    }
    path.write_text(json.dumps(data, indent=2) + "\n")


def record_branch_db(
    git_dir: Path,
    branch: str,
    db_name: str,
    created_from: str,
) -> None:
    """
    Add or update a branch database entry in the state file.
    """
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
    state = load_state(git_dir)
    state.databases.pop(branch, None)
    save_state(git_dir, state)


def get_branch_db(git_dir: Path, branch: str) -> BranchDbEntry | None:
    """
    Look up a branch database entry. Returns None if not found.
    """
    state = load_state(git_dir)
    return state.databases.get(branch)
