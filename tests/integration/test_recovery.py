import subprocess
import sys

import pytest

from db_git.backends.postgresql.operations import PostgresResources, operation_scope
from db_git.db import parse_database_url
from db_git.errors import DbGitError, SnapshotError
from db_git.recovery import discard, finish, operations, require_recovered, rollback
from db_git.state import load_state
from db_git.storage import snapshot_dump_path
from tests._pg_helpers import build_url, get_names, reconnect, seed_users

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("strategy_name", ["template", "pgdump"])
@pytest.mark.parametrize("cut", ["ready", "published"])
@pytest.mark.parametrize("recovery_action", ["finish", "rollback"])
def test_database_recovers_after_process_exit(
    backend, make_config, strategy_name, cut, recovery_action
):
    config = make_config(strategy=strategy_name)
    seed_users(config.database_url)
    strategy = backend.detect_strategy(config)
    strategy.save(config.database_url, "main", config.snapshot_dir, config)
    with reconnect(config.database_url) as conn:
        conn.execute("INSERT INTO users (name) VALUES ('Working')")
    code = """
import os, sys
from pathlib import Path
from db_git.config import DbGitConfig
from db_git.backends.postgresql.backend import PostgresqlBackend
import db_git.recovery as recovery
from db_git.backends.postgresql.operations import PostgresResources
url, directory, strategy, cut = sys.argv[1:]
config = DbGitConfig(database_url=url, strategy=strategy, snapshot_dir=Path(directory))
if cut == 'ready':
    PostgresResources.publish = lambda *args: os._exit(77)
else:
    recovery.apply_metadata = lambda *args: os._exit(77)
strategy_impl = PostgresqlBackend().detect_strategy(config)
strategy_impl.restore(url, 'main', config.snapshot_dir, config)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            config.database_url,
            str(config.snapshot_dir),
            strategy_name,
            cut,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 77, result.stderr
    root = config.snapshot_dir / ".operations"
    pending = [r for r in operations(root) if r.phase == "ready"]
    assert len(pending) == 1
    op = pending[0]
    assert config.database_url not in (root / f"{op.id}.json").read_text()
    with pytest.raises(DbGitError, match="Interrupted"):
        strategy.save(config.database_url, "main", config.snapshot_dir, config)
    params = backend.apply_url_defaults(parse_database_url(config.database_url))
    with operation_scope(backend, params, root, recovering=True) as conn:
        resources = PostgresResources(conn, config)
        if recovery_action == "finish":
            finish(root, resources, op)
        else:
            rollback(root, resources, op)
        require_recovered(root)
        discard(root, resources, op)
    names = get_names(config.database_url)
    assert ("Working" in names) == (recovery_action == "rollback")
    assert names[:3] == ["Alice", "Bob", "Charlie"]


def test_bad_dump_does_not_replace_working_database(backend, make_config):
    config = make_config(strategy="pgdump")
    seed_users(config.database_url)
    strategy = backend.detect_strategy(config)
    strategy.save(config.database_url, "main", config.snapshot_dir, config)
    snapshot_dump_path(config.snapshot_dir, "main").write_bytes(b"invalid archive")
    with pytest.raises(SnapshotError, match="pg_restore failed"):
        strategy.restore(config.database_url, "main", config.snapshot_dir, config)
    assert get_names(config.database_url) == ["Alice", "Bob", "Charlie"]
    assert all(
        op.phase in {"complete", "rolled_back"}
        for op in operations(config.snapshot_dir / ".operations")
    )


@pytest.mark.parametrize("strategy_name", ["template", "pgdump"])
def test_failed_reset_keeps_branch_and_ownership(
    backend, make_config, git_dir, pg_info, strategy_name
):
    config = make_config(strategy=strategy_name, mode="per-branch")
    manager = backend.branch_db_manager(config)
    seed_users(config.database_url)
    target = pg_info["dbname"] + "__feature"
    manager.create(target, pg_info["dbname"], "feature", "main", git_dir)
    before = load_state(git_dir)
    with pytest.raises(SnapshotError):
        manager.reset(target, "missing_database", "feature", "main", git_dir)
    assert get_names(build_url(pg_info, target)) == ["Alice", "Bob", "Charlie"]
    assert load_state(git_dir) == before


def test_database_publication_failure_restores_old_snapshot(
    backend, make_config, monkeypatch
):
    import db_git.recovery as recovery

    config = make_config(strategy="template")
    seed_users(config.database_url)
    strategy = backend.detect_strategy(config)
    strategy.save(config.database_url, "main", config.snapshot_dir, config)
    with reconnect(config.database_url) as conn:
        conn.execute("INSERT INTO users (name) VALUES ('Later')")
    original = recovery.apply_metadata
    failed = False

    def fail_once(record, text):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("metadata disk full")
        original(record, text)

    with monkeypatch.context() as patch:
        patch.setattr(recovery, "apply_metadata", fail_once)
        with pytest.raises(OSError, match="disk full"):
            strategy.save(config.database_url, "main", config.snapshot_dir, config)
    strategy.restore(config.database_url, "main", config.snapshot_dir, config)
    assert get_names(config.database_url) == ["Alice", "Bob", "Charlie"]


def test_server_lock_blocks_other_repository(backend, make_config, tmp_path):
    config = make_config(strategy="template")
    params = backend.apply_url_defaults(parse_database_url(config.database_url))
    root = config.snapshot_dir / ".operations"
    code = """
