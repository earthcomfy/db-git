from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from db_git.recovery import operations
from db_git.repository import git_directory, operations_directory
from db_git.state import load_state
from tests._pg_helpers import reconnect, run_db_git, run_git, seed_users
from tests.e2e._helpers import run_init

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("strategy", ["template", "pgdump"])
def test_parallel_worktrees_share_management_but_isolate_application_data(
    cli_env, strategy
):
    main, env = cli_env["repo"], cli_env["subprocess_env"]
    seed_users(cli_env["db_url"])
    run_init(cli_env, "per-branch", strategy)
    linked = main / "linked checkout"
    run_git("worktree", "add", "-b", "feature", str(linked), cwd=main, env=env)
    # The actual post-checkout hook creates the database on worktree add.
    state = load_state(git_directory(main))
    assert state.databases["feature"].created_from == "main"
    assert not (linked / ".db-git.toml").exists()
    feature_url = run_db_git("url", cwd=linked, env=env).stdout.strip()
    with reconnect(feature_url) as conn:
        conn.execute("INSERT INTO users (name) VALUES ('feature only')")
    # Applications run concurrently against the correct database in each checkout.
    program = (
        "import os, psycopg; "
        "c=psycopg.connect(os.environ['DATABASE_URL']); "
        'print(c.execute("SELECT count(*) FROM users '
        "WHERE name='feature only'\").fetchone()[0]); "
        "assert os.environ['DB_GIT_DATABASE_URL'] == os.environ['EXPECTED_SEED']"
    )
    binary = str(Path(sys.executable).parent / "db-git")
    processes = [
        subprocess.Popen(
            [binary, "run", "--", sys.executable, "-c", program],
            cwd=root,
            env={**env, "EXPECTED_SEED": cli_env["db_url"]},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for root in (main, linked)
    ]
    outputs = [process.communicate(timeout=30) for process in processes]
    assert [process.returncode for process in processes] == [0, 0], outputs
    assert [out.strip() for out, _ in outputs] == ["0", "1"]
    # Explicitly clone a sibling branch using the common ownership records.
    run_db_git("create", "copy", "--from", "feature", cwd=main, env=env)
    copy_url = run_db_git("url", "copy", cwd=linked, env=env).stdout.strip()
    with reconnect(copy_url) as conn:
        assert conn.execute(
            "SELECT count(*) FROM users WHERE name='feature only'"
        ).fetchone() == (1,)
    # Reset in one worktree, then roll back from the other using the shared journal.
    run_db_git("reset", "feature", cwd=linked, env=env)
    root = operations_directory(git_directory(linked))
    reset = next(record for record in operations(root) if record.action == "reset")
    run_db_git("recover", reset.id, "--rollback", "--yes", cwd=main, env=env)
    with reconnect(feature_url) as conn:
        assert conn.execute(
            "SELECT count(*) FROM users WHERE name='feature only'"
        ).fetchone() == (1,)
    # Interrupted operations are visible and block application launch everywhere.
    reset.phase = "ready"
    reset.write(root)
    for checkout in (main, linked):
        listing = run_db_git("recover", cwd=checkout, env=env).stderr
        assert reset.id in listing
        result = run_db_git(
            "run",
            "--",
            sys.executable,
            "-c",
            "print('unexpected')",
            cwd=checkout,
            env=env,
            check=False,
        )
        assert result.returncode == 1
        assert "Interrupted operation" in result.stdout + result.stderr
        assert "unexpected" not in result.stdout
    reset.phase = "rolled_back"
    reset.write(root)


@pytest.mark.parametrize("strategy", ["template", "pgdump"])
def test_shared_mode_is_blocked_without_touching_database(cli_env, strategy):
    main, env = cli_env["repo"], cli_env["subprocess_env"]
    seed_users(cli_env["db_url"])
    run_init(cli_env, "shared", strategy)
    run_db_git("save", "main", cwd=main, env=env)
    snapshot_dir = main / ".git/db-git/snapshots"
    original_files = {
        p.name: p.read_bytes() for p in snapshot_dir.glob("*") if p.is_file()
    }
    linked = main / "linked"
    run_git("worktree", "add", "-b", "feature", str(linked), cwd=main, env=env)
    assert (git_directory(linked) / "db-git/disabled").exists()
    for command in (
        ("save", "main"),
        ("restore", "main"),
        ("run", "--", sys.executable, "-c", "print('unexpected')"),
        ("enable",),
    ):
        result = run_db_git(*command, cwd=linked, env=env, check=False)
        assert result.returncode == 1
        assert "multiple Git worktrees" in " ".join(
            (result.stdout + result.stderr).split()
        )
    assert {
        p.name: p.read_bytes() for p in snapshot_dir.glob("*") if p.is_file()
    } == original_files
    with reconnect(cli_env["db_url"]) as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone() == (3,)
    report = run_db_git("doctor", "--json", cwd=linked, env=env, check=False)
    checks = {check["id"]: check for check in json.loads(report.stdout)["checks"]}
    assert checks["worktrees"]["status"] == "error"
    assert "configuration.file" not in checks
    run_db_git("recover", cwd=linked, env=env)


def test_init_from_linked_worktree_uses_primary_config_and_default_branch(cli_env):
    main, env = cli_env["repo"], cli_env["subprocess_env"]
    linked = main / "linked"
    run_git("worktree", "add", "-b", "feature", str(linked), cwd=main, env=env)
    run_db_git(
        "init",
        "--database-url",
        cli_env["db_url"],
        "--mode",
        "per-branch",
        "--strategy",
        "template",
        cwd=linked,
        env=env,
    )
    assert 'default_branch = "main"' in (main / ".db-git.toml").read_text()
    assert not (linked / ".db-git.toml").exists()
    run_db_git("create", cwd=linked, env=env)
    assert "feature" in load_state(git_directory(main)).databases
