from __future__ import annotations

import json
from dataclasses import asdict, replace

import pytest
from typer.testing import CliRunner

from db_git.cli import app
from db_git.config import write_config
from db_git.errors import SnapshotError
from db_git.history import (
    list_checkpoints,
    resolve_checkpoint,
    retention_plan,
    validate_name,
)
from db_git.storage import (
    Checkpoint,
    has_snapshot,
    identify_stale_snapshots,
    list_snapshots,
    make_metadata,
    metadata_path,
    snapshot_db_name,
    snapshot_dump_path,
    write_metadata,
)


def entry(index, *, branch="main", name=None):
    return replace(
        make_metadata(
            branch,
            "seed",
            "pgdump",
            "postgresql",
            "17",
            checkpoint=Checkpoint(f"{index:032x}", name),
            resource_identity="hash",
        ),
        created_at=f"2026-09-{index:02d}T12:00:00+00:00",
    )


def test_checkpoints_do_not_replace_latest_or_join_ordinary_pruning(tmp_path):
    latest = make_metadata("main", "seed", "pgdump", "postgresql", "17")
    write_metadata(tmp_path, latest)
    for index in (1, 2):
        write_metadata(tmp_path, entry(index))
    assert list_snapshots(tmp_path) == [latest]
    assert has_snapshot(tmp_path, "main")
    assert identify_stale_snapshots(tmp_path, 1, []) == [latest]
    assert [m.checkpoint_id for m in list_checkpoints(tmp_path)] == [
        f"{i:032x}" for i in (2, 1)
    ]


def test_history_alone_is_not_a_branch_snapshot(tmp_path):
    write_metadata(tmp_path, entry(1))
    assert not has_snapshot(tmp_path, "main")
    assert list_snapshots(tmp_path) == []


def test_retention_preserves_named_and_newest_of_each_branch():
    entries = [
        entry(1, name="baseline"),
        entry(2),
        entry(3),
        entry(4),
        entry(5, branch="feature"),
    ]
    assert retention_plan(entries, 1) == [entries[2], entries[1]]
    assert retention_plan(entries, 1, True) == [entries[2], entries[1], entries[0]]
    with pytest.raises(SnapshotError, match="at least one"):
        retention_plan(entries, 0)


def test_names_and_ids_resolve_only_within_selected_branch(tmp_path):
    for item in [
        entry(1, name="baseline"),
        entry(2, branch="feature", name="baseline"),
    ]:
        write_metadata(tmp_path, item)
    assert resolve_checkpoint(tmp_path, "baseline", "main").checkpoint_id == f"{1:032x}"
    assert resolve_checkpoint(tmp_path, f"{2:032x}", "feature").branch == "feature"
    with pytest.raises(SnapshotError, match="not found"):
        resolve_checkpoint(tmp_path, f"{2:032x}", "main")
    with pytest.raises(SnapshotError, match="ambiguous"):
        resolve_checkpoint(tmp_path, "00000000", "missing")


@pytest.mark.parametrize("name", ["", "../outside", "white space", "x" * 81, "\n"])
def test_invalid_names(name):
    with pytest.raises(SnapshotError, match="Checkpoint names"):
        validate_name(name)


@pytest.mark.parametrize("identifier", ["../escape", "a" * 31, "A" * 32, ""])
def test_invalid_ids_cannot_escape_storage(tmp_path, identifier):
    with pytest.raises(SnapshotError):
        metadata_path(tmp_path, "main", identifier)
    with pytest.raises(SnapshotError):
        snapshot_db_name("main", "seed", checkpoint_id=identifier)


@pytest.mark.parametrize("damage", ["json", "id", "identity", "name", "timestamp"])
def test_corrupt_history_is_not_silently_ignored(tmp_path, damage):
    item = entry(1)
    write_metadata(tmp_path, item)
    path = metadata_path(tmp_path, "main", item.checkpoint_id)
    data = asdict(item)
    if damage == "json":
        path.write_text("{")
    else:
        field = {
            "id": "checkpoint_id",
            "identity": "resource_identity",
            "name": "checkpoint_name",
            "timestamp": "created_at",
        }[damage]
        data[field] = "bad" if damage != "identity" else None
        if damage == "name":
            data[field] = "../bad"
        path.write_text(json.dumps(data))
    with pytest.raises(SnapshotError):
        list_checkpoints(tmp_path)


def test_binary_metadata_has_a_clear_error(tmp_path):
    (tmp_path / f"checkpoint_{'a' * 32}.meta.json").write_bytes(b"\xff")
    with pytest.raises(SnapshotError, match="Invalid snapshot metadata"):
        list_checkpoints(tmp_path)


def test_legacy_checkpoint_prefix_with_invalid_branch_is_refused(tmp_path):
    data = asdict(entry(1))
    data.update(checkpoint_id=None, branch=123)
    (tmp_path / "checkpoint_bad.meta.json").write_text(json.dumps(data))
    with pytest.raises(SnapshotError, match="no identity"):
        list_checkpoints(tmp_path)


def test_checkpoint_storage_names_are_distinct_and_short(tmp_path):
    names = {
        snapshot_db_name("feature/long" * 20, "seed", checkpoint_id=f"{i:032x}")
        for i in (1, 2)
    }
    assert len(names) == 2
    assert all(len(name.encode()) <= 63 for name in names)
    assert snapshot_dump_path(tmp_path, "main", f"{1:032x}") != snapshot_dump_path(
        tmp_path, "main"
    )


def test_history_json_and_invalid_retention_options(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    write_config(
        git_repo,
        {"database_url": "postgresql:///seed", "mode": "shared", "strategy": "pgdump"},
    )
    snapshots = git_repo / ".git/db-git/snapshots"
    write_metadata(snapshots, entry(1, name="baseline"))
    result = CliRunner().invoke(app, ["history", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["checkpoints"][0]["checkpoint_name"] == "baseline"
    assert CliRunner().invoke(app, ["history", "--keep", "1"]).exit_code == 1
    assert CliRunner().invoke(app, ["history", "--prune", "--keep", "0"]).exit_code == 2
    result = CliRunner().invoke(app, ["history", "--prune", "--keep", "1", "--dry-run"])
    assert result.exit_code == 0
    assert len(list_checkpoints(snapshots)) == 1


def test_per_branch_mode_reports_unsupported_checkpoint_commands(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    write_config(
        git_repo,
        {
            "database_url": "postgresql:///seed",
            "mode": "per-branch",
            "strategy": "pgdump",
        },
    )
    for args in (["checkpoint"], ["history"], ["restore", "--checkpoint", "baseline"]):
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 1
        assert "shared mode" in result.output
