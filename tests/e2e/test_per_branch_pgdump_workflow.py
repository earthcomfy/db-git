from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path

import psycopg
import pytest

from db_git.storage import branch_db_name
from tests._pg_helpers import (
    build_url,
    get_default_branch,
    git_commit_file,
    reconnect,
    run_db_git,
    run_git,
    seed_users,
)
from tests.e2e._helpers import make_branch, run_init

pytestmark = pytest.mark.e2e


def _init(cli_env: dict, *extra: str) -> subprocess.CompletedProcess:
    return run_init(cli_env, "per-branch", "pgdump", *extra)


def _pg_has_db(cli_env: dict, dbname: str) -> bool:
    conn = reconnect(cli_env["db_url"])
    try:
        cur = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (dbname,),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def _state_data(repo: Path) -> dict:
    f = repo / ".git" / "db-git" / "state.json"
    return json.loads(f.read_text()) if f.exists() else {}


@pytest.fixture
def initialized(cli_env: dict) -> dict:
    _init(cli_env)
    return cli_env


class TestPerBranchPgdumpWorkflow:
    # -----------------------------------------------------------------------
    # Init
    # -----------------------------------------------------------------------

    def test_init_writes_config_and_installs_hook(self, cli_env: dict) -> None:
        _init(cli_env)
        repo = cli_env["repo"]

        toml = (repo / ".db-git.toml").read_text()
        assert 'mode = "per-branch"' in toml
        assert 'strategy = "pgdump"' in toml
        assert "default_branch" in toml

        hook = repo / ".git" / "hooks" / "post-checkout"
        assert hook.exists()
        assert "db-git-hook-v1" in hook.read_text()

    def test_init_no_hook_flag_skips_hook(self, cli_env: dict) -> None:
        _init(cli_env, "--no-hook")
        assert not (cli_env["repo"] / ".git" / "hooks" / "post-checkout").exists()

    def test_reinit_updates_existing_config(self, initialized: dict) -> None:
        run_db_git(
            "init",
            "--database-url",
            initialized["db_url"],
            "--mode",
            "per-branch",
            "--strategy",
            "pgdump",
            "--on-active-connections",
            "fail",
            cwd=initialized["repo"],
            env=initialized["subprocess_env"],
        )
        assert (
            'on_active_connections = "fail"'
            in (initialized["repo"] / ".db-git.toml").read_text()
        )

    # -----------------------------------------------------------------------
    # Config precedence
    # -----------------------------------------------------------------------

    def test_env_var_overrides_toml(self, initialized: dict) -> None:
        toml = initialized["repo"] / ".db-git.toml"
        toml.write_text(
            toml.read_text().replace(
                initialized["db_url"],
                "postgresql://nope:nope@127.0.0.1:1/nope",
            )
        )
        env = {**initialized["subprocess_env"], "DATABASE_URL": initialized["db_url"]}
        assert run_db_git("status", cwd=initialized["repo"], env=env).returncode == 0

    def test_cli_flag_overrides_env(self, initialized: dict) -> None:
        env = {
            **initialized["subprocess_env"],
            "DATABASE_URL": "postgresql://nope:nope@127.0.0.1:1/nope",
        }
        assert (
            run_db_git(
                "status",
                "--database-url",
                initialized["db_url"],
                cwd=initialized["repo"],
                env=env,
            ).returncode
            == 0
        )

    # -----------------------------------------------------------------------
    # Hook install / remove
    # -----------------------------------------------------------------------

    def test_hook_install_is_idempotent(self, initialized: dict) -> None:
        hook = initialized["repo"] / ".git" / "hooks" / "post-checkout"
        before = hook.read_text()
        run_db_git(
            "hook",
            "install",
            cwd=initialized["repo"],
            env=initialized["subprocess_env"],
        )
        assert hook.read_text() == before

    def test_install_preserves_legacy_and_remove_restores(self, cli_env: dict) -> None:
        hooks = cli_env["repo"] / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        legacy = "#!/bin/sh\nexit 0\n"
        (hooks / "post-checkout").write_text(legacy)
        (hooks / "post-checkout").chmod(0o755)

        _init(cli_env)
        preserved = hooks / "post-checkout.legacy"
        assert preserved.exists()

        run_db_git("hook", "remove", cwd=cli_env["repo"], env=cli_env["subprocess_env"])
        assert not preserved.exists()
        assert (hooks / "post-checkout").read_text() == legacy

    # -----------------------------------------------------------------------
    # Hook-driven branch DB creation
    # -----------------------------------------------------------------------

    def test_checkout_new_branch_auto_creates_branch_database(
        self, initialized: dict
    ) -> None:
        seed = initialized["pg_info"]["dbname"]
        default = get_default_branch(initialized["repo"])

        seed_users(initialized["db_url"])
        make_branch(initialized, "feature")

        branch_db = branch_db_name("feature", seed, default)
        assert _pg_has_db(initialized, branch_db)

        feature_url = build_url(initialized["pg_info"], branch_db)
        conn = reconnect(feature_url)
        try:
            cur = conn.execute("SELECT name FROM users ORDER BY name")
            assert [r[0] for r in cur.fetchall()] == ["Alice", "Bob", "Charlie"]
        finally:
            conn.close()

    def test_branch_db_persists_across_checkouts(self, initialized: dict) -> None:
        seed = initialized["pg_info"]["dbname"]
        default = get_default_branch(initialized["repo"])
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        make_branch(initialized, "feature")
        feature_db = branch_db_name("feature", seed, default)
        feature_url = build_url(initialized["pg_info"], feature_db)

        conn = reconnect(feature_url)
        try:
            conn.execute("ALTER TABLE users ADD COLUMN tag varchar(20)")
            conn.execute("INSERT INTO users (name, tag) VALUES ('Ev', 'f')")
        finally:
            conn.close()

        run_git("checkout", default, cwd=repo, env=env)
        run_git("checkout", "feature", cwd=repo, env=env)

        conn = reconnect(feature_url)
        try:
            cur = conn.execute("SELECT tag FROM users WHERE name = 'Ev'")
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "f"
        finally:
            conn.close()

    def test_lineage_new_branch_from_current_seeds_from_current(
        self, initialized: dict
    ) -> None:
        seed = initialized["pg_info"]["dbname"]
        default = get_default_branch(initialized["repo"])
        repo = initialized["repo"]

        seed_users(initialized["db_url"])
        make_branch(initialized, "feature-a")

        feature_a_db = branch_db_name("feature-a", seed, default)
        conn = reconnect(build_url(initialized["pg_info"], feature_a_db))
        try:
            conn.execute("INSERT INTO users (name) VALUES ('FromA')")
        finally:
            conn.close()

        make_branch(initialized, "feature-b")
        feature_b_db = branch_db_name("feature-b", seed, default)

        conn = reconnect(build_url(initialized["pg_info"], feature_b_db))
        try:
            cur = conn.execute("SELECT name FROM users ORDER BY name")
            assert "FromA" in [r[0] for r in cur.fetchall()]
        finally:
            conn.close()

        state = _state_data(repo)
        assert state["databases"]["feature-b"]["created_from"] == "feature-a"

    # -----------------------------------------------------------------------
    # Explicit create / reset
    # -----------------------------------------------------------------------

    def test_create_proactively_builds_branch_db(self, initialized: dict) -> None:
        seed = initialized["pg_info"]["dbname"]
        default = get_default_branch(initialized["repo"])
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        run_git("checkout", "-b", "ahead", cwd=repo, env=env)
        db_name = branch_db_name("ahead", seed, default)
        if _pg_has_db(initialized, db_name):
            conn = psycopg.connect(
                host=initialized["pg_info"]["host"],
                port=initialized["pg_info"]["port"],
                user=initialized["pg_info"]["user"],
                password=initialized["pg_info"]["password"] or None,
                dbname="postgres",
                autocommit=True,
            )
            try:
                conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
            finally:
                conn.close()

        run_db_git("create", "ahead", cwd=repo, env=env)
        assert _pg_has_db(initialized, db_name)

    def test_create_refuses_when_branch_db_exists(self, initialized: dict) -> None:
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        make_branch(initialized, "feature")

        result = run_db_git("create", "feature", cwd=repo, env=env, check=False)
        assert result.returncode != 0

    def test_reset_drops_and_reclones_from_seed(self, initialized: dict) -> None:
        seed = initialized["pg_info"]["dbname"]
        default = get_default_branch(initialized["repo"])
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        make_branch(initialized, "feature")
        feature_db = branch_db_name("feature", seed, default)
        feature_url = build_url(initialized["pg_info"], feature_db)

        conn = reconnect(feature_url)
        try:
            conn.execute("INSERT INTO users (name) VALUES ('Mut')")
        finally:
            conn.close()

        run_db_git("reset", "feature", cwd=repo, env=env)

        conn = reconnect(feature_url)
        try:
            cur = conn.execute("SELECT name FROM users ORDER BY name")
            assert [r[0] for r in cur.fetchall()] == ["Alice", "Bob", "Charlie"]
        finally:
            conn.close()

    def test_reset_refuses_default_branch(self, initialized: dict) -> None:
        default = get_default_branch(initialized["repo"])
        result = run_db_git(
            "reset",
            default,
            cwd=initialized["repo"],
            env=initialized["subprocess_env"],
            check=False,
        )
        assert result.returncode != 0

    # -----------------------------------------------------------------------
    # Disable / enable / DB_GIT_SKIP
    # -----------------------------------------------------------------------

    def test_disable_gates_the_hook(self, initialized: dict) -> None:
        seed = initialized["pg_info"]["dbname"]
        default = get_default_branch(initialized["repo"])
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        run_db_git("disable", cwd=repo, env=env)

        make_branch(initialized, "feature")
        feature_db = branch_db_name("feature", seed, default)
        assert not _pg_has_db(initialized, feature_db)

    def test_db_git_skip_env_bypasses_auto_create(self, initialized: dict) -> None:
        seed = initialized["pg_info"]["dbname"]
        default = get_default_branch(initialized["repo"])
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        skip_env = {**env, "DB_GIT_SKIP": "1"}
        run_git("checkout", "-b", "feature", cwd=repo, env=skip_env)
        git_commit_file(repo, "f.txt", "f\n", env=skip_env)

        feature_db = branch_db_name("feature", seed, default)
        assert not _pg_has_db(initialized, feature_db)

    # -----------------------------------------------------------------------
    # Active-connections policy
    # -----------------------------------------------------------------------

    def test_terminate_policy_allows_reset_under_open_connection(
        self, cli_env: dict
    ) -> None:
        _init(cli_env, "--on-active-connections", "terminate")
        seed = cli_env["pg_info"]["dbname"]
        default = get_default_branch(cli_env["repo"])
        seed_users(cli_env["db_url"])
        make_branch(cli_env, "feature")

        feature_db = branch_db_name("feature", seed, default)
        holder = reconnect(build_url(cli_env["pg_info"], feature_db))
        try:
            run_db_git(
                "reset",
                "feature",
                cwd=cli_env["repo"],
                env=cli_env["subprocess_env"],
            )
        finally:
            with contextlib.suppress(Exception):
                holder.close()

    def test_fail_policy_refuses_reset_on_active_connections(
        self, cli_env: dict
    ) -> None:
        _init(cli_env, "--on-active-connections", "fail")
        seed = cli_env["pg_info"]["dbname"]
        default = get_default_branch(cli_env["repo"])
        seed_users(cli_env["db_url"])
        make_branch(cli_env, "feature")

        feature_db = branch_db_name("feature", seed, default)
        holder = reconnect(build_url(cli_env["pg_info"], feature_db))
        try:
            result = run_db_git(
                "reset",
                "feature",
                cwd=cli_env["repo"],
                env=cli_env["subprocess_env"],
                check=False,
            )
            assert result.returncode != 0
            assert not holder.closed
            cur = holder.execute("SELECT 1")
            assert cur.fetchone() == (1,)
        finally:
            with contextlib.suppress(Exception):
                holder.close()

    # -----------------------------------------------------------------------
    # Hook safety: never block git checkout
    # -----------------------------------------------------------------------

    def test_hook_exits_zero_when_pg_unreachable(self, initialized: dict) -> None:
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        broken = {**env, "DATABASE_URL": "postgresql://x:x@127.0.0.1:1/x"}
        result = subprocess.run(
            ["git", "checkout", "-b", "feature"],
            cwd=repo,
            env=broken,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_hook_exits_zero_with_invalid_toml(self, initialized: dict) -> None:
        repo = initialized["repo"]
        env = initialized["subprocess_env"]
        (repo / ".db-git.toml").write_text("not [[[ valid toml")
        result = subprocess.run(
            ["git", "checkout", "-b", "feature"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_hook_exits_zero_without_config_file(self, cli_env: dict) -> None:
        repo = cli_env["repo"]
        env = cli_env["subprocess_env"]

        run_db_git("hook", "install", cwd=repo, env=env)
        run_git("checkout", "-b", "feature", cwd=repo, env=env)
        git_commit_file(repo, "f.txt", "f\n", env=env)

        default = get_default_branch(repo)
        result = subprocess.run(
            ["git", "checkout", default],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_hook_exits_zero_when_pg_dump_broken(
        self, initialized: dict, tmp_path: Path
    ) -> None:
        """
        Per-branch pgdump: the hook tries to auto-create a branch DB via
        pg_dump|pg_restore. If pg_dump is broken, the DB creation fails but
        the checkout must still exit 0.
        """
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])

        fakebin = tmp_path / "fakebin"
        fakebin.mkdir()
        broken = fakebin / "pg_dump"
        broken.write_text("#!/bin/sh\necho broken >&2\nexit 1\n")
        broken.chmod(0o755)

        shadow = {**env, "PATH": f"{fakebin}:{env['PATH']}"}
        result = subprocess.run(
            ["git", "checkout", "-b", "feature"],
            cwd=repo,
            env=shadow,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    # -----------------------------------------------------------------------
    # Edge cases the hook must skip
    # -----------------------------------------------------------------------

    def test_hook_skips_during_paused_rebase(
        self, initialized: dict, tmp_path: Path
    ) -> None:
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        git_commit_file(repo, "a.txt", "a\n", env=env)
        git_commit_file(repo, "b.txt", "b\n", env=env)

        editor = tmp_path / "seq-editor.sh"
        editor.write_text("#!/bin/sh\nsed -i.bak '1s/^pick/edit/' \"$1\"\n")
        editor.chmod(0o755)
        rebase_env = {
            **env,
            "GIT_SEQUENCE_EDITOR": str(editor),
            "GIT_EDITOR": "true",
        }
        before = _state_data(repo).get("databases", {})

        run_git("rebase", "-i", "HEAD~2", cwd=repo, env=rebase_env)
        run_git("rebase", "--abort", cwd=repo, env=env, check=False)

        after = _state_data(repo).get("databases", {})
        assert after == before

    def test_hook_skips_on_detached_head(self, initialized: dict) -> None:
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        before = _state_data(repo).get("databases", {})
        result = subprocess.run(
            ["git", "checkout", head_sha],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert _state_data(repo).get("databases", {}) == before

    def test_hook_skips_on_file_only_checkout(self, initialized: dict) -> None:
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        (repo / "f.txt").write_text("v1\n")
        run_git("add", "f.txt", cwd=repo, env=env)
        run_git("commit", "-m", "v1", cwd=repo, env=env)
        (repo / "f.txt").write_text("v2\n")

        before = _state_data(repo).get("databases", {})
        result = subprocess.run(
            ["git", "checkout", "--", "f.txt"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert _state_data(repo).get("databases", {}) == before

    def test_hook_tolerates_fresh_clone_with_null_prev_head(
        self, initialized: dict, tmp_path: Path
    ) -> None:
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        (repo / ".gitignore").write_text(".db-git/\n")
        run_git("add", ".db-git.toml", ".gitignore", cwd=repo, env=env)
        run_git("commit", "-m", "add db-git config", cwd=repo, env=env)

        bare = tmp_path / "upstream.git"
        run_git("clone", "--bare", str(repo), str(bare), cwd=tmp_path, env=env)

        template = tmp_path / "gittemplate"
        (template / "hooks").mkdir(parents=True)
        (template / "hooks" / "post-checkout").write_text(
            (repo / ".git" / "hooks" / "post-checkout").read_text()
        )
        (template / "hooks" / "post-checkout").chmod(0o755)

        fresh = tmp_path / "fresh"
        result = subprocess.run(
            ["git", "clone", f"--template={template}", str(bare), str(fresh)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    # -----------------------------------------------------------------------
    # Branch name sanitization
    # -----------------------------------------------------------------------

    def test_branch_name_with_slash_sanitized(self, initialized: dict) -> None:
        seed = initialized["pg_info"]["dbname"]
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        run_git("checkout", "-b", "feature/auth", cwd=repo, env=env)
        git_commit_file(repo, "a.txt", "a\n", env=env)

        expected_db = f"{seed}__feature__auth"
        assert _pg_has_db(initialized, expected_db)

    def test_long_branch_name_uses_hash_fallback(self, initialized: dict) -> None:
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        long_name = "x" * 100
        run_git("checkout", "-b", long_name, cwd=repo, env=env)
        git_commit_file(repo, "l.txt", "l\n", env=env)

        state = _state_data(repo)
        assert long_name in state["databases"]
        db_name = state["databases"][long_name]["db_name"]
        assert "__h" in db_name
        assert _pg_has_db(initialized, db_name)

    # -----------------------------------------------------------------------
    # Prune
    # -----------------------------------------------------------------------

    def test_prune_dry_run_does_not_drop(self, initialized: dict) -> None:
        seed = initialized["pg_info"]["dbname"]
        default = get_default_branch(initialized["repo"])
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        make_branch(initialized, "stale")
        run_git("checkout", default, cwd=repo, env=env)
        run_git("branch", "-D", "stale", cwd=repo, env=env)

        stale_db = branch_db_name("stale", seed, default)
        assert _pg_has_db(initialized, stale_db)

        run_db_git("prune", "--dry-run", cwd=repo, env=env)
        assert _pg_has_db(initialized, stale_db)

    def test_prune_yes_drops_stale_branch_dbs(self, initialized: dict) -> None:
        seed = initialized["pg_info"]["dbname"]
        default = get_default_branch(initialized["repo"])
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        make_branch(initialized, "stale")
        run_git("checkout", default, cwd=repo, env=env)
        run_git("branch", "-D", "stale", cwd=repo, env=env)

        run_db_git("prune", "--yes", cwd=repo, env=env)

        stale_db = branch_db_name("stale", seed, default)
        assert not _pg_has_db(initialized, stale_db)
        assert "stale" not in _state_data(repo).get("databases", {})

    def test_prune_under_fail_policy_preserves_active_branch_db(
        self, cli_env: dict
    ) -> None:
        """
        fail policy: a held connection prevents prune from dropping the DB.
        """
        _init(cli_env, "--on-active-connections", "fail")
        seed = cli_env["pg_info"]["dbname"]
        default = get_default_branch(cli_env["repo"])
        repo = cli_env["repo"]
        env = cli_env["subprocess_env"]

        seed_users(cli_env["db_url"])
        make_branch(cli_env, "stuck")
        run_git("checkout", default, cwd=repo, env=env)
        run_git("branch", "-D", "stuck", cwd=repo, env=env)

        stuck_db = branch_db_name("stuck", seed, default)
        holder = reconnect(build_url(cli_env["pg_info"], stuck_db))
        try:
            run_db_git("prune", "--yes", cwd=repo, env=env)
        finally:
            with contextlib.suppress(Exception):
                holder.close()
        assert _pg_has_db(cli_env, stuck_db)

    def test_prune_refuses_non_tty_without_yes(self, initialized: dict) -> None:
        repo = initialized["repo"]
        env = initialized["subprocess_env"]
        default = get_default_branch(repo)

        seed_users(initialized["db_url"])
        make_branch(initialized, "stale")
        run_git("checkout", default, cwd=repo, env=env)
        run_git("branch", "-D", "stale", cwd=repo, env=env)

        result = run_db_git("prune", cwd=repo, env=env, check=False)
        assert result.returncode != 0

    # -----------------------------------------------------------------------
    # list / status / url
    # -----------------------------------------------------------------------

    def test_list_shows_branch_databases(self, initialized: dict) -> None:
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        make_branch(initialized, "feature")

        result = run_db_git("list", cwd=repo, env=env)
        out = result.stdout + result.stderr
        assert "feature" in out

    def test_status_shows_per_branch_pgdump(self, initialized: dict) -> None:
        repo = initialized["repo"]
        env = initialized["subprocess_env"]
        seed_users(initialized["db_url"])
        result = run_db_git("status", cwd=repo, env=env)
        out = result.stdout + result.stderr
        assert "per-branch" in out
        assert "pgdump" in out

    def test_url_emits_connectable_branch_url(self, initialized: dict) -> None:
        pg_info = initialized["pg_info"]
        seed = pg_info["dbname"]
        default = get_default_branch(initialized["repo"])
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        make_branch(initialized, "feature")
        feature_db = branch_db_name("feature", seed, default)

        result = run_db_git("url", cwd=repo, env=env)
        printed = result.stdout.strip()
        assert printed == build_url(pg_info, feature_db)

        conn = reconnect(printed)
        try:
            cur = conn.execute("SELECT name FROM users ORDER BY name")
            assert [r[0] for r in cur.fetchall()] == ["Alice", "Bob", "Charlie"]
        finally:
            conn.close()

    def test_url_for_default_branch_returns_seed_url(self, initialized: dict) -> None:
        result = run_db_git(
            "url",
            get_default_branch(initialized["repo"]),
            cwd=initialized["repo"],
            env=initialized["subprocess_env"],
        )
        assert result.stdout.strip() == build_url(initialized["pg_info"])

    # -----------------------------------------------------------------------
    # Shared-mode-only commands
    # -----------------------------------------------------------------------

    def test_save_is_no_op_in_per_branch(self, initialized: dict) -> None:
        result = run_db_git(
            "save",
            cwd=initialized["repo"],
            env=initialized["subprocess_env"],
        )
        out = result.stdout + result.stderr
        assert "per-branch" in out.lower() or "create" in out.lower()

    def test_restore_is_no_op_in_per_branch(self, initialized: dict) -> None:
        result = run_db_git(
            "restore",
            cwd=initialized["repo"],
            env=initialized["subprocess_env"],
        )
        out = result.stdout + result.stderr
        assert "per-branch" in out.lower() or "reset" in out.lower()

    def test_commands_require_init(self, cli_env: dict) -> None:
        for cmd in ("create", "reset", "list", "status", "prune"):
            result = run_db_git(
                cmd,
                cwd=cli_env["repo"],
                env=cli_env["subprocess_env"],
                check=False,
            )
            assert result.returncode != 0, f"{cmd} unexpectedly succeeded pre-init"
