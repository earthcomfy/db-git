import pytest
from psycopg import sql

from db_git.errors import SnapshotError
from db_git.storage import snapshot_db_name, snapshot_dump_path

pytestmark = pytest.mark.integration


def test_template_save_preserves_untracked_database(
    template_strategy, make_config, maintenance_conn, pg_info
):
    config = make_config(strategy="template")
    name = snapshot_db_name("feature", pg_info["dbname"])
    maintenance_conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    with pytest.raises(SnapshotError, match="untracked snapshot database"):
        template_strategy.save(
            config.database_url, "feature", config.snapshot_dir, config
        )
    assert (
        maintenance_conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
        is not None
    )


def test_pgdump_save_preserves_untracked_file(pgdump_strategy, make_config):
    config = make_config(strategy="pgdump")
    config.snapshot_dir.mkdir(parents=True)
    dump = snapshot_dump_path(config.snapshot_dir, "feature")
    dump.write_bytes(b"untracked backup")
    with pytest.raises(SnapshotError, match="untracked dump file"):
        pgdump_strategy.save(
            config.database_url, "feature", config.snapshot_dir, config
        )
    assert dump.read_bytes() == b"untracked backup"
