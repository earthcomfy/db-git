from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import psycopg


def build_url(info: dict, dbname: str | None = None) -> str:
    """
    Build a postgresql:// URL from a pg_info dict.

    If `dbname` is provided, it overrides `info['dbname']`. Used by
    per-branch tests to connect directly to branch-scoped databases.
    """
    password_part = f":{info['password']}" if info["password"] else ""
    target = dbname or info["dbname"]
    return (
        f"postgresql://{info['user']}{password_part}"
        f"@{info['host']}:{info['port']}/{target}"
    )


def reconnect(url: str) -> psycopg.Connection:
    """
    Open a fresh autocommit psycopg connection.
    """
    return psycopg.connect(url, autocommit=True)


def get_names(url: str) -> list[str]:
    """
    Query users table and return sorted names.
    """
    conn = reconnect(url)
    try:
        cur = conn.execute("SELECT name FROM users ORDER BY name")
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def get_columns(url: str, table: str) -> list[str]:
    """
    Return column names for a table.
    """
    conn = reconnect(url)
    try:
        cur = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s ORDER BY ordinal_position",
            (table,),
        )
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def seed_users(url: str) -> None:
    """
    Create users table with 3 rows, then close connection.
    """
    conn = reconnect(url)
    try:
        conn.execute("""
            CREATE TABLE users (
                id serial PRIMARY KEY,
                name varchar(100) NOT NULL
            )
        """)
        conn.execute("INSERT INTO users (name) VALUES ('Alice'), ('Bob'), ('Charlie')")
    finally:
        conn.close()


def get_default_branch(repo_path: Path) -> str:
    """
    Detect whether the default branch is 'main' or 'master'.
    """
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
        cwd=repo_path,
    )
    return result.stdout.strip()


def admin_exec(host: str, port: int, user: str, password: str, stmt: str) -> None:
    """
    Run a single admin statement against the maintenance (postgres) DB.
    """
    conn = psycopg.connect(
        host=host,
        port=port,
        user=user,
        password=password or None,
        dbname="postgres",
        autocommit=True,
    )
    try:
        conn.execute(stmt)
    finally:
        conn.close()


def drop_db_and_branches(
    host: str, port: int, user: str, password: str, dbname: str
) -> None:
    """
    Drop the test DB and any branch DBs whose name starts with {dbname}__.
    """
    conn = psycopg.connect(
        host=host,
        port=port,
        user=user,
        password=password or None,
        dbname="postgres",
        autocommit=True,
    )
    try:
        pattern = f"{dbname}__%"
        cur = conn.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %s",
            (pattern,),
        )
        for row in cur.fetchall():
            conn.execute(f'DROP DATABASE IF EXISTS "{row[0]}" WITH (FORCE)')
        # Also drop shared-mode snapshot DBs: _gitdb_{dbname}_%
        snapshot_pattern = f"_gitdb_{dbname}_%"
        cur = conn.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %s",
            (snapshot_pattern,),
        )
        for row in cur.fetchall():
            conn.execute(f'DROP DATABASE IF EXISTS "{row[0]}" WITH (FORCE)')
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    finally:
        conn.close()


def _git_db_binary() -> str:
    """
    Locate the git-db console script installed next to this venv's python.
    """
    return str(Path(sys.executable).parent / "git-db")


def run_git_db(
    *args: str,
    env: dict,
    cwd: Path | None = None,
    check: bool = True,
    input: str | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """
    Invoke the git-db console script installed in this venv as a real
    subprocess. This matches how the post-checkout hook invokes git-db
    (via `command -v git-db`), so tests exercise the same binary shape.

    On `check=True`, a non-zero exit raises CalledProcessError with
    stdout and stderr captured. Failures in a test re-raise that
    exception; stderr_on_fail helpers can expose the CLI's Rich-formatted
    error output for debugging.
    """
    result = subprocess.run(
        [_git_db_binary(), *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        input=input,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git-db {' '.join(args)} exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def run_git(
    *args: str,
    env: dict,
    cwd: Path,
    check: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """
    Invoke a git command in the given cwd with the given env.
    """
    return subprocess.run(
        ["git", *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def git_commit_file(repo: Path, filename: str, content: str, env: dict) -> None:
    """
    Write a file, git add, git commit - common scaffolding for branch tests.
    """
    (repo / filename).write_text(content)
    run_git("add", filename, cwd=repo, env=env)
    run_git("commit", "-m", f"add {filename}", cwd=repo, env=env)
