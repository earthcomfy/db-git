from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from git_db.backends.postgresql.pgdump import PgDumpStrategy
from git_db.config import GitDbConfig
from git_db.errors import SnapshotError
from git_db.storage import has_snapshot, read_metadata
from tests._pg_helpers import get_names, reconnect, seed_users


@pytest.mark.integration
class TestPgDumpStrategy:
    def test_save_restore_round_trip(
        self,
        pg_info: dict,
        pgdump_strategy: PgDumpStrategy,
        make_config: Callable[..., GitDbConfig],
    ) -> None:
        config = make_config(strategy="pgdump")
        seed_users(config.database_url)

        pgdump_strategy.save(
            config.database_url,
            "main",
            config.snapshot_dir,
            config,
        )

        assert has_snapshot(config.snapshot_dir, "main")
        meta = read_metadata(config.snapshot_dir, "main")
        assert meta is not None
        assert meta.strategy == "pgdump"
        assert meta.database == pg_info["dbname"]
        assert meta.file_size_bytes is not None and meta.file_size_bytes > 0

        conn = reconnect(config.database_url)
        try:
            conn.execute("DROP TABLE users")
        finally:
            conn.close()

        pgdump_strategy.restore(
            config.database_url,
            "main",
            config.snapshot_dir,
            config,
        )
        assert get_names(config.database_url) == ["Alice", "Bob", "Charlie"]

    def test_restore_raises_when_dump_file_missing(
        self,
        pgdump_strategy: PgDumpStrategy,
        make_config: Callable[..., GitDbConfig],
    ) -> None:
        config = make_config(strategy="pgdump")
        config.snapshot_dir.mkdir(parents=True, exist_ok=True)

        with pytest.raises(SnapshotError, match="No dump file"):
            pgdump_strategy.restore(
                config.database_url,
                "ghost",
                config.snapshot_dir,
                config,
            )

    def test_cleanup_removes_dump_and_meta(
        self,
        pgdump_strategy: PgDumpStrategy,
        make_config: Callable[..., GitDbConfig],
    ) -> None:
        config = make_config(strategy="pgdump")
        seed_users(config.database_url)

        pgdump_strategy.save(
            config.database_url,
            "main",
            config.snapshot_dir,
            config,
        )
        assert has_snapshot(config.snapshot_dir, "main")

        pgdump_strategy.cleanup("main", config.snapshot_dir, config)
        assert not has_snapshot(config.snapshot_dir, "main")

    def test_save_creates_missing_snapshot_dir(
        self,
        pgdump_strategy: PgDumpStrategy,
        make_config: Callable[..., GitDbConfig],
        tmp_path: Path,
    ) -> None:
        """
        save() must mkdir(parents=True) for a snapshot_dir that doesn't exist.
        """
        deep = tmp_path / "does" / "not" / "exist" / "yet"
        config = make_config(strategy="pgdump", snapshot_dir=deep)
        seed_users(config.database_url)
        assert not deep.exists()

        pgdump_strategy.save(
            config.database_url,
            "main",
            config.snapshot_dir,
            config,
        )

        assert deep.is_dir()
        assert has_snapshot(config.snapshot_dir, "main")

    def test_save_overwrites_existing_snapshot(
        self,
        pgdump_strategy: PgDumpStrategy,
        make_config: Callable[..., GitDbConfig],
    ) -> None:
        """
        A second save() on the same branch replaces the first dump+meta.
        """
        config = make_config(strategy="pgdump")
        seed_users(config.database_url)

        pgdump_strategy.save(
            config.database_url,
            "main",
            config.snapshot_dir,
            config,
        )
        first_size = read_metadata(config.snapshot_dir, "main")
        assert first_size is not None

        # Mutate: add a row, then save again.
        conn = reconnect(config.database_url)
        try:
            conn.execute("INSERT INTO users (name) VALUES ('Dave')")
        finally:
            conn.close()

        pgdump_strategy.save(
            config.database_url,
            "main",
            config.snapshot_dir,
            config,
        )

        # Restore must bring back the *second* save's data.
        conn = reconnect(config.database_url)
        try:
            conn.execute("DROP TABLE users")
        finally:
            conn.close()
        pgdump_strategy.restore(
            config.database_url,
            "main",
            config.snapshot_dir,
            config,
        )
        assert get_names(config.database_url) == ["Alice", "Bob", "Charlie", "Dave"]

    def test_empty_database_round_trip(
        self,
        pgdump_strategy: PgDumpStrategy,
        make_config: Callable[..., GitDbConfig],
    ) -> None:
        """
        Save + restore on an empty DB should succeed and leave it empty.
        """
        config = make_config(strategy="pgdump")

        pgdump_strategy.save(
            config.database_url,
            "main",
            config.snapshot_dir,
            config,
        )

        # Create a junk table, then restore - it must get dropped.
        conn = reconnect(config.database_url)
        try:
            conn.execute("CREATE TABLE junk (id int)")
        finally:
            conn.close()

        pgdump_strategy.restore(
            config.database_url,
            "main",
            config.snapshot_dir,
            config,
        )
        conn = reconnect(config.database_url)
        try:
            cur = conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = 'junk'"
            )
            assert cur.fetchone() is None
        finally:
            conn.close()

    def test_save_round_trips_ddl_constraints_and_sequences(
        self,
        pgdump_strategy: PgDumpStrategy,
        make_config: Callable[..., GitDbConfig],
    ) -> None:
        """
        The dump must preserve non-trivial schema: primary key, unique
        constraint, check constraint, index, and a sequence's current value.
        This catches regressions in pg_dump flags (e.g. --schema-only,
        missing --no-owner, or format changes that strip constraints).
        """
        config = make_config(strategy="pgdump")
        conn = reconnect(config.database_url)
        try:
            conn.execute(
                """
                CREATE TABLE accounts (
                    id serial PRIMARY KEY,
                    email varchar(100) NOT NULL UNIQUE,
                    balance integer NOT NULL CHECK (balance >= 0)
                )
                """
            )
            conn.execute(
                "CREATE INDEX accounts_email_lower_idx ON accounts (lower(email))"
            )
            conn.execute(
                "INSERT INTO accounts (email, balance) VALUES "
                "('a@x.com', 100), ('b@x.com', 200)"
            )
        finally:
            conn.close()

        pgdump_strategy.save(
            config.database_url,
            "main",
            config.snapshot_dir,
            config,
        )

        # Drop everything, then restore.
        conn = reconnect(config.database_url)
        try:
            conn.execute("DROP TABLE accounts")
        finally:
            conn.close()

        pgdump_strategy.restore(
            config.database_url,
            "main",
            config.snapshot_dir,
            config,
        )

        conn = reconnect(config.database_url)
        try:
            # Check constraint must still reject negative balances.
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO accounts (email, balance) VALUES ('c@x.com', -1)"
                )
        finally:
            conn.close()

        conn = reconnect(config.database_url)
        try:
            # Unique constraint must still reject duplicates.
            with pytest.raises(psycopg.errors.UniqueViolation):
                conn.execute(
                    "INSERT INTO accounts (email, balance) VALUES ('a@x.com', 50)"
                )
        finally:
            conn.close()

        conn = reconnect(config.database_url)
        try:
            cur = conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'accounts' "
                "ORDER BY indexname"
            )
            indexes = [row[0] for row in cur.fetchall()]
            assert "accounts_email_lower_idx" in indexes
            assert "accounts_pkey" in indexes

            # Sequence state preserved: next id should be > the 2 already inserted.
            cur = conn.execute(
                "INSERT INTO accounts (email, balance) VALUES ('new@x.com', 0) "
                "RETURNING id"
            )
            row = cur.fetchone()
            assert row is not None and row[0] >= 3
        finally:
            conn.close()
