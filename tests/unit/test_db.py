from __future__ import annotations

from db_git.db import parse_database_url


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
