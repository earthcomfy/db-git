from __future__ import annotations

from db_git.db import parse_database_url, with_database_name


class TestParseDatabaseUrl:
    def test_standard_url(self):
        result = parse_database_url("postgresql://user:pass@host:5433/mydb")
        assert result == {
            "user": "user",
            "password": "pass",
            "host": "host",
            "port": 5433,
            "dbname": "mydb",
        }

    def test_missing_fields_return_none(self):
        result = parse_database_url("postgresql:///mydb")
        assert result["user"] is None
        assert result["password"] is None
        assert result["host"] is None
        assert result["port"] is None
        assert result["dbname"] == "mydb"


class TestWithDatabaseName:
    def test_swaps_name_preserving_credentials(self):
        result = with_database_name(
            "postgresql://user:pass@host:5433/myapp", "myapp__feature__auth"
        )
        assert result == "postgresql://user:pass@host:5433/myapp__feature__auth"

    def test_preserves_query_string(self):
        result = with_database_name(
            "postgresql://user:pass@host:5432/myapp?sslmode=require", "myapp__wip"
        )
        assert result == "postgresql://user:pass@host:5432/myapp__wip?sslmode=require"

    def test_bare_url_without_credentials(self):
        result = with_database_name("postgresql://localhost/myapp", "myapp__wip")
        assert result == "postgresql://localhost/myapp__wip"


def test_libpq_options_and_percent_encoding():
    params = parse_database_url(
        "postgresql://a%20user:p%40ss@[::1]:5433/a%2Fb%20db"
        "?sslmode=verify-full&sslrootcert=%2Ftmp%2Fca.pem"
        "&application_name=db-git&connect_timeout=7&options=-c%20search_path%3Dpublic"
    )
    assert params["user"] == "a user"
    assert params["password"] == "p@ss"
    assert params["host"] == "::1"
    assert params["dbname"] == "a/b db"
    assert params["sslmode"] == "verify-full"
    assert params["options"] == "-c search_path=public"
    assert params["connect_timeout"] == "7"
    assert params["sslrootcert"] == "/tmp/ca.pem"


def test_socket_multihost_and_query_precedence():
    assert parse_database_url("postgresql:///db?host=%2Ftmp")["host"] == "/tmp"
    params = parse_database_url("postgresql://host1:5432,host2:5433/db?user=other")
    assert params["host"] == "host1,host2"
    assert params["port"] == "5432,5433"
    assert params["user"] == "other"


def test_rewrite_replaces_query_database_and_escapes_name():
    url = "postgresql://host/ignored?dbname=actual&sslmode=require&password=a%2Bb"
    rewritten = with_database_name(url, "branch/db ?#ü")
    assert parse_database_url(rewritten)["dbname"] == "branch/db ?#ü"
    assert parse_database_url(rewritten)["password"] == "a+b"
    assert parse_database_url(rewritten)["sslmode"] == "require"
    assert "/branch%2Fdb%20%3F%23%C3%BC?" in rewritten


def test_invalid_url_errors_do_not_include_credentials():
    import pytest

    from db_git.errors import ConfigError

    with pytest.raises(ConfigError) as error:
        parse_database_url("postgresql://user:secret@host/db?unknown=other-secret")
    assert "secret" not in str(error.value)


def test_maintenance_quotes_values_and_preserves_options(monkeypatch):
    from unittest.mock import Mock

    from psycopg.conninfo import conninfo_to_dict

    from db_git.backends.postgresql.backend import PostgresqlBackend

    connect = Mock()
    monkeypatch.setattr("db_git.backends.postgresql.backend.psycopg.connect", connect)
    backend = PostgresqlBackend()
    params = backend.apply_url_defaults(
        parse_database_url(
            "postgresql://a%20user:p%27ass@localhost/app?sslmode=require&options=-c%20search_path%3Dpublic"
        )
    )
    backend.connect_maintenance(params)
    actual = conninfo_to_dict(connect.call_args.args[0])
    assert actual == {**{k: str(v) for k, v in params.items()}, "dbname": "postgres"}


def test_service_defaults_do_not_override_service_settings(monkeypatch):
    from db_git.backends.postgresql.backend import PostgresqlBackend

    monkeypatch.setenv("PGHOST", "wrong-host")
    monkeypatch.setenv("PGUSER", "wrong-user")
    params = PostgresqlBackend().apply_url_defaults(
        parse_database_url("postgresql:///app?service=project")
    )
    assert not ({"host", "port", "user"} & params.keys())
    assert params["service"] == "project"


