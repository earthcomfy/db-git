import subprocess
from unittest.mock import Mock

import pytest

from db_git.backends.postgresql.backend import PostgresqlBackend
from db_git.backends.postgresql.branch_db import PostgresBranchDbManager
from db_git.backends.postgresql.pgdump import PgDumpStrategy
from db_git.config import DbGitConfig
from db_git.errors import SnapshotError
from db_git.state import load_state
from db_git.storage import snapshot_dump_path


@pytest.mark.parametrize("stderr", ["pg_restore: error: invalid archive", "", "ERROR"])
def test_shared_restore_rejects_every_nonzero_exit(monkeypatch, tmp_path, stderr):
    import db_git.backends.postgresql.pgdump as pgdump

    strategy = PgDumpStrategy(PostgresqlBackend(), 14)
    monkeypatch.setattr(pgdump.shutil, "which", lambda name: name)
    monkeypatch.setattr(strategy, "_drop_and_create_db", Mock())
    monkeypatch.setattr(
        pgdump.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 1, "", stderr)),
    )
    snapshot_dump_path(tmp_path, "main").write_bytes(b"invalid archive")
    with pytest.raises(SnapshotError, match="pg_restore failed"):
        strategy.restore("postgresql://localhost/app", "main", tmp_path, DbGitConfig())


@pytest.mark.parametrize("stderr", ["pg_restore: error: invalid archive", "", "ERROR"])
def test_failed_branch_restore_is_not_recorded(monkeypatch, tmp_path, stderr):
    import db_git.backends.postgresql.branch_db as branch_db

    backend = Mock()
    backend.apply_url_defaults.return_value = {
        "host": "localhost",
        "port": 5432,
        "user": "postgres",
        "dbname": "app",
    }
    backend.detect_strategy.return_value.name = "pgdump"
    process = Mock(returncode=0)
    monkeypatch.setattr(branch_db.shutil, "which", lambda name: name)
    monkeypatch.setattr(branch_db, "handle_active_connections", Mock())
    monkeypatch.setattr(branch_db.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(
        branch_db.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 1, "", stderr)),
    )
    manager = PostgresBranchDbManager(backend, DbGitConfig())
    with pytest.raises(SnapshotError, match="pg_restore failed"):
        manager.create("app_feature", "app", "feature", "main", tmp_path)
    assert load_state(tmp_path).databases == {}
