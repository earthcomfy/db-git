from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from db_git.backends.postgresql.backend import PostgresqlBackend
from db_git.backends.postgresql.branch_db import PostgresBranchDbManager
from db_git.config import DbGitConfig
from db_git.errors import ActiveConnectionsError
from db_git.state import load_state, record_branch_db
from tests._pg_helpers import build_url, get_names, reconnect, seed_users


@pytest.mark.integration
class TestPostgresBranchDbManager:
    def test_exists_true_and_false(
        self,
        pg_info: dict,
        template_manager: PostgresBranchDbManager,
    ) -> None:
        assert template_manager.exists(pg_info["dbname"]) is True
        assert template_manager.exists("no_such_db_ever") is False

    def test_create_via_template(
        self,
        pg_info: dict,
        git_dir: Path,
        template_manager: PostgresBranchDbManager,
    ) -> None:
        seed_users(build_url(pg_info))
        target = f"{pg_info['dbname']}__feat"

        template_manager.create(
            target, pg_info["dbname"], "feat", pg_info["dbname"], git_dir
        )

        assert template_manager.exists(target)
        cloned_url = build_url({**pg_info, "dbname": target})
        assert get_names(cloned_url) == ["Alice", "Bob", "Charlie"]

        state = load_state(git_dir)
        assert "feat" in state.databases
        assert state.databases["feat"].db_name == target

    def test_drop_via_template(
        self,
        pg_info: dict,
        git_dir: Path,
        template_manager: PostgresBranchDbManager,
    ) -> None:
        seed_users(build_url(pg_info))
        target = f"{pg_info['dbname']}__drop_me"

        template_manager.create(
            target, pg_info["dbname"], "drop_me", pg_info["dbname"], git_dir
        )
        assert template_manager.exists(target)

        template_manager.drop(target, "drop_me", git_dir)

        assert not template_manager.exists(target)
        state = load_state(git_dir)
        assert "drop_me" not in state.databases

    def test_create_via_pgdump_round_trips_data(
        self,
        pg_info: dict,
        git_dir: Path,
        pgdump_manager: PostgresBranchDbManager,
    ) -> None:
        seed_users(build_url(pg_info))
        target = f"{pg_info['dbname']}__feat_dump"

        pgdump_manager.create(
            target, pg_info["dbname"], "feat_dump", pg_info["dbname"], git_dir
        )

        cloned_url = build_url({**pg_info, "dbname": target})
        assert get_names(cloned_url) == ["Alice", "Bob", "Charlie"]

    def test_create_replaces_existing_target(
        self,
        pg_info: dict,
        git_dir: Path,
        template_manager: PostgresBranchDbManager,
    ) -> None:
        """
        If the target DB already exists, create should drop+recreate it.
        """
        source_url = build_url(pg_info)
        seed_users(source_url)
        target = f"{pg_info['dbname']}__collide"

        template_manager.create(
            target, pg_info["dbname"], "collide", pg_info["dbname"], git_dir
        )
        source_conn = reconnect(source_url)
        try:
            source_conn.execute("INSERT INTO users (name) VALUES ('Dave')")
        finally:
            source_conn.close()

        template_manager.create(
            target, pg_info["dbname"], "collide", pg_info["dbname"], git_dir
        )

        cloned_url = build_url({**pg_info, "dbname": target})
        assert get_names(cloned_url) == ["Alice", "Bob", "Charlie", "Dave"]

    def test_drop_is_idempotent_when_db_missing(
        self,
        pg_info: dict,
        git_dir: Path,
        template_manager: PostgresBranchDbManager,
    ) -> None:
        """
        State entry should still get cleaned even if the PG DB is gone.
        """
        ghost = f"{pg_info['dbname']}__ghost"

        record_branch_db(git_dir, "ghost", ghost, pg_info["dbname"])
        assert not template_manager.exists(ghost)

        template_manager.drop(ghost, "ghost", git_dir)

        state = load_state(git_dir)
        assert "ghost" not in state.databases

    def test_drop_fail_policy_refuses_when_blocker_exists(
        self,
        pg_info: dict,
        git_dir: Path,
        backend: PostgresqlBackend,
        make_config: Callable[..., DbGitConfig],
    ) -> None:
        """
        fail policy: an active connection to the branch DB blocks drop.
        """
        config = make_config(strategy="template", mode="per-branch", policy="fail")
        manager = PostgresBranchDbManager(backend=backend, config=config)
        seed_users(config.database_url)
        target = f"{pg_info['dbname']}__blocked_drop"

        manager.create(
            target, pg_info["dbname"], "blocked_drop", pg_info["dbname"], git_dir
        )

        blocker = reconnect(build_url({**pg_info, "dbname": target}))
        try:
            with pytest.raises(ActiveConnectionsError):
                manager.drop(target, "blocked_drop", git_dir)
        finally:
            if not blocker.closed:
                blocker.close()

        assert manager.exists(target)
        assert "blocked_drop" in load_state(git_dir).databases

    def test_list_reflects_pg_state(
        self,
        pg_info: dict,
        git_dir: Path,
        template_manager: PostgresBranchDbManager,
    ) -> None:
        seed_users(build_url(pg_info))
        target = f"{pg_info['dbname']}__listme"

        template_manager.create(
            target, pg_info["dbname"], "listme", pg_info["dbname"], git_dir
        )

        entries = template_manager.list(git_dir)
        names = {branch: (entry.db_name, exists) for branch, entry, exists in entries}
        assert names["listme"] == (target, True)

    def test_list_shows_dropped_db_as_missing(
        self,
        pg_info: dict,
        git_dir: Path,
        template_manager: PostgresBranchDbManager,
        maintenance_conn: psycopg.Connection,
    ) -> None:
        """
        State says the DB exists, but PG disagrees - exists=False.
        """
        seed_users(build_url(pg_info))
        target = f"{pg_info['dbname']}__vanish"

        template_manager.create(
            target, pg_info["dbname"], "vanish", pg_info["dbname"], git_dir
        )

        maintenance_conn.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(target))
        )

        entries = template_manager.list(git_dir)
        vanish = next(e for b, e, _ in entries if b == "vanish")
        exists = next(ex for b, _, ex in entries if b == "vanish")
        assert vanish.db_name == target
        assert exists is False
