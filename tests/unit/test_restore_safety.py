import subprocess
from unittest.mock import Mock

import pytest

from db_git.backends.postgresql.branch_db import _create_via_pgdump
from db_git.backends.postgresql.pgdump import _restore_dump
from db_git.errors import SnapshotError

PARAMS = {"host": "localhost", "port": 5432, "user": "postgres", "dbname": "app"}


@pytest.mark.parametrize("stderr", ["pg_restore: error: invalid archive", "", "ERROR"])
def test_shared_restore_rejects_every_nonzero_exit(monkeypatch, stderr):
    import db_git.backends.postgresql.pgdump as pgdump

    monkeypatch.setattr(
        pgdump.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 1, "", stderr)),
    )
    with pytest.raises(SnapshotError, match="pg_restore failed"):
        _restore_dump("pg_restore", PARAMS, "backup.dump", {})


@pytest.mark.parametrize("stderr", ["pg_restore: error: invalid archive", "", "ERROR"])
def test_branch_clone_rejects_every_nonzero_restore_exit(monkeypatch, stderr):
    import db_git.backends.postgresql.branch_db as branch_db

    monkeypatch.setattr(branch_db.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        branch_db.subprocess,
        "run",
        Mock(
            side_effect=[
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 1, "", stderr),
            ]
        ),
    )
    with pytest.raises(SnapshotError, match="pg_restore failed"):
        _create_via_pgdump(Mock(), Mock(), PARAMS, "stage", "source")
