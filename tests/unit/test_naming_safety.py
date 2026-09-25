import json
from dataclasses import asdict

import pytest

from db_git.errors import SnapshotError
from db_git.state import record_branch_db
from db_git.storage import (
    branch_db_name,
    has_snapshot,
    make_metadata,
    metadata_path,
    read_metadata,
    snapshot_db_name,
    snapshot_dump_path,
    write_metadata,
)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("feature/foo-bar", "feature/foo_bar"),
        ("feature/auth", "feature__auth"),
        ("a" * 80 + "x", "a" * 80 + "y"),
        ("é", "ø"),
    ],
)
def test_distinct_branches_have_distinct_storage(tmp_path, first, second):
    assert branch_db_name(first, "app", "main") != branch_db_name(second, "app", "main")
    assert snapshot_db_name(first, "app") != snapshot_db_name(second, "app")
    assert snapshot_dump_path(tmp_path, first) != snapshot_dump_path(tmp_path, second)
    assert metadata_path(tmp_path, first) != metadata_path(tmp_path, second)


def test_names_include_full_seed_identity_and_respect_byte_limit():
    seeds = ["é" * 40 + "a", "é" * 40 + "b"]
    names = [branch_db_name("feature", seed, "main") for seed in seeds]
    assert names[0] != names[1]
    assert all(len(name.encode()) <= 63 for name in names)


def test_recorded_legacy_branch_database_is_preserved(tmp_path):
    record_branch_db(tmp_path, "feature/foo-bar", "app__feature__foo_bar", "main")
    assert (
        branch_db_name("feature/foo-bar", "app", "main", git_dir=tmp_path)
        == "app__feature__foo_bar"
    )
    assert (
        branch_db_name("feature/foo_bar", "app", "main", git_dir=tmp_path)
        != "app__feature__foo_bar"
    )


def test_ambiguous_legacy_ownership_is_rejected(tmp_path):
    for branch in ["feature/foo-bar", "feature/foo_bar"]:
        record_branch_db(tmp_path, branch, "app__feature__foo_bar", "main")
    with pytest.raises(SnapshotError, match="multiple branches"):
        branch_db_name("feature/foo-bar", "app", "main", git_dir=tmp_path)


def test_legacy_snapshot_is_only_accessible_to_its_recorded_branch(tmp_path):
    legacy = tmp_path / "feature__foo_bar.meta.json"
    meta = make_metadata("feature/foo-bar", "app", "pgdump", "postgresql", "14")
    legacy.write_text(json.dumps(asdict(meta)))
    old_dump = tmp_path / "feature__foo_bar.dump"
    old_dump.write_bytes(b"original")

    assert metadata_path(tmp_path, meta.branch) == legacy
    assert snapshot_dump_path(tmp_path, meta.branch) == old_dump
    assert has_snapshot(tmp_path, meta.branch)
    assert not has_snapshot(tmp_path, "feature/foo_bar")
    assert (
        snapshot_db_name(meta.branch, "app", snapshot_dir=tmp_path)
        == "_dbgit_app_feature__foo_bar"
    )

    other = make_metadata("feature/foo_bar", "app", "pgdump", "postgresql", "14")
    write_metadata(tmp_path, other)
    snapshot_dump_path(tmp_path, other.branch).write_bytes(b"other")
    write_metadata(tmp_path, meta)
    assert read_metadata(tmp_path, meta.branch) == meta
    assert read_metadata(tmp_path, other.branch) == other
    assert old_dump.read_bytes() == b"original"
    assert (
        snapshot_db_name(other.branch, "app", snapshot_dir=tmp_path)
        != "_dbgit_app_feature__foo_bar"
    )


def test_metadata_with_wrong_branch_cannot_authorize_overwrite(tmp_path):
    target = metadata_path(tmp_path, "feature")
    target.write_text(
        json.dumps(asdict(make_metadata("other", "app", "pgdump", "postgresql", "14")))
    )
    with pytest.raises(SnapshotError, match="collision"):
        metadata_path(tmp_path, "feature")


def test_corrupt_legacy_metadata_requires_repair(tmp_path):
    (tmp_path / "feature.meta.json").write_text("broken")
    with pytest.raises(SnapshotError, match="repair"):
        metadata_path(tmp_path, "feature")


@pytest.mark.parametrize("owned", [False, True])
def test_drop_refuses_unowned_or_ambiguous_database(tmp_path, owned):
    from unittest.mock import Mock

    from db_git.backends.postgresql.branch_db import PostgresBranchDbManager
    from db_git.config import DbGitConfig

    backend = Mock()
    backend.apply_url_defaults.return_value = {"dbname": "app"}
    manager = PostgresBranchDbManager(
        backend, DbGitConfig(database_url="postgresql:///app")
    )
    if owned:
        for branch in ["first", "second"]:
            record_branch_db(tmp_path, branch, "app__shared", "main")
    with pytest.raises(SnapshotError):
        manager.drop("app__shared", "first", tmp_path)
    backend.connect_maintenance.assert_not_called()


def test_new_name_cannot_select_database_owned_by_legacy_branch(tmp_path):
    generated = branch_db_name("feature", "app", "main")
    record_branch_db(tmp_path, "legacy", generated, "main")
    with pytest.raises(SnapshotError, match="already recorded"):
        branch_db_name("feature", "app", "main", git_dir=tmp_path)
