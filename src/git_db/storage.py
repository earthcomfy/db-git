from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from git_db import __version__

_DEFAULT_MAX_IDENTIFIER = 63


@dataclass
class SnapshotMetadata:
    """
    Metadata sidecar for a database snapshot.
    """

    branch: str
    database: str
    strategy: str
    created_at: str
    engine: str
    engine_version: str
    git_db_version: str
    file_size_bytes: int | None


def sanitize_branch_name(branch: str, max_length: int = _DEFAULT_MAX_IDENTIFIER) -> str:
    """
    Sanitize a git branch name for use as a DB identifier or filename.
    """
    sanitized = branch.replace("/", "__")
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", sanitized)
    sanitized = sanitized.strip("_") or "unnamed"
    return sanitized[:max_length]


def branch_db_name(
    branch: str,
    dbname: str,
    default_branch: str,
    max_length: int = _DEFAULT_MAX_IDENTIFIER,
) -> str:
    """
    Build the per-branch database name.
    """
    if branch == default_branch:
        return dbname

    sanitized = sanitize_branch_name(branch, max_length)
    name = f"{dbname}__{sanitized}"
    if len(name) <= max_length:
        return name

    branch_hash = hashlib.sha256(branch.encode("utf-8")).hexdigest()[:8]
    suffix = f"__h{branch_hash}"
    max_prefix_len = max_length - len(suffix)
    if max_prefix_len <= 0:
        return f"h{branch_hash}"[:max_length]
    return dbname[:max_prefix_len] + suffix


def snapshot_db_name(
    branch: str,
    dbname: str,
    max_length: int = _DEFAULT_MAX_IDENTIFIER,
) -> str:
    """
    Build the snapshot database name: _gitdb_{dbname}_{sanitized_branch}.
    """
    sanitized = sanitize_branch_name(branch, max_length)
    name = f"_gitdb_{dbname}_{sanitized}"
    if len(name) <= max_length:
        return name

    branch_hash = hashlib.sha256(branch.encode("utf-8")).hexdigest()[:8]
    suffix = f"_h{branch_hash}"
    prefix = f"_gitdb_{dbname}"
    max_prefix_len = max_length - len(suffix)
    if max_prefix_len <= 0:
        return f"_gitdb_h{branch_hash}"[:max_length]
    return prefix[:max_prefix_len] + suffix


def snapshot_dump_path(snapshot_dir: Path, branch: str) -> Path:
    """
    Return the path to the dump file for a branch.
    """
    return snapshot_dir / f"{sanitize_branch_name(branch)}.dump"


def metadata_path(snapshot_dir: Path, branch: str) -> Path:
    """
    Return the path to the metadata JSON file for a branch.
    """
    return snapshot_dir / f"{sanitize_branch_name(branch)}.meta.json"


def ensure_snapshot_dir(snapshot_dir: Path) -> None:
    """
    Create the snapshot directory if it doesn't exist.
    """
    snapshot_dir.mkdir(parents=True, exist_ok=True)


def write_metadata(snapshot_dir: Path, metadata: SnapshotMetadata) -> None:
    """
    Write snapshot metadata to a JSON sidecar file.
    """
    ensure_snapshot_dir(snapshot_dir)
    path = metadata_path(snapshot_dir, metadata.branch)
    path.write_text(json.dumps(asdict(metadata), indent=2) + "\n")


def read_metadata(snapshot_dir: Path, branch: str) -> SnapshotMetadata | None:
    """
    Read snapshot metadata from a JSON sidecar file.
    """
    path = metadata_path(snapshot_dir, branch)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return SnapshotMetadata(**data)
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def list_snapshots(snapshot_dir: Path) -> list[SnapshotMetadata]:
    """
    Read all snapshot metadata files in the snapshot directory.
    """
    if not snapshot_dir.exists():
        return []
    snapshots = []
    for path in sorted(snapshot_dir.glob("*.meta.json")):
        try:
            data = json.loads(path.read_text())
            snapshots.append(SnapshotMetadata(**data))
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
    return snapshots


def has_snapshot(snapshot_dir: Path, branch: str) -> bool:
    """
    Check whether a snapshot exists for the given branch.
    """
    return metadata_path(snapshot_dir, branch).exists()


def make_metadata(
    branch: str,
    database: str,
    strategy: str,
    engine: str,
    engine_version: str,
    file_size_bytes: int | None = None,
) -> SnapshotMetadata:
    """
    Create a SnapshotMetadata with current timestamp and version.
    """
    return SnapshotMetadata(
        branch=branch,
        database=database,
        strategy=strategy,
        created_at=datetime.now(UTC).isoformat(),
        engine=engine,
        engine_version=engine_version,
        git_db_version=__version__,
        file_size_bytes=file_size_bytes,
    )


def identify_stale_snapshots(
    snapshot_dir: Path,
    max_snapshots: int,
    existing_branches: list[str],
) -> list[SnapshotMetadata]:
    """
    Identify snapshots that should be pruned.
    """
    snapshots = list_snapshots(snapshot_dir)
    if not snapshots:
        return []

    result: list[SnapshotMetadata] = []

    # Phase 1: snapshots for branches that no longer exist
    stale = [s for s in snapshots if s.branch not in existing_branches]
    result.extend(stale)

    # Phase 2: enforce max count on remaining snapshots
    stale_branches = {s.branch for s in stale}
    remaining = [s for s in snapshots if s.branch not in stale_branches]
    if len(remaining) > max_snapshots:
        remaining.sort(key=lambda s: s.created_at)
        result.extend(remaining[: len(remaining) - max_snapshots])

    return result
