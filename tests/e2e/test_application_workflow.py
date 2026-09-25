from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from tests._pg_helpers import (
    _db_git_binary,
    get_names,
    reconnect,
    run_db_git,
    run_git,
    seed_users,
)
from tests.e2e._helpers import run_init

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("strategy", ["template", "pgdump"])
def test_create_from_specific_branch_preserves_lineage_and_seed(cli_env, strategy):
    repo, env = cli_env["repo"], cli_env["subprocess_env"]
    seed_users(cli_env["db_url"])
    run_init(cli_env, "per-branch", strategy, "--no-hook")
    run_db_git("create", "source", cwd=repo, env=env)
    source_url = run_db_git("url", "source", cwd=repo, env=env).stdout.strip()
    with reconnect(source_url) as conn:
        conn.execute("INSERT INTO users(name) VALUES ('Source only')")
    # HEAD is still main; --from must choose the source database explicitly.
    run_db_git("create", "target", "--from", "source", cwd=repo, env=env)
    target_url = run_db_git("url", "target", cwd=repo, env=env).stdout.strip()
    assert get_names(target_url) == ["Alice", "Bob", "Charlie", "Source only"]
    assert get_names(cli_env["db_url"]) == ["Alice", "Bob", "Charlie"]
    state = json.loads((repo / ".git/db-git/state.json").read_text())
    assert state["databases"]["target"]["created_from"] == "source"
    again = run_db_git(
        "create", "target", "--from", "main", cwd=repo, env=env, check=False
    )
    assert again.returncode == 1
    assert get_names(target_url)[-1] == "Source only"
    run_db_git("create", "fresh", "--from", "main", cwd=repo, env=env)
    fresh_url = run_db_git("url", "fresh", cwd=repo, env=env).stdout.strip()
    assert get_names(fresh_url) == ["Alice", "Bob", "Charlie"]


def test_run_targets_branch_and_nested_management_keeps_seed(cli_env):
    repo, env = cli_env["repo"], cli_env["subprocess_env"]
    seed_users(cli_env["db_url"])
    run_init(cli_env, "per-branch", "template", "--no-hook")
    run_git("checkout", "-b", "feature", cwd=repo, env=env)
    run_db_git("create", cwd=repo, env=env)
    url = run_db_git("url", cwd=repo, env=env).stdout.strip()
    before = (repo / ".db-git.toml").read_bytes()
    script = """
import json, os, subprocess, sys, psycopg
with psycopg.connect(os.environ['DATABASE_URL'], autocommit=True) as conn:
    conn.execute("INSERT INTO users(name) VALUES ('From application')")
seed_url = subprocess.check_output([sys.argv[1], 'url', 'main'], text=True).strip()
print(json.dumps({'seed': seed_url, 'application': os.environ['DATABASE_URL']}))
"""
    result = run_db_git(
        "run",
        "--",
        sys.executable,
        "-c",
        script,
        _db_git_binary(),
        cwd=repo,
        env={**env, "DATABASE_URL": url},
    )
    assert json.loads(result.stdout) == {"seed": cli_env["db_url"], "application": url}
    assert get_names(url)[-1] == "From application"
    assert get_names(cli_env["db_url"]) == ["Alice", "Bob", "Charlie"]
    assert (repo / ".db-git.toml").read_bytes() == before
    # Exporting an application URL must also be safe outside db-git run.
    assert (
        run_db_git(
            "url", "main", cwd=repo, env={**env, "DATABASE_URL": url}
        ).stdout.strip()
        == cli_env["db_url"]
    )

    run_db_git("reset", "feature", cwd=repo, env={**env, "DATABASE_URL": url})
    assert get_names(url) == ["Alice", "Bob", "Charlie"]
    assert get_names(cli_env["db_url"]) == ["Alice", "Bob", "Charlie"]


def test_run_preserves_arguments_input_output_cwd_and_exit_status(cli_env):
    repo, env = cli_env["repo"], cli_env["subprocess_env"]
    run_init(cli_env, "shared", "template", "--no-hook")
    nested = repo / "nested"
    nested.mkdir()
    script = """
import os, json, sys
print(json.dumps({'args': sys.argv[1:], 'input': sys.stdin.read(), 'cwd': os.getcwd(),
                  'url': os.environ['DATABASE_URL']}))
print('child stderr', file=sys.stderr)
sys.exit(7)
"""
    args = ["--help", "a b", "$(touch should-not-exist)", "--", "-x"]
    result = run_db_git(
        "run",
        "--",
        sys.executable,
        "-c",
        script,
        *args,
        cwd=nested,
        env=env,
        input="hello\n",
        check=False,
    )
    assert result.returncode == 7
    assert result.stderr.strip() == "child stderr"
    assert json.loads(result.stdout) == {
        "args": args,
        "input": "hello\n",
        "cwd": str(nested),
        "url": cli_env["db_url"],
    }
    assert not (nested / "should-not-exist").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX exec and signal semantics")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_run_replaces_wrapper_and_receives_signals(cli_env, sig):
    repo, env = cli_env["repo"], cli_env["subprocess_env"]
    run_init(cli_env, "shared", "template", "--no-hook")
    ready = repo / "child.pid"
    script = """
import os, signal, sys, time
from pathlib import Path
signal.signal(signal.SIGINT, signal.SIG_DFL)
Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(60)
"""
    proc = subprocess.Popen(
        [_db_git_binary(), "run", "--", sys.executable, "-c", script, str(ready)],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while (
            not ready.exists() and proc.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert ready.exists(), "Application did not start"
        assert int(ready.read_text()) == proc.pid
        proc.send_signal(sig)
        assert proc.wait(timeout=10) == -sig
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)


def test_run_missing_command_and_non_executable_exit_codes(cli_env):
    repo, env = cli_env["repo"], cli_env["subprocess_env"]
    run_init(cli_env, "shared", "template", "--no-hook")
    missing = run_db_git(
        "run", "--", "db-git-no-such-command-123", cwd=repo, env=env, check=False
    )
    assert missing.returncode == 127
    blocked = repo / "not-executable"
    blocked.write_text("hello")
    blocked.chmod(0o600)
    result = run_db_git("run", "--", str(blocked), cwd=repo, env=env, check=False)
    assert result.returncode == 126


def test_reinit_keeps_seed_when_application_url_is_exported(cli_env):
    repo, env = cli_env["repo"], cli_env["subprocess_env"]
    run_init(cli_env, "per-branch", "template", "--no-hook")
    result = run_db_git(
        "init",
        "--no-hook",
        cwd=repo,
        env={
            **env,
            "DATABASE_URL": "postgresql://invalid:invalid@127.0.0.1:1/application",
        },
    )
    assert result.returncode == 0
    assert (
        run_db_git("url", "main", cwd=repo, env=env).stdout.strip() == cli_env["db_url"]
    )
