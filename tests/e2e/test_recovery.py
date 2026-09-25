import pytest

from db_git.recovery import operations
from tests._pg_helpers import get_names, reconnect, run_db_git, seed_users
from tests.e2e._helpers import run_init

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("strategy", ["template", "pgdump"])
def test_cli_lists_rolls_back_and_discards_restore(cli_env, strategy):
    run_init(cli_env, "shared", strategy, "--no-hook")
    repo, env, url = cli_env["repo"], cli_env["subprocess_env"], cli_env["db_url"]
    seed_users(url)
    run_db_git("save", cwd=repo, env=env)
    with reconnect(url) as conn:
        conn.execute("INSERT INTO users (name) VALUES ('Unsaved')")
    run_db_git("restore", cwd=repo, env=env)
    assert "Unsaved" not in get_names(url)
    root = repo / ".git" / "db-git" / "snapshots" / ".operations"
    record = next(r for r in operations(root) if r.action == "restore")
    result = run_db_git("recover", cwd=repo, env=env)
    assert record.id in result.stderr + result.stdout
    run_db_git("recover", record.id, "--rollback", "--yes", cwd=repo, env=env)
    assert "Unsaved" in get_names(url)
    run_db_git("recover", record.id, "--discard", "--yes", cwd=repo, env=env)
    assert record.id not in {r.id for r in operations(root)}
    assert "Unsaved" in get_names(url)
