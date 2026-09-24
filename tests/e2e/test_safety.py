import json

import pytest
from psycopg import sql

from db_git.state import record_branch_db
from db_git.storage import metadata_path, snapshot_dump_path
from tests._pg_helpers import get_names, reconnect, run_db_git, run_git, seed_users
from tests.e2e._helpers import run_init

pytestmark = pytest.mark.e2e


def _add_user(url, name):
    with reconnect(url) as conn:
        conn.execute("INSERT INTO users (name) VALUES (%s)", (name,))


def _url(cli_env):
    return run_db_git(
        "url", cwd=cli_env["repo"], env=cli_env["subprocess_env"]
    ).stdout.strip()


@pytest.mark.parametrize("strategy", ["template", "pgdump"])
@pytest.mark.parametrize("mode", ["shared", "per-branch"])
def test_colliding_branch_names_keep_independent_data(cli_env, strategy, mode):
    run_init(cli_env, mode, strategy)
    seed_users(cli_env["db_url"])
    repo, env = cli_env["repo"], cli_env["subprocess_env"]
    run_git("checkout", "-b", "feature/foo-bar", cwd=repo, env=env)
    _add_user(_url(cli_env), "First")
    run_git("checkout", "main", cwd=repo, env=env)
    run_git("checkout", "-b", "feature/foo_bar", cwd=repo, env=env)
    assert "First" not in get_names(_url(cli_env))
    _add_user(_url(cli_env), "Second")
    run_git("checkout", "feature/foo-bar", cwd=repo, env=env)
    assert get_names(_url(cli_env)) == ["Alice", "Bob", "Charlie", "First"]
    run_git("checkout", "feature/foo_bar", cwd=repo, env=env)
    assert get_names(_url(cli_env)) == ["Alice", "Bob", "Charlie", "Second"]


@pytest.mark.parametrize("strategy", ["template", "pgdump"])
def test_legacy_shared_snapshot_can_restore_save_and_prune(cli_env, strategy):
    run_init(cli_env, "shared", strategy, "--no-hook")
    repo, env = cli_env["repo"], cli_env["subprocess_env"]
    seed_users(cli_env["db_url"])
    run_db_git("save", "feature/foo-bar", cwd=repo, env=env)
    snapshots = repo / ".git" / "db-git" / "snapshots"
    meta_path = metadata_path(snapshots, "feature/foo-bar")
    meta = json.loads(meta_path.read_text())
    legacy_meta = snapshots / "feature__foo_bar.meta.json"
    if strategy == "pgdump":
        snapshot_dump_path(snapshots, "feature/foo-bar").rename(
            snapshots / "feature__foo_bar.dump"
        )
    else:
        from db_git.backends.postgresql.backend import PostgresqlBackend
        from db_git.db import parse_database_url
        from db_git.storage import snapshot_db_name

        backend = PostgresqlBackend()
        params = backend.apply_url_defaults(parse_database_url(cli_env["db_url"]))
        conn = backend.connect_maintenance(params)
        try:
            conn.execute(
                sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                    sql.Identifier(
                        snapshot_db_name("feature/foo-bar", meta["database"])
                    ),
                    sql.Identifier(f"_dbgit_{meta['database']}_feature__foo_bar"),
                )
            )
        finally:
            conn.close()
    meta_path.rename(legacy_meta)
    _add_user(cli_env["db_url"], "Later")
    run_db_git("restore", "feature/foo-bar", cwd=repo, env=env)
    assert get_names(cli_env["db_url"]) == ["Alice", "Bob", "Charlie"]
    run_db_git("save", "feature/foo_bar", cwd=repo, env=env)
    _add_user(cli_env["db_url"], "Updated")
    run_db_git("save", "feature/foo-bar", cwd=repo, env=env)
    assert legacy_meta.exists()
    run_db_git("restore", "feature/foo_bar", cwd=repo, env=env)
    assert get_names(cli_env["db_url"]) == ["Alice", "Bob", "Charlie"]
    run_db_git("restore", "feature/foo-bar", cwd=repo, env=env)
    assert "Updated" in get_names(cli_env["db_url"])
    run_db_git("prune", "--yes", cwd=repo, env=env)
    assert not legacy_meta.exists()


@pytest.mark.parametrize("strategy", ["template", "pgdump"])
def test_legacy_branch_database_is_used_by_url_checkout_and_reset(cli_env, strategy):
    run_init(cli_env, "per-branch", strategy)
    repo, env = cli_env["repo"], cli_env["subprocess_env"]
    seed_users(cli_env["db_url"])
    run_git("checkout", "-b", "feature", cwd=repo, env=env)
    current_url = _url(cli_env)
    _add_user(current_url, "Legacy")
    run_git("checkout", "main", cwd=repo, env=env)

    from db_git.backends.postgresql.backend import PostgresqlBackend
    from db_git.db import parse_database_url

    backend = PostgresqlBackend()
    params = backend.apply_url_defaults(parse_database_url(cli_env["db_url"]))
    legacy_name = f"{params['dbname']}__feature"
    current_name = parse_database_url(current_url)["dbname"]
    conn = backend.connect_maintenance(params)
    try:
        conn.execute(
            sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                sql.Identifier(str(current_name)), sql.Identifier(legacy_name)
            )
        )
    finally:
        conn.close()
    record_branch_db(repo / ".git", "feature", legacy_name, "main")
    run_git("checkout", "feature", cwd=repo, env=env)
    assert _url(cli_env).endswith("/" + legacy_name)
    assert "Legacy" in get_names(_url(cli_env))
    run_db_git("reset", cwd=repo, env=env)
    assert _url(cli_env).endswith("/" + legacy_name)
    assert get_names(_url(cli_env)) == ["Alice", "Bob", "Charlie"]


def test_failed_save_keeps_working_data_until_explicit_recovery(cli_env):
    run_init(cli_env, "shared", "template", "--on-active-connections", "fail")
    repo, env = cli_env["repo"], cli_env["subprocess_env"]
    url = cli_env["db_url"]
    seed_users(url)
    run_git("checkout", "-b", "feature", cwd=repo, env=env)
    _add_user(url, "Unsaved")
    with reconnect(url):
        result = run_git("checkout", "main", cwd=repo, env=env)
        assert result.returncode == 0
    assert get_names(url) == ["Alice", "Bob", "Charlie", "Unsaved"]
    assert (repo / ".git" / "db-git" / "disabled").exists()
    # A subsequent checkout must not mislabel the preserved data as main's.
    run_git("checkout", "feature", cwd=repo, env=env)
    run_git("checkout", "main", cwd=repo, env=env)
    run_db_git("save", "feature", cwd=repo, env=env)
    run_db_git("restore", "main", cwd=repo, env=env)
    run_db_git("enable", cwd=repo, env=env)
    assert get_names(url) == ["Alice", "Bob", "Charlie"]
    run_git("checkout", "feature", cwd=repo, env=env)
    assert "Unsaved" in get_names(url)
