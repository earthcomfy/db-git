from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from tests._pg_helpers import run_db_git, run_git

from db_git.backends import get_backend
from db_git.backends.sqlite.branch_db import SQLiteBranchDbManager, branch_name
from db_git.backends.sqlite.files import backup
from db_git.backends.sqlite.urls import database_path, database_url
from db_git.config import load_config
from db_git.errors import ConfigError, DatabaseError
from db_git.recovery import operations
from db_git.repository import git_directory, operations_directory
from db_git.state import get_branch_db, load_state


def names(path):
    with closing(sqlite3.connect(path)) as conn:
        return [row[0] for row in conn.execute("SELECT name FROM users ORDER BY name")]


def insert(path, name):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("INSERT INTO users VALUES (?)", (name,))
        conn.commit()


@pytest.fixture
def project(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    for key in os.environ:
        if key.startswith("DB_GIT_") or key == "DATABASE_URL":
            monkeypatch.delenv(key)
    seed = git_repo / "seed space #é.db"
    with closing(sqlite3.connect(seed)) as conn:
        conn.execute("CREATE TABLE users(name TEXT)")
        conn.execute("INSERT INTO users VALUES ('seed')")
        conn.commit()
    env = dict(os.environ)
    run_db_git("init", "--database-url", database_url(seed), cwd=git_repo, env=env)
    return git_repo, seed, env


def command(project, *args, check=True, cwd=None):
    root, _, env = project
    return run_db_git(*args, cwd=cwd or root, env=env, check=check)


def branch_path(project, branch):
    return database_path(command(project, "url", branch).stdout.strip())


def test_sqlite_init_and_diagnostics_are_engine_specific(project):
    root, seed, _ = project
    config = load_config()
    assert config.mode == "per-branch"
    assert config.strategy == "backup"
    assert config.on_active_connections == "fail"
    assert database_path(config.database_url) == seed
    backend = get_backend(config.database_url)
    assert backend.capabilities.modes == frozenset({"per-branch"})
    assert not backend.capabilities.can_terminate_connections
    before = seed.read_bytes()
    result = command(project, "doctor", "--json", "--strict")
    report = json.loads(result.stdout)
    assert report["ok"]
    assert not any(c["id"].startswith("permissions.createdb") for c in report["checks"])
    assert seed.read_bytes() == before
    assert (
        "SQLite" in command(project, "status").stderr
        or "sqlite" in command(project, "status").stderr
    )
    assert not (root / ".git/db-git/state.json").exists()


@pytest.mark.parametrize(
    "url",
    [
        "sqlite://host/a",
        "sqlite:///:memory:",
        "sqlite:///",
        "sqlite:///file:db",
        "sqlite:///a?mode=memory",
        "sqlite:///a#fragment",
        "sqlite:///%00.db",
    ],
)
def test_reject_nonpersistent_or_ambiguous_urls(url):
    with pytest.raises(ConfigError):
        database_path(url)


def test_relative_url_resolves_from_primary_checkout(project):
    root, seed, env = project
    (root / ".db-git.toml").write_text(
        'database_url = "sqlite:///seed%20space%20%23%C3%A9.db"\n'
    )
    nested = root / "nested"
    nested.mkdir()
    assert database_path(command(project, "url", cwd=nested).stdout.strip()) == seed
    linked = root / "linked"
    run_git("worktree", "add", "-b", "feature", str(linked), cwd=root, env=env)
    result = command(project, "url", cwd=linked)
    path = database_path(result.stdout.strip())
    assert path.parent == root / ".git/db-git/sqlite/branches"
    assert names(path) == ["seed"]
    assert load_state(git_directory(root)).databases["feature"].db_name == str(path)


def test_create_from_and_application_runner(project):
    root, seed, env = project
    run_git("checkout", "-b", "feature", cwd=root, env=env)
    feature = branch_path(project, "feature")
    insert(feature, "feature")
    command(project, "create", "copy", "--from", "feature")
    copy = branch_path(project, "copy")
    assert names(copy) == ["feature", "seed"]
    assert names(seed) == ["seed"]
    result = command(
        project,
        "run",
        "--",
        sys.executable,
        "-c",
        "import os; from db_git.backends.sqlite.urls import database_path; "
        "print(database_path(os.environ['DATABASE_URL']))",
    )
    assert result.stdout.strip() == str(feature)
    missing = command(project, "url", "absent", check=False)
    assert missing.returncode == 1
    assert "No database recorded" in missing.stderr
    assert "copy" in command(project, "list").stderr


def test_online_backup_includes_committed_wal_and_excludes_uncommitted_data(project):
    _, seed, _ = project
    with closing(sqlite3.connect(seed)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("INSERT INTO users VALUES ('wal')")
        conn.commit()
        conn.execute("INSERT INTO users VALUES ('uncommitted')")
        assert Path(str(seed) + "-wal").exists()
        command(project, "create", "wal-copy")
        copied = branch_path(project, "wal-copy")
        assert names(copied) == ["seed", "wal"]
        assert not Path(str(copied) + "-wal").exists()
        conn.rollback()
    assert names(seed) == ["seed", "wal"]


def test_reset_and_rollback_keep_live_connections_and_files(project):
    root, seed, _ = project
    command(project, "create", "feature")
    original = branch_path(project, "feature")
    insert(original, "original")
    with closing(sqlite3.connect(original)) as live:
        live.execute("PRAGMA journal_mode=WAL")
        command(project, "reset", "feature")
        replacement = branch_path(project, "feature")
        assert replacement != original
        assert names(replacement) == ["seed"]
        live.execute("INSERT INTO users VALUES ('still-live')")
        live.commit()
        record = next(
            r
            for r in operations(operations_directory(git_directory(root)))
            if r.action == "reset"
        )
        command(project, "recover", record.id, "--rollback", "--yes")
        assert branch_path(project, "feature") == original
        assert names(original) == ["original", "seed", "still-live"]
        command(project, "recover", record.id, "--discard", "--yes")
        assert replacement.exists() and original.exists()
    assert names(seed) == ["seed"]


@pytest.mark.parametrize("cut", ["ready", "published"])
@pytest.mark.parametrize("action", ["finish", "rollback"])
def test_process_crash_recovers_generation_and_ownership(project, cut, action):
    root, seed, env = project
    command(project, "create", "feature")
    previous = branch_path(project, "feature")
    insert(previous, "old")
    code = """
import os, sys
import db_git.recovery as recovery
from db_git.backends.sqlite.operations import SQLiteResources
from db_git.backends.sqlite.branch_db import SQLiteBranchDbManager
from db_git.backends.sqlite.urls import database_path
from db_git.config import load_config
from db_git.repository import git_directory
from db_git.state import get_branch_db
if sys.argv[1] == "ready":
    recovery.finish = lambda *args: os._exit(97)
else:
    publish = SQLiteResources.publish
    def crash(self, record):
        publish(self, record)
        os._exit(97)
    SQLiteResources.publish = crash
config = load_config()
directory = git_directory()
manager = SQLiteBranchDbManager(config)
manager.reset(
    get_branch_db(directory, "feature").db_name,
    str(database_path(config.database_url)), "feature", "main", directory,
)
"""
    result = subprocess.run([sys.executable, "-c", code, cut], cwd=root, env=env)
    assert result.returncode == 97
    record = next(
        r
        for r in operations(operations_directory(git_directory(root)))
        if r.phase == "ready"
    )
    assert (
        command(
            project, "run", "--", sys.executable, "-c", "pass", check=False
        ).returncode
        == 1
    )
    command(project, "recover", record.id, f"--{action}", "--yes")
    selected = branch_path(project, "feature")
    assert names(selected) == (["seed"] if action == "finish" else ["old", "seed"])
    assert previous.exists()
    assert names(seed) == ["seed"]


def test_busy_backup_has_bounded_wait_and_does_not_publish(project):
    root, seed, _ = project
    (root / ".db-git.toml").write_text(
        (root / ".db-git.toml").read_text() + "\nbackup_timeout_ms = 150\n"
    )
    with closing(sqlite3.connect(seed)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        result = command(project, "create", "locked", check=False)
        assert result.returncode == 1
    assert "locked" not in load_state(git_directory(root)).databases
    assert names(seed) == ["seed"]


def test_backup_destination_is_never_overwritten(project):
    root, seed, _ = project
    destination = root / "keep.db"
    destination.write_bytes(b"existing content")
    with pytest.raises(FileExistsError):
        backup(seed, destination, 100)
    assert destination.read_bytes() == b"existing content"


def test_failed_clone_preserves_ownership_and_seed(project, monkeypatch):
    root, seed, _ = project
    manager = SQLiteBranchDbManager(load_config())
    git_dir = git_directory(root)
    target = branch_name("feature", str(seed), "main", git_dir)

    def fail(source, destination, timeout):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"partial")
        raise DatabaseError("disk full")

    monkeypatch.setattr("db_git.backends.sqlite.branch_db.backup", fail)
    with pytest.raises(DatabaseError, match="disk full"):
        manager.create(target, str(seed), "feature", "main", git_dir)
    assert "feature" not in load_state(git_dir).databases
    assert operations(operations_directory(git_dir))[0].phase == "rolled_back"
    assert names(seed) == ["seed"]


def test_prune_and_recovery_keep_file_with_open_connection(project):
    root, _, _ = project
    command(project, "create", "stale")
    path = branch_path(project, "stale")
    with closing(sqlite3.connect(path)) as live:
        preview = command(project, "prune", "--dry-run")
        assert "Would untrack (file retained)" in preview.stderr
        assert get_branch_db(git_directory(root), "stale") is not None
        command(project, "prune", "--yes")
        assert get_branch_db(git_directory(root), "stale") is None
        assert path.exists()
        assert live.execute("SELECT name FROM users").fetchone() == ("seed",)
        record = next(
            r
            for r in operations(operations_directory(git_directory(root)))
            if r.action == "drop"
        )
        command(project, "recover", record.id, "--rollback", "--yes")
        assert branch_path(project, "stale") == path


@pytest.mark.parametrize(
    "extra",
    [
        ("--mode", "shared"),
        ("--strategy", "template"),
        ("--on-active-connections", "terminate"),
    ],
)
def test_unsupported_capabilities_do_not_change_config(project, extra):
    root, seed, _ = project
    before = (root / ".db-git.toml").read_bytes()
    result = command(
        project, "init", "--database-url", database_url(seed), *extra, check=False
    )
    assert result.returncode == 1
    assert (root / ".db-git.toml").read_bytes() == before


def test_missing_seed_is_not_created(git_repo):
    result = run_db_git(
        "init",
        "--database-url",
        "sqlite:///missing.db",
        cwd=git_repo,
        env=dict(os.environ),
        check=False,
    )
    assert result.returncode == 1
    assert not (git_repo / "missing.db").exists()
    assert not (git_repo / ".db-git.toml").exists()


def test_recovery_refuses_newer_ownership_metadata(project):
    root, _, _ = project
    command(project, "create", "first")
    record = operations(operations_directory(git_directory(root)))[0]
    command(project, "create", "second")
    result = command(project, "recover", record.id, "--rollback", "--yes", check=False)
    assert result.returncode == 1
    assert "Metadata changed" in result.stderr
    assert set(load_state(git_directory(root)).databases) == {"first", "second"}


def test_direct_backup_times_out_while_writer_holds_exclusive_lock(project):
    import time

    root, seed, _ = project
    destination = root / "locked-copy.sqlite3"
    start = time.monotonic()
    with closing(sqlite3.connect(seed)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(DatabaseError, match="timed out"):
            backup(seed, destination, 100)
    assert time.monotonic() - start < 3
    assert names(seed) == ["seed"]


def test_reinitialize_cannot_reassign_existing_files_to_another_seed(project):
    root, _, _ = project
    command(project, "create", "feature")
    other = root / "other.db"
    with closing(sqlite3.connect(other)) as conn:
        conn.execute("CREATE TABLE other(x)")
    before = (root / ".db-git.toml").read_bytes()
    result = command(
        project, "init", "--database-url", database_url(other), check=False
    )
    assert result.returncode == 1
    assert "another seed" in result.stderr
    assert (root / ".db-git.toml").read_bytes() == before


def test_rollback_refuses_missing_previous_generation(project):
    root, _, _ = project
    command(project, "create", "feature")
    previous = branch_path(project, "feature")
    command(project, "reset", "feature")
    replacement = branch_path(project, "feature")
    previous.unlink()
    record = next(
        r
        for r in operations(operations_directory(git_directory(root)))
        if r.action == "reset"
    )
    result = command(project, "recover", record.id, "--rollback", "--yes", check=False)
    assert result.returncode == 1
    assert "does not exist" in result.stderr
    assert branch_path(project, "feature") == replacement


def test_invalid_backup_timeout_does_not_write_configuration(project):
    root, _, _ = project
    original = (root / ".db-git.toml").read_text()
    (root / ".db-git.toml").write_text(original + "\nbackup_timeout_ms = 0\n")
    before = (root / ".db-git.toml").read_bytes()
    result = command(project, "init", check=False)
    assert result.returncode == 1
    assert "positive" in result.stderr
    assert (root / ".db-git.toml").read_bytes() == before


def test_tracked_sqlite_seed_is_rejected(project):
    root, seed, env = project
    run_git("add", str(seed), cwd=root, env=env)
    result = command(project, "init", check=False)
    assert result.returncode == 1
    assert "tracked by Git" in result.stderr


def test_managed_file_symlinks_are_rejected(project):
    _, seed, _ = project
    command(project, "create", "feature")
    path = branch_path(project, "feature")
    path.unlink()
    path.symlink_to(seed)
    result = command(project, "url", "feature", check=False)
    assert result.returncode == 1
    assert "managed branch directory" in result.stderr
    result = command(project, "doctor", "--json", check=False)
    assert result.returncode == 1
    assert names(seed) == ["seed"]


@pytest.mark.parametrize(
    "resource", ["previous.meta.json", ".operations/previous.json"]
)
def test_init_preserves_configuration_for_custom_postgres_snapshots(project, resource):
    root, seed, _ = project
    config = root / ".db-git.toml"
    config.write_text(
        'database_url="postgresql:///old"\nstrategy="template"\nsnapshot_dir="custom"\n'
    )
    snapshot = root / "custom" / resource
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("preserve this record")
    before = config.read_bytes()
    result = command(project, "init", "--database-url", database_url(seed), check=False)
    assert result.returncode == 1
    assert config.read_bytes() == before
    assert snapshot.read_text() == "preserve this record"


def test_init_does_not_reinterpret_sqlite_files_as_postgres_databases(project):
    root, _, _ = project
    command(project, "create", "feature")
    before = (root / ".db-git.toml").read_bytes()
    result = command(
        project, "init", "--database-url", "postgresql:///not_used", check=False
    )
    assert result.returncode == 1
    assert "Existing SQLite ownership" in result.stderr
    assert (root / ".db-git.toml").read_bytes() == before
