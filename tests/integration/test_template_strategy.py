from __future__ import annotations

import contextlib
from collections.abc import Callable

import psycopg
import pytest
from psycopg import sql

from git_db.backends.postgresql.backend import PostgresqlBackend
from git_db.backends.postgresql.template import TemplateStrategy
from git_db.config import GitDbConfig
from git_db.errors import ActiveConnectionsError, SnapshotError
from git_db.storage import has_snapshot, read_metadata, snapshot_db_name
from tests._pg_helpers import get_names, reconnect, seed_users


@pytest.mark.integration
class TestTemplateStrategy:
    def test_save_restore_round_trip(
        self,
        pg_info: dict,
        backend: PostgresqlBackend,
        template_strategy: TemplateStrategy,
        make_config: Callable[..., GitDbConfig],
    ) -> None:
        config = make_config(strategy="template")
        seed_users(config.database_url)

        template_strategy.save(
            config.database_url,
            "main",
            config.snapshot_dir,
            config,
        )

        meta = read_metadata(config.snapshot_dir, "main")
        assert meta is not None
        assert meta.strategy == "template"
        assert meta.file_size_bytes is None  # template has no dump file

        snapshot_name = snapshot_db_name(
            "main",
            pg_info["dbname"],
            backend.max_identifier_length,
        )
        assert backend.branch_db_manager(config).exists(snapshot_name)

        conn = reconnect(config.database_url)
        try:
            conn.execute("DROP TABLE users")
        finally:
            conn.close()

        template_strategy.restore(
            config.database_url,
            "main",
            config.snapshot_dir,
            config,
        )
        assert get_names(config.database_url) == ["Alice", "Bob", "Charlie"]

    def test_save_raises_under_fail_policy_with_active_connection(
        self,
        template_strategy: TemplateStrategy,
        make_config: Callable[..., GitDbConfig],
    ) -> None:
        config = make_config(strategy="template", policy="fail")
        seed_users(config.database_url)

        blocker = reconnect(config.database_url)
        try:
            with pytest.raises(ActiveConnectionsError):
                template_strategy.save(
                    config.database_url,
                    "main",
                    config.snapshot_dir,
                    config,
                )
        finally:
            if not blocker.closed:
                blocker.close()

        # Metadata must not have been written on failure.
        assert not has_snapshot(config.snapshot_dir, "main")

    def test_save_terminates_blocker_under_terminate_policy(
        self,
        pg_info: dict,
        backend: PostgresqlBackend,
        template_strategy: TemplateStrategy,
        make_config: Callable[..., GitDbConfig],
        maintenance_conn: psycopg.Connection,
    ) -> None:
        """
        terminate policy at the strategy level: an active connection to the
        source DB must be killed so CREATE DATABASE ... TEMPLATE can proceed.
        """
        config = make_config(strategy="template", policy="terminate")
        seed_users(config.database_url)
        blocker = reconnect(config.database_url)
        pid_row = blocker.execute("SELECT pg_backend_pid()").fetchone()
        assert pid_row is not None
        blocker_pid = pid_row[0]

        try:
            template_strategy.save(
                config.database_url,
                "main",
                config.snapshot_dir,
                config,
            )
        finally:
            if not blocker.closed:
                with contextlib.suppress(psycopg.Error):
                    blocker.close()

        # Snapshot DB created, metadata written.
        snapshot_name = snapshot_db_name(
            "main",
            pg_info["dbname"],
            backend.max_identifier_length,
        )
        assert backend.branch_db_manager(config).exists(snapshot_name)
        assert has_snapshot(config.snapshot_dir, "main")

        # Blocker was terminated - its PID no longer appears in pg_stat_activity.
        cur = maintenance_conn.execute(
            "SELECT 1 FROM pg_stat_activity WHERE pid = %s",
            (blocker_pid,),
        )
        assert cur.fetchone() is None

    def test_save_overwrites_existing_snapshot(
        self,
        template_strategy: TemplateStrategy,
        make_config: Callable[..., GitDbConfig],
    ) -> None:
        """
        A second save on the same branch replaces the first snapshot DB.
        """
        config = make_config(strategy="template")
        seed_users(config.database_url)

        template_strategy.save(
            config.database_url,
            "main",
            config.snapshot_dir,
            config,
        )

        conn = reconnect(config.database_url)
        try:
            conn.execute("INSERT INTO users (name) VALUES ('Dave')")
        finally:
            conn.close()

        template_strategy.save(
            config.database_url,
            "main",
            config.snapshot_dir,
            config,
        )

        # Mutate source again, then restore - must see the second save's data.
        conn = reconnect(config.database_url)
        try:
            conn.execute("DROP TABLE users")
        finally:
            conn.close()

        template_strategy.restore(
            config.database_url,
            "main",
            config.snapshot_dir,
            config,
        )
        assert get_names(config.database_url) == ["Alice", "Bob", "Charlie", "Dave"]

    def test_restore_raises_when_snapshot_db_dropped_out_of_band(
        self,
        pg_info: dict,
        backend: PostgresqlBackend,
        template_strategy: TemplateStrategy,
        make_config: Callable[..., GitDbConfig],
        maintenance_conn: psycopg.Connection,
    ) -> None:
        """
        If the snapshot DB was dropped behind git-db's back, restore must
        fail with SnapshotError (not a raw psycopg error) so callers can
        handle it.
        """
        config = make_config(strategy="template")
        seed_users(config.database_url)
        template_strategy.save(
            config.database_url,
            "main",
            config.snapshot_dir,
            config,
        )

        snapshot_name = snapshot_db_name(
            "main",
            pg_info["dbname"],
            backend.max_identifier_length,
        )
        maintenance_conn.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                sql.Identifier(snapshot_name)
            )
        )

        with pytest.raises(SnapshotError):
            template_strategy.restore(
                config.database_url,
                "main",
                config.snapshot_dir,
                config,
            )

    def test_cleanup_removes_metadata_when_snapshot_db_already_dropped(
        self,
        pg_info: dict,
        backend: PostgresqlBackend,
        template_strategy: TemplateStrategy,
        make_config: Callable[..., GitDbConfig],
        maintenance_conn: psycopg.Connection,
    ) -> None:
        """
        cleanup must be idempotent: if the snapshot DB was already dropped
        out-of-band, it should still delete the metadata file and not raise.
        """
        config = make_config(strategy="template")
        seed_users(config.database_url)

        template_strategy.save(
            config.database_url,
            "main",
            config.snapshot_dir,
            config,
        )
        assert has_snapshot(config.snapshot_dir, "main")

        # Drop the snapshot DB before cleanup runs.
        snapshot_name = snapshot_db_name(
            "main",
            pg_info["dbname"],
            backend.max_identifier_length,
        )
        maintenance_conn.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                sql.Identifier(snapshot_name)
            )
        )
        assert not backend.branch_db_manager(config).exists(snapshot_name)

        template_strategy.cleanup("main", config.snapshot_dir, config)

        assert not has_snapshot(config.snapshot_dir, "main")

    def test_cleanup_after_normal_save_drops_both_db_and_metadata(
        self,
        pg_info: dict,
        backend: PostgresqlBackend,
        template_strategy: TemplateStrategy,
        make_config: Callable[..., GitDbConfig],
    ) -> None:
        config = make_config(strategy="template")
        seed_users(config.database_url)

        template_strategy.save(
            config.database_url,
            "main",
            config.snapshot_dir,
            config,
        )
        template_strategy.cleanup("main", config.snapshot_dir, config)

        assert not has_snapshot(config.snapshot_dir, "main")
        snapshot_name = snapshot_db_name(
            "main",
            pg_info["dbname"],
            backend.max_identifier_length,
        )
        assert not backend.branch_db_manager(config).exists(snapshot_name)

    def test_cleanup_on_never_saved_branch_is_noop(
        self,
        template_strategy: TemplateStrategy,
        make_config: Callable[..., GitDbConfig],
    ) -> None:
        """
        cleanup on a branch that was never saved must not raise.
        """
        config = make_config(strategy="template")
        config.snapshot_dir.mkdir(parents=True, exist_ok=True)

        template_strategy.cleanup("never_saved", config.snapshot_dir, config)

        assert not has_snapshot(config.snapshot_dir, "never_saved")
