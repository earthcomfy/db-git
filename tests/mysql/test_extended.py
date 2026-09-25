from __future__ import annotations

import json
import sys
from contextlib import closing
from pathlib import Path

import pytest
from tests._pg_helpers import run_db_git, run_git
from tests.mysql.test_mysql import command, rows, selected

from db_git.backends.mysql.connections import Connection, identifier
from db_git.backends.mysql.operations import prefix
from db_git.config import load_config
from db_git.recovery import operations
from db_git.storage import snapshot_dump_path


def add_objects(project):
    seed = identifier(project[1]["dbname"])
    conn = project[4]
    conn.execute(f"CREATE TABLE {seed}.audit (message VARCHAR(100)) ENGINE=InnoDB")
    conn.execute(
        f"CREATE FUNCTION {seed}.user_count() RETURNS INT DETERMINISTIC "
        f"READS SQL DATA RETURN (SELECT COUNT(*) FROM {seed}.users)"
    )
    conn.execute(
        f"CREATE PROCEDURE {seed}.add_user(IN n VARCHAR(100)) "
        f"BEGIN INSERT INTO {seed}.users(name) VALUES(n); END"
    )
    conn.execute(f"CREATE VIEW {seed}.z_base AS SELECT id, name FROM {seed}.users")
    conn.execute(f"CREATE VIEW {seed}.a_dependent AS SELECT name FROM {seed}.z_base")
    conn.execute(
        f"CREATE TRIGGER {seed}.record_user AFTER INSERT ON {seed}.users "
        f"FOR EACH ROW INSERT INTO {seed}.audit VALUES(NEW.name)"
    )
    conn.execute(
        f"CREATE EVENT {seed}.daily ON SCHEDULE EVERY 1 DAY "
        "STARTS CURRENT_TIMESTAMP + INTERVAL 1 DAY "
        f"DO INSERT INTO {seed}.audit VALUES('scheduled')"
    )


def assert_objects(project, database):
    conn = project[4]
    name = identifier(database)
    original_count = len(rows(project, project[1]["dbname"]))
    assert conn.execute(f"SELECT {name}.user_count()").fetchone() == (original_count,)
    assert conn.execute(f"SELECT COUNT(*) FROM {name}.a_dependent").fetchone() == (
        original_count,
    )
    assert conn.execute(f"SELECT COUNT(*) FROM {name}.audit").fetchone() == (0,)
    conn.execute(f"CALL {name}.add_user('cloned-only')")
    assert conn.execute(f"SELECT * FROM {name}.audit").fetchall() == (("cloned-only",),)
    assert len(rows(project, project[1]["dbname"])) == original_count
    assert conn.execute(
        "SELECT STATUS FROM information_schema.EVENTS WHERE EVENT_SCHEMA=%s",
        (database,),
    ).fetchone() == ("DISABLED",)


def test_objects_cloned_with_local_references_and_events_disabled(project):
    add_objects(project)
    command(project, "create", "feature")
    assert_objects(project, selected(project))
    assert project[4].execute(
        "SELECT STATUS FROM information_schema.EVENTS WHERE EVENT_SCHEMA=%s",
        (project[1]["dbname"],),
    ).fetchone() == ("ENABLED",)


def shared(project):
    command(project, "init", "--mode", "shared")
    return load_config()


def active(project):
    return command(project, "url").stdout.strip().rsplit("/", 1)[1]


def test_shared_save_restore_recovery_and_snapshot_prune(project):
    add_objects(project)
    config = shared(project)
    seed = project[1]["dbname"]
    conn = project[4]
    command(project, "save", "main")
    conn.execute(f"INSERT INTO {identifier(seed)}.users(name) VALUES('working')")
    command(project, "restore", "main")
    restored = active(project)
    assert restored != seed
    assert len(rows(project, restored)) == 1
    assert len(rows(project, seed)) == 2
    assert conn.execute(
        "SELECT STATUS FROM information_schema.EVENTS WHERE EVENT_SCHEMA=%s",
        (restored,),
    ).fetchone() == ("DISABLED",)
    record = operations(config.snapshot_dir / ".operations")[0]
    command(project, "recover", record.id, "--rollback", "--yes")
    assert active(project) == seed
    command(project, "restore", "main")
    command(project, "save", "checkpoint")
    path = snapshot_dump_path(config.snapshot_dir, "checkpoint")
    assert path.is_file()
    command(project, "prune", "--yes")
    assert not path.exists()
    prune = operations(config.snapshot_dir / ".operations")[0]
    command(project, "recover", prune.id, "--rollback", "--yes")
    assert path.is_file()
    assert json.loads(command(project, "doctor", "--json", "--strict").stdout)["ok"]


