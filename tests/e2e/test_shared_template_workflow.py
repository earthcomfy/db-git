from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

import pytest

from tests._pg_helpers import (
    get_columns,
    get_default_branch,
    get_names,
    git_commit_file,
    reconnect,
    run_db_git,
    run_git,
    seed_users,
)
from tests.e2e._helpers import make_branch, run_init

pytestmark = pytest.mark.e2e


def _init(cli_env: dict, *extra: str) -> subprocess.CompletedProcess:
    return run_init(cli_env, "shared", "template", *extra)


def _snapshot_dir(repo: Path) -> Path:
    return repo / ".git" / "db-git" / "snapshots"


def _meta_files(repo: Path) -> set[str]:
    d = _snapshot_dir(repo)
    return {p.name for p in d.glob("*.meta.json")} if d.exists() else set()


@pytest.fixture
def initialized(cli_env: dict) -> dict:
    """
    cli_env after `db-git init --mode shared --strategy template`.
    """
    _init(cli_env)
    return cli_env


class TestSharedTemplateWorkflow:
    # -----------------------------------------------------------------------
    # Init
    # -----------------------------------------------------------------------

    def test_init_writes_config_and_installs_hook(self, cli_env: dict) -> None:
        _init(cli_env)
        repo = cli_env["repo"]

        toml = (repo / ".db-git.toml").read_text()
        assert 'mode = "shared"' in toml
        assert 'strategy = "template"' in toml
        assert cli_env["db_url"] in toml

        hook = repo / ".git" / "hooks" / "post-checkout"
        assert hook.exists()
        assert "db-git-hook-v1" in hook.read_text()
        assert hook.stat().st_mode & 0o111

    def test_init_no_hook_flag_skips_hook(self, cli_env: dict) -> None:
        _init(cli_env, "--no-hook")
        assert not (cli_env["repo"] / ".git" / "hooks" / "post-checkout").exists()
        assert (cli_env["repo"] / ".db-git.toml").exists()

    def test_reinit_updates_existing_config(self, initialized: dict) -> None:
        run_db_git(
            "init",
            "--database-url",
            initialized["db_url"],
            "--mode",
            "shared",
            "--strategy",
            "template",
            "--on-active-connections",
            "fail",
            cwd=initialized["repo"],
            env=initialized["subprocess_env"],
        )
        toml = (initialized["repo"] / ".db-git.toml").read_text()
        assert 'on_active_connections = "fail"' in toml

    def test_init_fails_without_database_url(self, cli_env: dict) -> None:
        env = {
            k: v
            for k, v in cli_env["subprocess_env"].items()
            if k not in {"DATABASE_URL", "DB_GIT_DATABASE_URL"}
        }
        result = run_db_git(
            "init",
            "--mode",
            "shared",
            "--strategy",
            "template",
            cwd=cli_env["repo"],
            env=env,
            check=False,
        )
        assert result.returncode != 0

    def test_commands_require_init(self, cli_env: dict) -> None:
        for cmd in ("save", "restore", "list", "status", "prune"):
            result = run_db_git(
                cmd,
                cwd=cli_env["repo"],
                env=cli_env["subprocess_env"],
                check=False,
            )
            assert result.returncode != 0, f"{cmd} unexpectedly succeeded pre-init"

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
        result = run_db_git("status", cwd=initialized["repo"], env=env)
        assert result.returncode == 0

    def test_cli_flag_overrides_env(self, initialized: dict) -> None:
        env = {
            **initialized["subprocess_env"],
            "DATABASE_URL": "postgresql://nope:nope@127.0.0.1:1/nope",
        }
        result = run_db_git(
            "status",
            "--database-url",
            initialized["db_url"],
            cwd=initialized["repo"],
            env=env,
        )
        assert result.returncode == 0

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
        assert not (
            initialized["repo"] / ".git" / "hooks" / "post-checkout.legacy"
        ).exists()

    def test_install_preserves_legacy_and_remove_restores(self, cli_env: dict) -> None:
        hooks = cli_env["repo"] / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        legacy_content = "#!/bin/sh\nexit 0\n"
        existing = hooks / "post-checkout"
        existing.write_text(legacy_content)
        existing.chmod(0o755)

        _init(cli_env)
        preserved = hooks / "post-checkout.legacy"
        assert preserved.exists()
        assert preserved.read_text() == legacy_content

        run_db_git(
            "hook",
            "remove",
            cwd=cli_env["repo"],
            env=cli_env["subprocess_env"],
        )
        assert not preserved.exists()
        assert (hooks / "post-checkout").read_text() == legacy_content

    def test_hook_remove_fails_without_installed_hook(self, cli_env: dict) -> None:
        result = run_db_git(
            "hook",
            "remove",
            cwd=cli_env["repo"],
            env=cli_env["subprocess_env"],
            check=False,
        )
        assert result.returncode != 0

    # -----------------------------------------------------------------------
    # Hook-driven save/restore
    # -----------------------------------------------------------------------

    def test_branch_switch_preserves_data_per_branch(self, initialized: dict) -> None:
        url = initialized["db_url"]
        repo = initialized["repo"]
        env = initialized["subprocess_env"]
        default = get_default_branch(repo)

        seed_users(url)
        make_branch(initialized, "feature")

        # Mutate schema + data on feature
        conn = reconnect(url)
        try:
            conn.execute("ALTER TABLE users ADD COLUMN email varchar(100)")
            conn.execute(
                "INSERT INTO users (name, email) VALUES ('Dave', 'dave@x.com')"
            )
        finally:
            conn.close()

        # The hook fires for every branch switch below. No manual save/restore.
        run_git("checkout", default, cwd=repo, env=env)
        assert get_names(url) == ["Alice", "Bob", "Charlie"]
        assert "email" not in get_columns(url, "users")

        run_git("checkout", "feature", cwd=repo, env=env)
        assert "Dave" in get_names(url)
        assert "email" in get_columns(url, "users")

    def test_first_visit_with_no_snapshot_leaves_db_untouched(
        self, initialized: dict
    ) -> None:
        url = initialized["db_url"]
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(url)
        run_git("checkout", "-b", "brand-new", cwd=repo, env=env)
        assert get_names(url) == ["Alice", "Bob", "Charlie"]

    def test_manual_save_restore_without_hook_installed(self, cli_env: dict) -> None:
        _init(cli_env, "--no-hook")
        url = cli_env["db_url"]
        repo = cli_env["repo"]
        env = cli_env["subprocess_env"]
        default = get_default_branch(repo)

        seed_users(url)
        run_db_git("save", cwd=repo, env=env)
        assert {"master.meta.json", "main.meta.json"} & _meta_files(repo) or (
            f"{default}.meta.json" in _meta_files(repo)
        )

        make_branch(cli_env, "feature")
        conn = reconnect(url)
        try:
            conn.execute("INSERT INTO users (name) VALUES ('OnFeature')")
        finally:
            conn.close()
        run_db_git("save", "feature", cwd=repo, env=env)

        # Hook not installed: checkout does not restore. Data unchanged.
        run_git("checkout", default, cwd=repo, env=env)
        assert "OnFeature" in get_names(url)

        # Explicit restore rewinds to default's snapshot.
        run_db_git("restore", default, cwd=repo, env=env)
        assert get_names(url) == ["Alice", "Bob", "Charlie"]

    def test_empty_database_roundtrip(self, initialized: dict) -> None:
        url = initialized["db_url"]
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        run_db_git("save", cwd=repo, env=env)
        conn = reconnect(url)
        try:
            conn.execute("CREATE TABLE t (id int)")
        finally:
            conn.close()
        run_db_git("restore", cwd=repo, env=env)
        conn = reconnect(url)
        try:
            cur = conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = 't'"
            )
            assert cur.fetchone() is None
        finally:
            conn.close()

    def test_save_with_explicit_branch_label(self, initialized: dict) -> None:
        repo = initialized["repo"]
        env = initialized["subprocess_env"]
        seed_users(initialized["db_url"])
        run_db_git("save", "arbitrary-label", cwd=repo, env=env)
        assert "arbitrary_label.meta.json" in _meta_files(repo)

    # -----------------------------------------------------------------------
    # Disable / enable / DB_GIT_SKIP
    # -----------------------------------------------------------------------

    def test_disable_gates_the_hook_and_enable_restores(
        self, initialized: dict
    ) -> None:
        url = initialized["db_url"]
        repo = initialized["repo"]
        env = initialized["subprocess_env"]
        default = get_default_branch(repo)

        seed_users(url)
        run_db_git("save", cwd=repo, env=env)
        make_branch(initialized, "feature")
        run_git("checkout", default, cwd=repo, env=env)

        run_db_git("disable", cwd=repo, env=env)
        assert (repo / ".git" / "db-git" / "disabled").exists()

        # While disabled: mutate then switch. Hook must not save or restore.
        conn = reconnect(url)
        try:
            conn.execute("INSERT INTO users (name) VALUES ('WhileDisabled')")
        finally:
            conn.close()
        run_git("checkout", "feature", cwd=repo, env=env)
        assert "WhileDisabled" in get_names(url)

        run_db_git("enable", cwd=repo, env=env)
        assert not (repo / ".git" / "db-git" / "disabled").exists()

    def test_db_git_skip_env_bypasses_one_checkout(self, initialized: dict) -> None:
        url = initialized["db_url"]
        repo = initialized["repo"]
        env = initialized["subprocess_env"]
        default = get_default_branch(repo)

        seed_users(url)
        run_db_git("save", cwd=repo, env=env)
        make_branch(initialized, "feature")

        conn = reconnect(url)
        try:
            conn.execute("INSERT INTO users (name) VALUES ('SkipMe')")
        finally:
            conn.close()

        skip_env = {**env, "DB_GIT_SKIP": "1"}
        run_git("checkout", default, cwd=repo, env=skip_env)
        # Hook skipped at shell layer: no save, no restore. Data untouched.
        assert "SkipMe" in get_names(url)

    # -----------------------------------------------------------------------
    # Active-connections policy
    # -----------------------------------------------------------------------

    def test_terminate_policy_kills_blocking_connection(
        self, initialized: dict
    ) -> None:
        url = initialized["db_url"]
        seed_users(url)
        holder = reconnect(url)
        try:
            run_db_git(
                "save",
                cwd=initialized["repo"],
                env=initialized["subprocess_env"],
            )
        finally:
            with contextlib.suppress(Exception):
                holder.close()

    def test_fail_policy_refuses_on_active_connections(self, cli_env: dict) -> None:
        _init(cli_env, "--on-active-connections", "fail")
        url = cli_env["db_url"]
        seed_users(url)
        holder = reconnect(url)
        try:
            result = run_db_git(
                "save",
                cwd=cli_env["repo"],
                env=cli_env["subprocess_env"],
                check=False,
            )
            assert result.returncode != 0
        finally:
            holder.close()

    def test_fail_policy_refuses_restore_on_active_connections(
        self, cli_env: dict
    ) -> None:
        _init(cli_env, "--on-active-connections", "fail")
        url = cli_env["db_url"]
        seed_users(url)
        run_db_git("save", cwd=cli_env["repo"], env=cli_env["subprocess_env"])

        holder = reconnect(url)
        try:
            result = run_db_git(
                "restore",
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
        make_branch(initialized, "feature")
        run_git("checkout", get_default_branch(repo), cwd=repo, env=env)

        broken = {**env, "DATABASE_URL": "postgresql://x:x@127.0.0.1:1/x"}
        result = subprocess.run(
            ["git", "checkout", "feature"],
            cwd=repo,
            env=broken,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_hook_exits_zero_with_invalid_toml(self, initialized: dict) -> None:
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        make_branch(initialized, "feature")
        run_git("checkout", get_default_branch(repo), cwd=repo, env=env)

        (repo / ".db-git.toml").write_text("this is = not [[[ valid toml")

        result = subprocess.run(
            ["git", "checkout", "feature"],
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
        # feature was the current branch after its creation commit; checkout back
        # to default must succeed even though no config exists.
        assert result.returncode == 0, result.stderr

    # -----------------------------------------------------------------------
    # Edge cases the hook must skip
    # -----------------------------------------------------------------------

    def test_hook_skips_during_paused_rebase(
        self, initialized: dict, tmp_path: Path
    ) -> None:
        """
        Drive a real `git rebase -i` that pauses at 'edit' via GIT_SEQUENCE_EDITOR.
        Any internal checkouts git performs during rebase must not trigger
        db-git snapshot saves because `.git/rebase-merge/` exists.
        """
        url = initialized["db_url"]
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(url)
        run_db_git("save", cwd=repo, env=env)

        # Two extra commits so HEAD~2 is valid
        git_commit_file(repo, "a.txt", "a\n", env=env)
        git_commit_file(repo, "b.txt", "b\n", env=env)

        # Editor that rewrites the rebase-todo: first 'pick' -> 'edit' so rebase pauses.
        editor = tmp_path / "seq-editor.sh"
        editor.write_text("#!/bin/sh\nsed -i.bak '1s/^pick/edit/' \"$1\"\n")
        editor.chmod(0o755)

        rebase_env = {
            **env,
            "GIT_SEQUENCE_EDITOR": str(editor),
            "GIT_EDITOR": "true",
        }
        before = _meta_files(repo)

        # The rebase internally does checkout(s) that fire post-checkout. While
        # rebase-merge exists the hook must short-circuit and not save.
        run_git("rebase", "-i", "HEAD~2", cwd=repo, env=rebase_env)
        run_git("rebase", "--abort", cwd=repo, env=env, check=False)

        after = _meta_files(repo)
        # No unexpected per-commit snapshots should have accumulated
        assert after == before

    def test_hook_skips_on_detached_head(self, initialized: dict) -> None:
        url = initialized["db_url"]
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(url)
        run_db_git("save", cwd=repo, env=env)

        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        result = subprocess.run(
            ["git", "checkout", head_sha],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert get_names(url) == ["Alice", "Bob", "Charlie"]

    def test_hook_skips_on_file_only_checkout(self, initialized: dict) -> None:
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        run_db_git("save", cwd=repo, env=env)

        (repo / "already.txt").write_text("v1\n")
        run_git("add", "already.txt", cwd=repo, env=env)
        run_git("commit", "-m", "v1", cwd=repo, env=env)
        (repo / "already.txt").write_text("v2\n")

        before = _meta_files(repo)
        result = subprocess.run(
            ["git", "checkout", "--", "already.txt"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        after = _meta_files(repo)
        # Shell wrapper exits at $3==0; no hook-triggered save should occur
        assert after == before

    def test_hook_tolerates_fresh_clone_with_null_prev_head(
        self, initialized: dict, tmp_path: Path
    ) -> None:
        """
        On `git clone`, git fires post-checkout with prev-HEAD = 40 zeros. The
        hook must exit 0. We clone our just-initialized repo via a --template
        that installs our hook script in the fresh clone so post-checkout fires.
        """
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        # Commit .db-git.toml so the clone receives it and the hook has config
        (repo / ".gitignore").write_text(".db-git/\n.db-git.toml.swp\n")
        run_git("add", ".db-git.toml", ".gitignore", cwd=repo, env=env)
        run_git("commit", "-m", "add db-git config", cwd=repo, env=env)

        bare = tmp_path / "upstream.git"
        run_git("clone", "--bare", str(repo), str(bare), cwd=tmp_path, env=env)

        template = tmp_path / "gittemplate"
        (template / "hooks").mkdir(parents=True)
        hook_text = (repo / ".git" / "hooks" / "post-checkout").read_text()
        (template / "hooks" / "post-checkout").write_text(hook_text)
        (template / "hooks" / "post-checkout").chmod(0o755)

        fresh = tmp_path / "fresh"
        result = subprocess.run(
            ["git", "clone", f"--template={template}", str(bare), str(fresh)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert (fresh / ".db-git.toml").exists()

    # -----------------------------------------------------------------------
    # Branch name sanitization
    # -----------------------------------------------------------------------

    def test_branch_name_with_slash_is_sanitized(self, initialized: dict) -> None:
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        run_db_git("save", cwd=repo, env=env)

        run_git("checkout", "-b", "feature/auth", cwd=repo, env=env)
        git_commit_file(repo, "a.txt", "a\n", env=env)
        run_db_git("save", "feature/auth", cwd=repo, env=env)

        assert "feature__auth.meta.json" in _meta_files(repo)

    def test_long_branch_name_hash_falls_back(self, initialized: dict) -> None:
        repo = initialized["repo"]
        env = initialized["subprocess_env"]
        long_name = "x" * 100

        seed_users(initialized["db_url"])
        run_git("checkout", "-b", long_name, cwd=repo, env=env)
        git_commit_file(repo, "l.txt", "l\n", env=env)
        result = run_db_git("save", long_name, cwd=repo, env=env)
        assert result.returncode == 0

    # -----------------------------------------------------------------------
    # Prune
    # -----------------------------------------------------------------------

    def test_prune_dry_run_does_not_delete(self, initialized: dict) -> None:
        url = initialized["db_url"]
        repo = initialized["repo"]
        env = initialized["subprocess_env"]
        default = get_default_branch(repo)

        seed_users(url)
        run_db_git("save", cwd=repo, env=env)
        make_branch(initialized, "stale")
        run_db_git("save", "stale", cwd=repo, env=env)
        run_git("checkout", default, cwd=repo, env=env)
        run_git("branch", "-D", "stale", cwd=repo, env=env)

        before = _meta_files(repo)
        run_db_git("prune", "--dry-run", cwd=repo, env=env)
        assert _meta_files(repo) == before

    def test_prune_yes_drops_stale_snapshots(self, initialized: dict) -> None:
        url = initialized["db_url"]
        repo = initialized["repo"]
        env = initialized["subprocess_env"]
        default = get_default_branch(repo)

        seed_users(url)
        run_db_git("save", cwd=repo, env=env)
        make_branch(initialized, "stale")
        run_db_git("save", "stale", cwd=repo, env=env)
        run_git("checkout", default, cwd=repo, env=env)
        run_git("branch", "-D", "stale", cwd=repo, env=env)

        run_db_git("prune", "--yes", cwd=repo, env=env)
        assert not any("stale" in n for n in _meta_files(repo))

    def test_prune_refuses_non_tty_without_yes(self, initialized: dict) -> None:
        repo = initialized["repo"]
        env = initialized["subprocess_env"]
        default = get_default_branch(repo)

        seed_users(initialized["db_url"])
        run_db_git("save", cwd=repo, env=env)
        make_branch(initialized, "stale")
        run_db_git("save", "stale", cwd=repo, env=env)
        run_git("checkout", default, cwd=repo, env=env)
        run_git("branch", "-D", "stale", cwd=repo, env=env)

        result = run_db_git("prune", cwd=repo, env=env, check=False)
        assert result.returncode != 0

    def test_prune_respects_max_snapshots(self, initialized: dict) -> None:
        url = initialized["db_url"]
        repo = initialized["repo"]
        env = {**initialized["subprocess_env"], "DB_GIT_MAX_SNAPSHOTS": "1"}
        default = get_default_branch(repo)

        seed_users(url)
        run_db_git("save", cwd=repo, env=env)
        for name in ("a", "b", "c"):
            run_git("checkout", "-b", name, cwd=repo, env=env)
            git_commit_file(repo, f"{name}.txt", name, env=env)
            run_db_git("save", name, cwd=repo, env=env)
            run_git("checkout", default, cwd=repo, env=env)

        run_db_git("prune", "--yes", cwd=repo, env=env)
        assert len(_meta_files(repo)) <= 1

    # -----------------------------------------------------------------------
    # list / status
    # -----------------------------------------------------------------------

    def test_list_shows_saved_snapshots(self, initialized: dict) -> None:
        url = initialized["db_url"]
        repo = initialized["repo"]
        env = initialized["subprocess_env"]
        default = get_default_branch(repo)

        seed_users(url)
        run_db_git("save", cwd=repo, env=env)
        make_branch(initialized, "feature")
        run_db_git("save", "feature", cwd=repo, env=env)
        run_git("checkout", default, cwd=repo, env=env)

        result = run_db_git("list", cwd=repo, env=env)
        out = result.stdout + result.stderr
        assert default in out
        assert "feature" in out

    def test_status_shows_enabled_and_disabled_state(self, initialized: dict) -> None:
        repo = initialized["repo"]
        env = initialized["subprocess_env"]

        seed_users(initialized["db_url"])
        r1 = run_db_git("status", cwd=repo, env=env)
        out1 = r1.stdout + r1.stderr
        assert "template" in out1

        run_db_git("disable", cwd=repo, env=env)
        r2 = run_db_git("status", cwd=repo, env=env)
        out2 = r2.stdout + r2.stderr
        assert "no" in out2.lower()
