from __future__ import annotations

import json

import pytest
from tests.mysql.test_extended import active, shared
from tests.mysql.test_mysql import command, rows

from db_git.backends.mysql.connections import identifier
from db_git.errors import DbGitError
from db_git.history import create_checkpoint, list_checkpoints
from db_git.recovery import operations
from db_git.storage import snapshot_dump_path


def history(project):
    return json.loads(command(project, "history", "--json").stdout)["checkpoints"]


def test_cli_checkpoint_history_restore_and_latest_snapshot_are_independent(project):
    config = shared(project)
    command(project, "checkpoint", "baseline")
    saved = history(project)[0]
    assert saved["checkpoint_name"] == "baseline"
    assert command(project, "checkpoint", "baseline", check=False).returncode == 1
    seed = project[1]["dbname"]
    project[4].execute(f"INSERT INTO {identifier(seed)}.users(name) VALUES('after')")
    command(project, "save", "main")
    command(project, "checkpoint")
    command(project, "restore", "--checkpoint", "baseline")
    generation = active(project)
    assert generation != seed
    assert len(rows(project, generation)) == 1
    assert len(rows(project, seed)) == 2
    command(project, "restore", "main")
    assert len(rows(project, active(project))) == 2
    assert len(list_checkpoints(config.snapshot_dir)) == 2
    assert "Checkpoints:" in command(project, "status").stderr
    assert json.loads(command(project, "doctor", "--json", "--strict").stdout)["ok"]


def test_cli_retention_keeps_named_and_latest_and_can_rollback(project):
    config = shared(project)
    command(project, "checkpoint", "baseline")
    baseline = history(project)[0]["checkpoint_id"]
    command(project, "checkpoint")
    older = history(project)[0]["checkpoint_id"]
    command(project, "checkpoint")
    newest = history(project)[0]["checkpoint_id"]
    preview = command(project, "history", "--prune", "--keep", "1", "--dry-run")
    assert older in preview.stderr
    assert len(history(project)) == 3
    command(project, "history", "--prune", "--keep", "1", "--yes")
    assert {m["checkpoint_id"] for m in history(project)} == {baseline, newest}
    record = next(
        r
        for r in operations(config.snapshot_dir / ".operations")
        if r.action == "prune"
    )
    command(project, "recover", record.id, "--rollback", "--yes")
    assert len(history(project)) == 3
    command(project, "restore", "--checkpoint", older)
    assert rows(project, active(project))


def test_pruned_name_stays_reserved_until_recovery_discarded(project):
    config = shared(project)
    command(project, "checkpoint", "baseline")
    command(project, "checkpoint")
    command(project, "history", "--prune", "--keep", "1", "--include-named", "--yes")
    result = command(project, "checkpoint", "baseline", check=False)
    assert result.returncode == 1
    assert "retained in recovery" in result.stderr
    for record in operations(config.snapshot_dir / ".operations"):
        command(project, "recover", record.id, "--discard", "--yes")
    command(project, "checkpoint", "baseline")
    assert len(history(project)) == 2


def test_corrupt_newest_checkpoint_blocks_retention_and_restore(project):
    config = shared(project)
    command(project, "checkpoint")
    command(project, "checkpoint")
    newest = history(project)[0]["checkpoint_id"]
    original = active(project)
    snapshot_dump_path(config.snapshot_dir, "main", newest).write_bytes(b"corrupt")
    assert (
        command(project, "restore", "--checkpoint", newest, check=False).returncode == 1
    )
    assert (
        command(
            project, "history", "--prune", "--keep", "1", "--yes", check=False
        ).returncode
        == 1
    )
    assert active(project) == original
    assert len(history(project)) == 2
    assert command(project, "doctor", "--json", check=False).returncode == 1


def test_interrupted_checkpoint_is_recovered_through_cli(project, monkeypatch):
    import db_git.recovery as recovery

    config = shared(project)
    with monkeypatch.context() as patch:

        def interrupted(*args):
            raise KeyboardInterrupt

        patch.setattr(recovery, "apply_metadata", interrupted)
        with pytest.raises(KeyboardInterrupt):
            create_checkpoint(config, "main", "interrupted")
    record = operations(config.snapshot_dir / ".operations")[0]
    assert record.phase == "ready"
    with pytest.raises(DbGitError, match="Interrupted operation"):
        create_checkpoint(config, "main")
    command(project, "recover", record.id, "--finish", "--yes")
    assert history(project)[0]["checkpoint_name"] == "interrupted"
    command(project, "restore", "--checkpoint", "interrupted")
    assert rows(project, active(project))