def test_shared_checkout_run_and_old_connections(project):
    shared(project)
    root, params, _, env, conn = project
    run_git("checkout", "-b", "feature", cwd=root, env=env)
    first = active(project)
    conn.execute(f"INSERT INTO {identifier(first)}.users(name) VALUES('feature')")
    with closing(Connection(params)) as application:
        application.execute(f"USE {identifier(first)}")
        run_git("checkout", "main", cwd=root, env=env)
        assert len(rows(project, active(project))) == 1
        assert application.execute("SELECT DATABASE()").fetchone() == (first,)
        assert application.execute("SELECT COUNT(*) FROM users").fetchone() == (2,)
    run_git("checkout", "feature", cwd=root, env=env)
    assert len(rows(project, active(project))) == 2
    result = command(
        project,
        "run",
        "--",
        sys.executable,
        "-c",
        "import os; print(os.environ['DATABASE_URL'])",
    )
    assert result.stdout.strip().endswith("/" + active(project))
    assert not (root / ".git/db-git/disabled").exists()


def test_shared_corrupt_snapshot_preserves_working_database(project):
    config = shared(project)
    command(project, "save", "main")
    before = active(project)
    snapshot_dump_path(config.snapshot_dir, "main").write_bytes(b"invalid archive")
    result = command(project, "restore", "main", check=False)
    assert result.returncode == 1
    assert active(project) == before
    assert rows(project, before)


def test_shared_failed_hook_disables_switch_and_launch(project):
    config = shared(project)
    root, _, _, env, conn = project
    run_git("checkout", "-b", "feature", cwd=root, env=env)
    conn.execute(
        f"INSERT INTO {identifier(active(project))}.users(name) VALUES('feature')"
    )
    snapshot_dump_path(config.snapshot_dir, "main").write_bytes(b"broken")
    run_git("checkout", "main", cwd=root, env=env)
    assert (root / ".git/db-git/disabled").exists()
    assert (
        command(
            project, "run", "--", sys.executable, "-c", "pass", check=False
        ).returncode
        == 1
    )


def test_schema_grants_and_default_role_work(project):
    root, params, url, env, conn = project
    user = "scoped_" + params["dbname"]
    role = "role_" + params["dbname"]
    conn.execute("CREATE USER %s@%s IDENTIFIED BY %s", (user, "%", "role-password"))
    conn.execute("CREATE ROLE %s", (role,))
    try:
        conn.execute(f"GRANT ALL ON {identifier(params['dbname'])}.* TO '{role}'")
        conn.execute(
            f"GRANT ALL ON {identifier(prefix(params['dbname']) + '%')}.* TO '{role}'"
        )
        conn.execute(f"GRANT BACKUP_ADMIN, SHOW_ROUTINE ON *.* TO '{role}'")
        conn.execute("GRANT %s TO %s@%s", (role, user, "%"))
        conn.execute("SET DEFAULT ROLE %s TO %s@%s", (role, user, "%"))
        scoped_url = url.replace("root:dbgit-test-only", user + ":role-password")
        scoped_env = {**env, "DB_GIT_DATABASE_URL": scoped_url}
        run_db_git("create", "scoped", cwd=root, env=scoped_env)
        assert rows(project, selected(project, "scoped")) == rows(
            project, params["dbname"]
        )
    finally:
        conn.execute("DROP USER %s@%s", (user, "%"))
        conn.execute("DROP ROLE %s", (role,))


def test_shared_interrupted_restore_finish_and_retained_rollback(project, monkeypatch):
    import db_git.recovery as recovery
    from db_git.backends.mysql.shared import MySQLDumpStrategy

    config = shared(project)
    command(project, "save", "main")
    first = active(project)

    def interrupted(*args):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(recovery, "apply_metadata", interrupted)
        with pytest.raises(KeyboardInterrupt):
            MySQLDumpStrategy().restore(
                config.database_url, "main", config.snapshot_dir, config
            )
    record = operations(config.snapshot_dir / ".operations")[0]
    assert record.phase == "ready"
    assert command(project, "url", check=False).returncode == 1
    command(project, "recover", record.id, "--finish", "--yes")
    assert active(project) == record.stage
    command(project, "recover", record.id, "--rollback", "--yes")
    assert active(project) == first
    command(project, "recover", record.id, "--discard", "--yes")
    assert rows(project, first) and rows(project, record.stage)


def test_shared_interrupted_save_can_finish_then_roll_back(project, monkeypatch):
    import db_git.recovery as recovery
    from db_git.backends.mysql.shared import MySQLDumpStrategy

    config = shared(project)
    command(project, "save", "main")
    dump = snapshot_dump_path(config.snapshot_dir, "main")
    before = dump.read_bytes()
    project[4].execute(
        f"INSERT INTO {identifier(active(project))}.users(name) VALUES('later')"
    )

    def interrupted(*args):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(recovery, "apply_metadata", interrupted)
        with pytest.raises(KeyboardInterrupt):
            MySQLDumpStrategy().save(
                config.database_url, "main", config.snapshot_dir, config
            )
    record = operations(config.snapshot_dir / ".operations")[0]
    assert record.kind == "file" and record.phase == "ready"
    command(project, "recover", record.id, "--finish", "--yes")
    assert dump.read_bytes() != before
    command(project, "recover", record.id, "--rollback", "--yes")
    assert dump.read_bytes() == before
    command(project, "recover", record.id, "--discard", "--yes")
    assert not Path(record.stage).exists()


