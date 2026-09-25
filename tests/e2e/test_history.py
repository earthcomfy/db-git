from __future__ import annotations

import json

import psycopg
import pytest

from db_git.config import write_config
from tests._pg_helpers import get_names, run_db_git, seed_users

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("strategy", ["template", "pgdump"])
def test_checkpoint_cli_and_doctor_follow_saved_strategy(cli_env, strategy):
    root, env = cli_env["repo"], cli_env["subprocess_env"]
    url = cli_env["db_url"]
    seed_users(url)

    def command(*args):
        return run_db_git(*args, cwd=root, env=env)

    command("init", "--strategy", strategy, "--mode", "shared")
    command("checkpoint", "baseline")
    saved = json.loads(command("history", "main", "--json").stdout)["checkpoints"]
    assert saved[0]["strategy"] == strategy
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("INSERT INTO users(name) VALUES('extra')")
    write_config(root, {"strategy": "pgdump" if strategy == "template" else "template"})
    command("restore", "--checkpoint", saved[0]["checkpoint_id"][:12])
    assert get_names(url) == ["Alice", "Bob", "Charlie"]
    assert json.loads(command("doctor", "--json", "--strict").stdout)["ok"]
    assert "Checkpoints:" in command("status").stderr
    assert "No snapshots found" in command("list").stderr