import sys
from pathlib import Path
from db_git.backends.postgresql.backend import PostgresqlBackend
from db_git.backends.postgresql.operations import operation_scope
from db_git.db import parse_database_url
backend = PostgresqlBackend()
params = backend.apply_url_defaults(parse_database_url(sys.argv[1]))
with operation_scope(backend, params, Path(sys.argv[2])): pass
"""
    with operation_scope(backend, params, root):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                config.database_url,
                str(tmp_path / "other-repo"),
            ],
            capture_output=True,
            text=True,
        )
    assert result.returncode != 0
    assert "Another db-git operation" in result.stderr


def test_failed_dump_save_keeps_previous_snapshot(backend, make_config, monkeypatch):
    import db_git.backends.postgresql.pgdump as pgdump

    config = make_config(strategy="pgdump")
    seed_users(config.database_url)
    strategy = backend.detect_strategy(config)
    strategy.save(config.database_url, "main", config.snapshot_dir, config)
    dump = snapshot_dump_path(config.snapshot_dir, "main")
    before = dump.read_bytes()
    run = subprocess.run

    def fail(args, **kwargs):
        from pathlib import Path

        # Inject a dump failure without intercepting Git's worktree safety checks.
        if Path(args[0]).name != "pg_dump":
            return run(args, **kwargs)
        Path(args[args.index("-f") + 1]).write_bytes(b"partial archive")
        return subprocess.CompletedProcess(args, 1, "", "disk full")

    with monkeypatch.context() as patch:
        patch.setattr(pgdump.subprocess, "run", fail)
        with pytest.raises(SnapshotError, match="disk full"):
            strategy.save(config.database_url, "main", config.snapshot_dir, config)
    assert dump.read_bytes() == before
    strategy.restore(config.database_url, "main", config.snapshot_dir, config)
    assert get_names(config.database_url) == ["Alice", "Bob", "Charlie"]


def test_failed_second_database_rename_is_atomic(backend, make_config, monkeypatch):
    import psycopg

    config = make_config(strategy="template")
    seed_users(config.database_url)
    strategy = backend.detect_strategy(config)
    strategy.save(config.database_url, "main", config.snapshot_dir, config)
    with reconnect(config.database_url) as conn:
        conn.execute("INSERT INTO users (name) VALUES ('Later')")
    connect = backend.connect_maintenance

    class Connection:
        def __init__(self, connection):
            self.connection = connection
            self.renames = 0

        def execute(self, query, params=None):
            if "RENAME TO" in str(query):
                self.renames += 1
                if self.renames == 2:
                    raise psycopg.OperationalError("rename failure")
            return self.connection.execute(query, params)

        def close(self):
            self.connection.close()

    with monkeypatch.context() as patch:
        patch.setattr(
            backend, "connect_maintenance", lambda params: Connection(connect(params))
        )
        with pytest.raises(SnapshotError, match="rename failure"):
            strategy.save(config.database_url, "main", config.snapshot_dir, config)
    strategy.restore(config.database_url, "main", config.snapshot_dir, config)
    assert get_names(config.database_url) == ["Alice", "Bob", "Charlie"]