def test_mode_change_refused_when_resources_exist(project):
    command(project, "create", "feature")
    before = (project[0] / ".db-git.toml").read_bytes()
    result = command(project, "init", "--mode", "shared", check=False)
    assert result.returncode == 1
    assert (project[0] / ".db-git.toml").read_bytes() == before


def test_shared_worktree_layout_rejected(project):
    shared(project)
    root, _, _, env, _ = project
    result = run_git(
        "worktree",
        "add",
        "-b",
        "linked",
        str(root.parent / (root.name + "-linked")),
        cwd=root,
        env=env,
    )
    assert result.returncode == 0
    assert command(project, "save", "main", check=False).returncode == 1


def test_routine_creation_settings_and_unicode_are_preserved(project):
    conn = project[4]
    seed = identifier(project[1]["dbname"])
    conn.execute("SET SESSION sql_mode='ANSI_QUOTES,NO_BACKSLASH_ESCAPES'")
    conn.execute(
        f"CREATE PROCEDURE {seed}.message() "
        f"""SELECT 'héllo\\world', "name" FROM {seed}.users"""
    )
    conn.execute("SET SESSION sql_mode=''")
    command(project, "create", "feature")
    name = identifier(selected(project))
    assert conn.execute(f"CALL {name}.message()").fetchone() == (
        "héllo\\world",
        "seed é 🐈",
    )
    original = conn.execute(f"SHOW CREATE PROCEDURE {seed}.message").fetchone()
    copied = conn.execute(f"SHOW CREATE PROCEDURE {name}.message").fetchone()
    assert original[1] == copied[1]


def test_latin1_routine_definition_is_restored_without_mojibake(project):
    conn = project[4]
    seed = identifier(project[1]["dbname"])
    conn.execute(
        "SET SESSION character_set_client='latin1', "
        "collation_connection='latin1_swedish_ci'"
    )
    conn.execute(
        f"CREATE PROCEDURE {seed}.latin_message() SELECT 'café'".encode("latin1")
    )
    conn.execute(
        "SET SESSION character_set_client='utf8mb4', "
        "collation_connection='utf8mb4_unicode_ci'"
    )
    command(project, "create", "feature")
    name = identifier(selected(project))
    assert conn.execute(f"CALL {name}.latin_message()").fetchone() == ("café",)


def test_expired_snapshot_event_is_kept_disabled(project, tmp_path):
    import zipfile

    from db_git.backends.mysql.dump import capture, restore_archive
    from db_git.backends.mysql.operations import generation

    _, params, _, _, conn = project
    seed = params["dbname"]
    conn.execute(
        f"CREATE EVENT {identifier(seed)}.one_time "
        "ON SCHEDULE AT '2099-01-01 00:00:00' "
        "ON COMPLETION NOT PRESERVE DISABLE DO SELECT 1"
    )
    archive = tmp_path / "original.dump"
    capture(conn, params, seed, archive)
    old = tmp_path / "aged.dump"
    with zipfile.ZipFile(archive) as input_file, zipfile.ZipFile(old, "w") as output:
        manifest = json.loads(input_file.read("manifest.json"))
        manifest["objects"][0]["sql"] = manifest["objects"][0]["sql"].replace(
            "2099-01-01", "2000-01-01"
        )
        output.writestr("manifest.json", json.dumps(manifest))
        output.writestr("data.sql", input_file.read("data.sql"))
    destination = generation(seed, "aged")
    restore_archive(conn, params, old, destination)
    assert conn.execute(
        "SELECT STATUS, ON_COMPLETION FROM information_schema.EVENTS "
        "WHERE EVENT_SCHEMA=%s",
        (destination,),
    ).fetchone() == ("DISABLED", "PRESERVE")


def test_view_string_literals_preserve_session_quoting(project):
    from db_git.backends.mysql.dump import clone
    from db_git.backends.mysql.operations import generation

    _, params, _, _, conn = project
    seed = params["dbname"]
    conn.execute("SET SESSION sql_mode='NO_BACKSLASH_ESCAPES,ANSI_QUOTES'")
    conn.execute(
        f"CREATE VIEW {identifier(seed)}.literal AS SELECT 'a\\z' AS text_value"
    )
    destination = generation(seed, "quoting")
    clone(conn, params, seed, destination)
    assert conn.execute(
        f"SELECT text_value FROM {identifier(destination)}.literal"
    ).fetchone() == ("a\\z",)
