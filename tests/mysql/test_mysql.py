from __future__ import annotations

import json
import subprocess
import sys
from contextlib import closing

import pytest
from tests._pg_helpers import run_db_git, run_git

from db_git.backends.mysql.branch_db import MySQLBranchDbManager
from db_git.backends.mysql.connections import Connection, identifier
from db_git.backends.mysql.operations import MARKER, generation
from db_git.config import load_config
from db_git.errors import DatabaseError, DbGitError
from db_git.recovery import operations
from db_git.repository import git_directory, operations_directory
from db_git.state import get_branch_db, load_state


def command(project, *args, check=True, cwd=None):
    root, _, _, env, _ = project
    return run_db_git(*args, cwd=cwd or root, env=env, check=check)


def selected(project, branch="feature"):
    return get_branch_db(git_directory(project[0]), branch).db_name


def rows(project, name):
    return (
        project[4]
        .execute(f"SELECT name, payload FROM {identifier(name)}.users ORDER BY id")
        .fetchall()
    )


def test_init_create_from_reset_and_recovery(project):
    root, params, url, _, conn = project
    command(project, "create", "feature")
    first = selected(project)
    assert rows(project, first) == rows(project, params["dbname"])
    assert (
        command(project, "url", "feature").stdout.strip()
        == url.rsplit("/", 1)[0] + "/" + first
    )
    conn.execute(f"INSERT INTO {identifier(first)}.users (name) VALUES ('branch-only')")
    command(project, "create", "copy", "--from", "feature")
    assert rows(project, selected(project, "copy")) == rows(project, first)
    command(project, "reset", "feature")
    second = selected(project)
    assert first != second
    assert len(rows(project, first)) == 2
    assert rows(project, second) == rows(project, params["dbname"])
    record = next(
        r
        for r in operations(operations_directory(git_directory(root)))
        if r.action == "reset"
    )
    command(project, "recover", record.id, "--rollback", "--yes")
    assert selected(project) == first
    assert rows(project, second)
    command(project, "recover", record.id, "--discard", "--yes")
    assert rows(project, first) and rows(project, second)


def test_hook_worktree_run_doctor_and_prune(project):
    root, params, _, env, _ = project
    run_git("checkout", "-b", "feature", cwd=root, env=env)
    first = selected(project)
    worktree = root.parent / (root.name + "-linked")
    run_git("worktree", "add", "-b", "linked", str(worktree), cwd=root, env=env)
    assert rows(project, selected(project, "linked")) == rows(project, first)
    result = command(
        project,
        "run",
        "--",
        sys.executable,
        "-c",
        "import os; print(os.environ['DATABASE_URL']); "
        "print(os.environ['DB_GIT_DATABASE_URL'])",
        cwd=worktree,
    )
    assert result.stdout.splitlines()[0].endswith("/" + selected(project, "linked"))
    assert result.stdout.splitlines()[1].endswith("/" + params["dbname"])
    assert json.loads(command(project, "doctor", "--json", "--strict").stdout)["ok"]
    assert (
        first in command(project, "list").stderr.replace("\n", "")
        or "feature" in command(project, "list").stderr
    )
    command(project, "create", "orphan")
    orphan = selected(project, "orphan")
    assert "retained" in command(project, "prune", "--dry-run").stderr
    command(project, "prune", "--yes")
    assert "orphan" not in load_state(git_directory(root)).databases
    assert rows(project, orphan)


def test_internal_foreign_keys_and_collation(project):
    _, params, _, _, conn = project
    seed = identifier(params["dbname"])
    conn.execute(
        f"CREATE TABLE {seed}.children (id INT PRIMARY KEY, user_id INT, "
        f"CONSTRAINT users_fk FOREIGN KEY (user_id) REFERENCES {seed}.users(id)) "
        "ENGINE=InnoDB"
    )
    conn.execute(f"INSERT INTO {seed}.children VALUES (1, 1)")
    command(project, "create", "feature")
    target = selected(project)
    assert conn.execute(
        "SELECT REFERENCED_TABLE_SCHEMA FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='children' "
        "AND REFERENCED_TABLE_NAME IS NOT NULL",
        (target,),
    ).fetchone() == (target,)
    assert conn.execute(
        "SELECT DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA "
        "WHERE SCHEMA_NAME=%s",
        (target,),
    ).fetchone() == ("utf8mb4_unicode_ci",)


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE {seed}.bad (id INT) ENGINE=MyISAM",
    ],
)
def test_unsupported_objects_refused_without_touching_seed(project, ddl):
    root, params, _, _, conn = project
    before = rows(project, params["dbname"])
    conn.execute(ddl.format(seed=identifier(params["dbname"])))
    result = command(project, "create", "feature", check=False)
    assert result.returncode == 1
    assert rows(project, params["dbname"]) == before
    assert not load_state(git_directory(root)).databases


