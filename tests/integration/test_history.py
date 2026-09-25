from __future__ import annotations

from dataclasses import replace

import psycopg
import pytest

from db_git.backends import get_backend
from db_git.backends.postgresql.operations import PostgresResources
from db_git.db import parse_database_url
from db_git.errors import DbGitError
from db_git.history import (
    create_checkpoint,
    history_scope,
    list_checkpoints,
    prune_checkpoints,
    restore_checkpoint,
    retention_plan,
)
from db_git.recovery import discard, finish, operations, rollback
from db_git.resources import FileResources
from db_git.storage import (
    has_snapshot,
    metadata_path,
    read_metadata,
    snapshot_dump_path,
)
from tests._pg_helpers import get_names, seed_users

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("strategy", ["template", "pgdump"])
def test_checkpoint_restore_survives_snapshot_overwrites_and_strategy_change(
    make_config, git_repo, monkeypatch, strategy
):
    monkeypatch.chdir(git_repo)
    config = make_config(strategy=strategy)
    seed_users(config.database_url)
    before = create_checkpoint(config, "main", "before-migration")
    assert before.resource_identity
    assert not has_snapshot(config.snapshot_dir, "main")
    with psycopg.connect(config.database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO users(name) VALUES('after')")
    latest = get_backend(config.database_url).detect_strategy(config)
    latest.save(config.database_url, "main", config.snapshot_dir, config)
    create_checkpoint(config, "main")
    with pytest.raises(DbGitError, match="already exists"):
        create_checkpoint(config, "main", "before-migration")
    changed = replace(
        config, strategy="pgdump" if strategy == "template" else "template"
    )
    restore_checkpoint(changed, "main", "before-migration")
    assert get_names(config.database_url) == ["Alice", "Bob", "Charlie"]
    assert len(list_checkpoints(config.snapshot_dir)) == 2
    assert read_metadata(config.snapshot_dir, "main").checkpoint_id is None
    latest.restore(config.database_url, "main", config.snapshot_dir, config)
    assert set(get_names(config.database_url)) == {"Alice", "Bob", "Charlie", "after"}


@pytest.mark.parametrize("strategy", ["template", "pgdump"])
def test_retention_is_recoverable_and_retains_named_and_last_checkpoint(
    make_config, git_repo, monkeypatch, strategy
):
    monkeypatch.chdir(git_repo)
    config = make_config(strategy=strategy)
    seed_users(config.database_url)
    baseline = create_checkpoint(config, "main", "baseline")
    older = create_checkpoint(config, "main")
    newest = create_checkpoint(config, "main")
    entries = list_checkpoints(config.snapshot_dir)
    assert retention_plan(entries, 1) == [older]
    prune_checkpoints(config, {older.checkpoint_id}, keep=1)
    assert {m.checkpoint_id for m in list_checkpoints(config.snapshot_dir)} == {
        baseline.checkpoint_id,
        newest.checkpoint_id,
    }
    root = config.snapshot_dir / ".operations"
    record = next(r for r in operations(root) if r.action == "prune")
    backend = get_backend(config.database_url)
    with history_scope(config, backend) as conn:
        resources = (
            FileResources()
            if record.kind == "file"
            else PostgresResources(conn, config)
        )
        rollback(root, resources, record)
        discard(root, resources, record)
    assert len(list_checkpoints(config.snapshot_dir)) == 3
    restore_checkpoint(config, "main", older.checkpoint_id)
    assert get_names(config.database_url) == ["Alice", "Bob", "Charlie"]
    with pytest.raises(DbGitError, match="changed since"):
        prune_checkpoints(config, {newest.checkpoint_id}, keep=1, include_named=True)


@pytest.mark.parametrize("strategy", ["template", "pgdump"])
def test_interrupted_checkpoint_publication_finishes_from_journal(
    make_config, git_repo, monkeypatch, strategy
):
    import db_git.recovery as recovery

    monkeypatch.chdir(git_repo)
    config = make_config(strategy=strategy)
    seed_users(config.database_url)
    with monkeypatch.context() as patch:

        def interrupted(*args):
            raise KeyboardInterrupt

        patch.setattr(recovery, "apply_metadata", interrupted)
        with pytest.raises(KeyboardInterrupt):
            create_checkpoint(config, "main", "recover-me")
    root = config.snapshot_dir / ".operations"
    record = operations(root)[0]
    assert record.phase == "ready"
    with pytest.raises(DbGitError, match="Interrupted operation"):
        create_checkpoint(config, "main", "blocked")
    backend = get_backend(config.database_url)
    params = backend.apply_url_defaults(parse_database_url(config.database_url))
    conn = backend.connect_maintenance(params)
    try:
        resources = (
            FileResources()
            if record.kind == "file"
            else PostgresResources(conn, config)
        )
        finish(root, resources, record)
    finally:
        conn.close()
    assert list_checkpoints(config.snapshot_dir)[0].checkpoint_name == "recover-me"
    restore_checkpoint(config, "main", "recover-me")


def test_changed_checkpoint_blocks_restore_and_retention(
    make_config, git_repo, monkeypatch
):
    monkeypatch.chdir(git_repo)
    config = make_config(strategy="pgdump")
    seed_users(config.database_url)
    older = create_checkpoint(config, "main")
    newer = create_checkpoint(config, "main")
    snapshot_dump_path(config.snapshot_dir, "main", newer.checkpoint_id).write_bytes(
        b"corrupt"
    )
    with pytest.raises(DbGitError, match="missing or changed"):
        restore_checkpoint(config, "main", newer.checkpoint_id)
    with pytest.raises(DbGitError, match="missing or changed"):
        prune_checkpoints(config, {older.checkpoint_id}, keep=1)
    assert metadata_path(config.snapshot_dir, "main", older.checkpoint_id).is_file()
    assert len(list_checkpoints(config.snapshot_dir)) == 2
    assert get_names(config.database_url) == ["Alice", "Bob", "Charlie"]