def test_client_connections_keep_options_and_password_out_of_argv(monkeypatch):
    from psycopg.conninfo import conninfo_to_dict

    from db_git.backends.postgresql.backend import PostgresqlBackend
    from db_git.backends.postgresql.pgdump import (
        _build_pg_dump_cmd,
        _build_pg_restore_cmd,
    )

    monkeypatch.setenv("PGPASSWORD", "ambient-password")
    backend = PostgresqlBackend()
    params = backend.apply_url_defaults(
        parse_database_url(
            "postgresql://user:url-secret@host/db?sslmode=require&application_name=db-git"
        )
    )
    for command in (
        _build_pg_dump_cmd("pg_dump", params, "dump", 14),
        _build_pg_restore_cmd("pg_restore", params, "dump"),
    ):
        dsn = conninfo_to_dict(command[command.index("-d") + 1])
        assert dsn["sslmode"] == "require"
        assert dsn["application_name"] == "db-git"
        assert dsn["dbname"] == "db"
        assert "url-secret" not in str(command)
    assert backend.build_subprocess_env(params)["PGPASSWORD"] == "url-secret"


def test_mask_url_handles_ipv6_and_query_passwords():
    from db_git.cli._format import mask_url

    masked = mask_url(
        "postgresql://user:secret@[::1]:5433/app?password=query-secret&sslpassword=key-secret"
    )
    assert "secret" not in masked
    assert "[::1]:5433" in masked
    assert "password=****" in masked


def test_rewrite_keeps_literal_plus_and_encoded_values():
    original = (
        "postgresql://host/db?password=a+b&dbname=old&options=-c%20search_path%3Dpublic"
    )
    new = with_database_name(original, "new db")
    assert parse_database_url(new)["password"] == "a+b"
    assert "options=-c%20search_path%3Dpublic" in new
    assert parse_database_url(new)["dbname"] == "new db"


def test_client_secret_options_fail_explicitly_instead_of_exposing_or_ignoring():
    import pytest

    from db_git.backends.postgresql.connections import client_dsn
    from db_git.errors import ConfigError

    for params in (
        {"sslpassword": "secret"},
        {"service": "project", "password": "secret"},
    ):
        with pytest.raises(ConfigError) as error:
            client_dsn({"dbname": "app", **params})
        assert "secret" not in str(error.value)
        assert "service" in str(error.value)


def test_branch_clone_passes_options_to_source_and_staging_database(monkeypatch):
    import subprocess
    from unittest.mock import Mock

    from psycopg.conninfo import conninfo_to_dict

    from db_git.backends.postgresql.backend import PostgresqlBackend
    from db_git.backends.postgresql.branch_db import _create_via_pgdump

    monkeypatch.setattr(
        "db_git.backends.postgresql.branch_db.shutil.which", lambda name: name
    )
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr("db_git.backends.postgresql.branch_db.subprocess.run", run)
    params = PostgresqlBackend().apply_url_defaults(
        parse_database_url(
            "postgresql://user:secret@host/base?sslmode=require&application_name=clone"
        )
    )
    _create_via_pgdump(Mock(), PostgresqlBackend(), params, "stage", "source")
    for call, database in zip(run.call_args_list, ["source", "stage"], strict=True):
        command = call.args[0]
        dsn = conninfo_to_dict(command[command.index("-d") + 1])
        assert dsn["dbname"] == database
        assert dsn["sslmode"] == "require"
        assert dsn["application_name"] == "clone"
        assert "secret" not in str(command)
        assert call.kwargs["env"]["PGPASSWORD"] == "secret"


def test_rewrite_preserves_empty_authority_for_socket_and_service_urls():
    for original in ("postgresql:///seed", "postgresql:///seed?service=project"):
        rewritten = with_database_name(original, "branch")
        assert rewritten.startswith("postgresql:///branch")
        assert parse_database_url(rewritten)["dbname"] == "branch"


def test_application_connection_defaults_match_management_connection():
    from db_git.backends.postgresql.backend import PostgresqlBackend
    from db_git.db import with_connection_defaults

    original = "postgresql:///app?password=a+b&sslmode=require"
    params = PostgresqlBackend().apply_url_defaults(parse_database_url(original))
    resolved = with_connection_defaults(original, params)
    assert parse_database_url(resolved) == {**parse_database_url(original), **params}
    assert "password=a+b&sslmode=require" in resolved


def test_service_url_keeps_service_defaults():
    from db_git.backends.postgresql.backend import PostgresqlBackend
    from db_git.db import with_connection_defaults

    original = "postgresql:///app?service=project"
    params = PostgresqlBackend().apply_url_defaults(parse_database_url(original))
    assert with_connection_defaults(original, params) == original