def test_failed_restore_and_interrupted_publication_preserve_previous_database(
    project, monkeypatch
):
    import db_git.backends.mysql.dump as dump
    import db_git.recovery as recovery

    root, _, _, _, _ = project
    command(project, "create", "feature")
    before = selected(project)
    manager = MySQLBranchDbManager(load_config())
    git_dir = git_directory(root)
    original_run = subprocess.run

    def failed_restore(args, **kwargs):
        if args[0] == "mysql" and "--binary-mode" in args:
            return subprocess.CompletedProcess(
                args, 1, stderr=b"sensitive server output"
            )
        return original_run(args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(dump.subprocess, "run", failed_restore)
        with pytest.raises(DatabaseError, match="restore failed"):
            manager.reset(before, manager.seed, "feature", "main", git_dir)
    assert selected(project) == before
    assert rows(project, before)
    assert operations(operations_directory(git_dir))[0].phase == "rolled_back"

    def interrupted(*args):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(recovery, "apply_metadata", interrupted)
        with pytest.raises(KeyboardInterrupt):
            manager.reset(before, manager.seed, "feature", "main", git_dir)
    record = operations(operations_directory(git_dir))[0]
    assert record.phase == "ready"
    assert command(project, "create", "blocked", check=False).returncode == 1
    command(project, "recover", record.id, "--finish", "--yes")
    assert selected(project) == record.stage
    assert selected(project) != before
    assert rows(project, before)


def test_recovery_refuses_missing_old_generation_or_later_metadata(project):
    root, _, _, _, conn = project
    command(project, "create", "feature")
    first = selected(project)
    command(project, "reset", "feature")
    second = selected(project)
    record = operations(operations_directory(git_directory(root)))[0]
    conn.execute(f"DROP DATABASE {identifier(first)}")
    result = command(project, "recover", record.id, "--rollback", "--yes", check=False)
    assert result.returncode == 1
    assert selected(project) == second
    # A failed rollback blocks other operations until manually repaired.
    assert "rolling_back" in command(project, "recover").stderr


def test_unowned_database_collision_is_preserved(project):
    root, params, _, _, conn = project
    manager = MySQLBranchDbManager(load_config())
    target = generation(params["dbname"], "feature")
    conn.execute(f"CREATE DATABASE {identifier(target)}")
    with pytest.raises(DbGitError, match="already exists"):
        manager.create(target, params["dbname"], "feature", "main", git_directory(root))
    assert conn.execute(
        "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s",
        (target,),
    ).fetchone()


def test_existing_connections_keep_old_generation_after_reset(project):
    _, params, _, _, _ = project
    command(project, "create", "feature")
    before = selected(project)
    with closing(Connection(params)) as application:
        application.execute(f"USE {identifier(before)}")
        command(project, "reset", "feature")
        assert application.execute("SELECT DATABASE()").fetchone() == (before,)
        application.execute("INSERT INTO users(name) VALUES ('still-old')")
    assert len(rows(project, before)) == 2
    assert len(rows(project, selected(project))) == 1


def test_marker_change_prevents_recovery(project):
    root, _, _, _, conn = project
    command(project, "create", "feature")
    record = operations(operations_directory(git_directory(root)))[0]
    conn.execute(
        f"ALTER TABLE {identifier(record.stage)}.{identifier(MARKER)} COMMENT='changed'"
    )
    result = command(project, "recover", record.id, "--finish", "--yes", check=False)
    assert result.returncode == 1
    assert "changed" in result.stderr


def test_readonly_user_rejected_without_secret_output(project):
    _, params, _, _, conn = project
    user = "limited_" + params["dbname"]
    password = "secret-should-never-print"
    conn.execute("CREATE USER %s@%s IDENTIFIED BY %s", (user, "%", password))
    conn.execute(f"GRANT SELECT ON {identifier(params['dbname'])}.* TO '{user}'@'%'")
    url = f"mysql://{user}:{password}@{params['host']}:{params['port']}/{params['dbname']}"
    env = {**project[3], "DB_GIT_DATABASE_URL": url}
    result = run_db_git("doctor", "--json", cwd=project[0], env=env, check=False)
    assert result.returncode == 1
    assert password not in result.stdout + result.stderr
    assert "missing:" in result.stdout
    conn.execute(f"DROP USER '{user}'@'%'")


@pytest.mark.parametrize(
    "args",
    [
        ("--strategy", "template"),
        ("--on-active-connections", "terminate"),
    ],
)
def test_reinit_rejects_unsupported_options_without_rewriting(project, args):
    before = (project[0] / ".db-git.toml").read_bytes()
    assert command(project, "init", *args, check=False).returncode == 1
    assert (project[0] / ".db-git.toml").read_bytes() == before


def test_later_metadata_blocks_stale_rollback(project):
    root = project[0]
    command(project, "create", "first")
    record = operations(operations_directory(git_directory(root)))[0]
    command(project, "create", "second")
    result = command(project, "recover", record.id, "--rollback", "--yes", check=False)
    assert result.returncode == 1
    assert "Metadata changed" in result.stderr
    assert set(load_state(git_directory(root)).databases) == {"first", "second"}


def test_cross_database_foreign_key_rejected(project):
    root, params, _, _, conn = project
    command(project, "create", "other")
    other = selected(project, "other")
    conn.execute(
        f"CREATE TABLE {identifier(params['dbname'])}.external_ref "
        f"(user_id INT, FOREIGN KEY (user_id) REFERENCES {identifier(other)}.users(id))"
    )
    result = command(project, "create", "feature", check=False)
    assert result.returncode == 1
    assert "cross-database" in result.stderr
    assert "feature" not in load_state(git_directory(root)).databases
    conn.execute(f"DROP TABLE {identifier(params['dbname'])}.external_ref")


def test_backup_lock_contention_fails_without_publication(project):
    root, params, _, _, _ = project
    with closing(Connection(params)) as writer:
        writer.execute("LOCK INSTANCE FOR BACKUP")
        # Backup locks can coexist during the dump, but this other session's lock
        # prevents creation of the destination's persistent InnoDB tables.
        result = command(project, "create", "feature", check=False)
        assert result.returncode == 1
    assert not load_state(git_directory(root)).databases
    assert rows(project, params["dbname"])


def test_seed_lock_serializes_other_processes(project):
    import hashlib

    root, params, _, _, conn = project
    lock = "dbgit:" + hashlib.sha256(params["dbname"].encode()).hexdigest()[:56]
    assert conn.execute("SELECT GET_LOCK(%s, 0)", (lock,)).fetchone() == (1,)
    try:
        result = command(project, "create", "feature", check=False)
        assert result.returncode == 1
        assert "holds the MySQL seed lock" in result.stderr
        assert not load_state(git_directory(root)).databases
    finally:
        conn.execute("SELECT RELEASE_LOCK(%s)", (lock,))


def test_reinitialize_cannot_reassign_owned_generations(project):
    root, params, _, _, _ = project
    command(project, "create", "feature")
    before = (root / ".db-git.toml").read_bytes()
    result = command(
        project, "init", "--database-url", "postgresql:///unused", check=False
    )
    assert result.returncode == 1
    assert "Existing MySQL ownership" in result.stderr
    result = command(
        project,
        "init",
        "--database-url",
        f"mysql://dev@localhost/{params['dbname']}_other",
        check=False,
    )
    assert result.returncode == 1
    assert "another seed" in result.stderr
    assert (root / ".db-git.toml").read_bytes() == before


def test_interrupted_build_can_only_roll_back(project, monkeypatch):
    import db_git.backends.mysql.dump as dump

    root, params, _, _, _ = project
    command(project, "create", "feature")
    before = selected(project)
    original_run = subprocess.run

    def interrupted_restore(args, **kwargs):
        if args[0] == "mysql" and "--binary-mode" in args:
            raise KeyboardInterrupt
        return original_run(args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(dump.subprocess, "run", interrupted_restore)
        with pytest.raises(KeyboardInterrupt):
            MySQLBranchDbManager(load_config()).reset(
                before, params["dbname"], "feature", "main", git_directory(root)
            )
    record = operations(operations_directory(git_directory(root)))[0]
    assert record.phase == "building"
    assert (
        command(
            project, "recover", record.id, "--finish", "--yes", check=False
        ).returncode
        == 1
    )
    command(project, "recover", record.id, "--rollback", "--yes")
    assert selected(project) == before
    assert rows(project, before)


def test_recovery_refuses_different_server_identity(project):
    root = project[0]
    command(project, "create", "feature")
    recovery_root = operations_directory(git_directory(root))
    record = operations(recovery_root)[0]
    record.server = "different-server"
    record.write(recovery_root)
    result = command(project, "recover", record.id, "--rollback", "--yes", check=False)
    assert result.returncode == 1
    assert "another MySQL server" in result.stderr
    assert selected(project) == record.stage


def test_client_password_escaping_with_live_server(project):
    from urllib.parse import quote

    from db_git.backends.mysql.connections import client_options, subprocess_env

    _, params, _, _, conn = project
    user = "escape_" + params["dbname"]
    password = 'quote"hash#slash\\line\nbreak'
    conn.execute("CREATE USER %s@%s IDENTIFIED BY %s", (user, "%", password))
    try:
        options_params = {**params, "user": user, "password": password}
        with client_options(options_params) as options:
            result = subprocess.run(
                ["mysql", *options, "--batch", "--skip-column-names", "-e", "SELECT 1"],
                capture_output=True,
                text=True,
                env=subprocess_env(),
                timeout=10,
            )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "1"
        url = (
            f"mysql://{user}:{quote(password, safe='')}@{params['host']}:"
            f"{params['port']}/{params['dbname']}"
        )
        from db_git.backends.mysql.backend import MySQLBackend

        assert str(MySQLBackend().get_engine_version(url)).startswith("8.")
    finally:
        conn.execute("DROP USER %s@%s", (user, "%"))
