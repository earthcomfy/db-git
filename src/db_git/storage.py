from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from db_git import __version__
from db_git.errors import SnapshotError
from db_git.files import atomic_write
from db_git.state import get_branch_db, load_state

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
    db_git_version: str
    file_size_bytes: int | None


def sanitize_branch_name(branch: str, max_length: int = _DEFAULT_MAX_IDENTIFIER) -> str:
    """
    Sanitize a git branch name for use as a DB identifier or filename.
    """
    sanitized = branch.replace("/", "__")
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", sanitized)
    sanitized = sanitized.strip("_") or "unnamed"
    return sanitized[:max_length]


def _hashed_name(prefix: str, identity: str, max_length: int) -> str:
    """Keep a readable prefix and a digest of the complete, unsanitized identity."""
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    suffix = f"__h{digest}"
    if max_length < len(suffix):
        raise ValueError("Identifier limit is too small for a collision-resistant name")
    # PostgreSQL limits identifier bytes, not Unicode characters.
    prefix = prefix.encode("utf-8")[: max_length - len(suffix)].decode(
        "utf-8", errors="ignore"
    )
    return prefix + suffix


def branch_db_name(
    branch: str,
    dbname: str,
    default_branch: str,
    max_length: int = _DEFAULT_MAX_IDENTIFIER,
    *,
    git_dir: Path | None = None,
    engine: str = "postgresql",
) -> str:
    """Resolve a recorded database, or generate a distinct name for a new branch."""
    if engine == "sqlite":
        from db_git.backends.sqlite.branch_db import branch_name

        return branch_name(branch, dbname, default_branch, git_dir)
    if engine == "mysql":
        from db_git.backends.mysql.branch_db import branch_name

        return branch_name(branch, dbname, default_branch, git_dir)
    if branch == default_branch:
        return dbname
    if git_dir is not None:
        entry = get_branch_db(git_dir, branch)
        if entry is not None:
            if entry.db_name == dbname:
                raise SnapshotError("Branch database points to the seed database")
            return entry.db_name
    name = _hashed_name(
        f"{dbname}__{sanitize_branch_name(branch)}",
        json.dumps([dbname, branch]),
        max_length,
    )
    if git_dir is not None:
        for owner, entry in load_state(git_dir).databases.items():
            if entry.db_name == name:
                raise SnapshotError(
                    f"Database name '{name}' is already recorded for '{owner}'"
                )
    return name


def _legacy_snapshot_db_name(branch: str, dbname: str, max_length: int) -> str:
    """The pre-hash naming algorithm, used only for metadata-owned snapshots."""
    name = f"_dbgit_{dbname}_{sanitize_branch_name(branch, max_length)}"
    if len(name) <= max_length:
        return name
    branch_hash = hashlib.sha256(branch.encode("utf-8")).hexdigest()[:8]
    suffix = f"_h{branch_hash}"
    prefix = f"_dbgit_{dbname}"
    max_prefix_len = max_length - len(suffix)
    if max_prefix_len <= 0:
        return f"_dbgit_h{branch_hash}"[:max_length]
    return prefix[:max_prefix_len] + suffix


def snapshot_db_name(
    branch: str,
    dbname: str,
    max_length: int = _DEFAULT_MAX_IDENTIFIER,
    *,
    snapshot_dir: Path | None = None,
) -> str:
    """Retain legacy names only when their metadata identifies the exact branch."""
    if snapshot_dir is not None:
        path = metadata_path(snapshot_dir, branch)
        legacy = snapshot_dir / f"{sanitize_branch_name(branch)}.meta.json"
        if path == legacy and path.exists():
            return _legacy_snapshot_db_name(branch, dbname, max_length)
    return _hashed_name(
        f"_dbgit_{dbname}_{sanitize_branch_name(branch)}",
        json.dumps([dbname, branch]),
        max_length,
    )


def snapshot_dump_path(snapshot_dir: Path, branch: str) -> Path:
    """Resolve the dump alongside the branch's owned metadata."""
    meta = metadata_path(snapshot_dir, branch)
    return meta.with_name(meta.name.removesuffix(".meta.json") + ".dump")


def _read_metadata_file(path: Path) -> SnapshotMetadata | None:
    if not path.exists():
        return None
    try:
        return SnapshotMetadata(**json.loads(path.read_text()))
    except (json.JSONDecodeError, TypeError, KeyError) as e:
        raise SnapshotError(
            f"Invalid snapshot metadata at {path}; repair it first"
        ) from e


def metadata_path(snapshot_dir: Path, branch: str) -> Path:
    """Prefer new names; reuse a legacy path only when its branch matches exactly."""
    stem = _hashed_name(sanitize_branch_name(branch), branch, _DEFAULT_MAX_IDENTIFIER)
    path = snapshot_dir / f"{stem}.meta.json"
    meta = _read_metadata_file(path)
    if meta is not None:
        if meta.branch != branch:
            raise SnapshotError(f"Snapshot name collision at {path}")
        return path
    legacy = snapshot_dir / f"{sanitize_branch_name(branch)}.meta.json"
    meta = _read_metadata_file(legacy)
    if meta is not None and meta.branch == branch:
        return legacy
    return path


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
    atomic_write(path, json.dumps(asdict(metadata), indent=2) + "\n")


def read_metadata(snapshot_dir: Path, branch: str) -> SnapshotMetadata | None:
    """
    Read snapshot metadata from a JSON sidecar file.
    """
    return _read_metadata_file(metadata_path(snapshot_dir, branch))


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
    return read_metadata(snapshot_dir, branch) is not None


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
        db_git_version=__version__,
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
