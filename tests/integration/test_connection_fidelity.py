from __future__ import annotations

import shutil
import subprocess
from urllib.parse import quote, urlencode

import pytest

from db_git.backends.postgresql.pgdump import _build_pg_dump_cmd, _restore_dump
from db_git.db import parse_database_url
from db_git.errors import DatabaseError, SnapshotError
from tests._pg_helpers import seed_users

pytestmark = pytest.mark.integration


def test_maintenance_preserves_session_options(backend, make_config):
    config = make_config()
    config.database_url += "?" + urlencode(
        {
            "application_name": "db-git fidelity",
            "options": "-c search_path=pg_catalog",
            "connect_timeout": "3",
        },
        quote_via=quote,
    )
    params = backend.apply_url_defaults(parse_database_url(config.database_url))
    conn = backend.connect_maintenance(params)
    try:
        assert conn.execute("SHOW application_name").fetchone() == ("db-git fidelity",)
        assert conn.execute("SHOW search_path").fetchone() == ("pg_catalog",)
        assert conn.execute("SELECT current_database()").fetchone() == ("postgres",)
    finally:
        conn.close()


def test_client_restore_preserves_read_only_option(backend, make_config, tmp_path):
    config = make_config(strategy="pgdump")
    seed_users(config.database_url)
    params = backend.apply_url_defaults(parse_database_url(config.database_url))
    dump = str(tmp_path / "probe.dump")
    pg_dump, pg_restore = shutil.which("pg_dump"), shutil.which("pg_restore")
    assert pg_dump and pg_restore
    result = subprocess.run(
        _build_pg_dump_cmd(pg_dump, params, dump, 14),
        env=backend.build_subprocess_env(params),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    # If options were lost, restore would reach an unrelated duplicate-table error.
    with pytest.raises(SnapshotError, match="read-only transaction"):
        _restore_dump(
            pg_restore,
            {**params, "options": "-c default_transaction_read_only=on"},
            dump,
            backend.build_subprocess_env(params),
        )


def test_required_ssl_is_not_silently_dropped(backend, make_config, tmp_path):
    config = make_config(strategy="pgdump")
    params = backend.apply_url_defaults(
        parse_database_url(config.database_url + "?sslmode=require")
    )
    # The disposable PostgreSQL fixture has no TLS configured.
    with pytest.raises(DatabaseError):
        backend.connect_maintenance(params)
    pg_dump = shutil.which("pg_dump")
    assert pg_dump
    result = subprocess.run(
        _build_pg_dump_cmd(pg_dump, params, str(tmp_path / "dump"), 14),
        env=backend.build_subprocess_env(params),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "SSL" in result.stderr


def test_service_file_connection_and_dump(backend, pg_info, tmp_path, monkeypatch):
    service = tmp_path / "pg_service.conf"
    service.write_text(
        "[project]\n"
        + "\n".join(f"{key}={value}" for key, value in pg_info.items())
        + "\napplication_name=from_service\n"
    )
    monkeypatch.setenv("PGSERVICEFILE", str(service))
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.setenv("PGUSER", "invalid_user")
    params = backend.apply_url_defaults(
        parse_database_url(f"postgresql:///{pg_info['dbname']}?service=project")
    )
    conn = backend.connect_maintenance(params)
    try:
        assert conn.execute("SHOW application_name").fetchone() == ("from_service",)
        assert conn.execute("SELECT current_database()").fetchone() == ("postgres",)
    finally:
        conn.close()
    pg_dump = shutil.which("pg_dump")
    assert pg_dump
    result = subprocess.run(
        _build_pg_dump_cmd(pg_dump, params, str(tmp_path / "dump"), 14),
        env=backend.build_subprocess_env(params),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
