from __future__ import annotations

import subprocess

from tests._pg_helpers import git_commit_file, run_db_git, run_git


def run_init(
    cli_env: dict, mode: str, strategy: str, *extra: str
) -> subprocess.CompletedProcess:
    """
    Invoke `db-git init --mode <mode> --strategy <strategy>` via subprocess.
    """
    return run_db_git(
        "init",
        "--database-url",
        cli_env["db_url"],
        "--mode",
        mode,
        "--strategy",
        strategy,
        *extra,
        cwd=cli_env["repo"],
        env=cli_env["subprocess_env"],
    )


def make_branch(cli_env: dict, name: str) -> None:
    """
    Create and check out a new branch, committing a marker file.
    """
    run_git(
        "checkout",
        "-b",
        name,
        cwd=cli_env["repo"],
        env=cli_env["subprocess_env"],
    )
    git_commit_file(
        cli_env["repo"],
        f"{name}.txt",
        f"{name}\n",
        env=cli_env["subprocess_env"],
    )
