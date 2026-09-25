"""Identify local resources whose original initialization must remain recoverable."""

from __future__ import annotations

from pathlib import Path

from db_git.repository import operations_directory, shared_state_directory
from db_git.state import load_state


def has_resources(root: Path, git_dir: Path, existing: dict[str, object]) -> bool:
    """Check both configured and default storage before changing engine or mode."""
    common = shared_state_directory(git_dir)
    snapshots = Path(str(existing.get("snapshot_dir") or common / "snapshots"))
    if not snapshots.is_absolute():
        snapshots = root / snapshots
    return bool(
        load_state(git_dir).databases
        or any(operations_directory(git_dir).glob("*.json"))
        or any(
            (location / "mysql-active.json").exists()
            or any(location.glob("*.meta.json"))
            or any(location.glob("*.dump"))
            or any((location / ".operations").glob("*.json"))
            for location in {snapshots, common / "snapshots"}
        )
    )


def same_mysql_seed(previous: str, current: str) -> bool:
    """Credential/TLS changes do not reassign resources to another server or seed."""
    if previous == current:
        return True
    if not previous.startswith("mysql:") or not current.startswith("mysql:"):
        return False
    from db_git.backends.mysql.urls import parse_url

    before, after = parse_url(previous), parse_url(current)
    return all(before[key] == after[key] for key in ("host", "port", "dbname"))
