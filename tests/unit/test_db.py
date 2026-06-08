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
