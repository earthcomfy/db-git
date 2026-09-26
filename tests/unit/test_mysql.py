from __future__ import annotations

import os
from pathlib import Path

import pytest

from db_git.backends import get_backend
from db_git.backends.mysql.connections import (
    client_options,
    server_version,
    subprocess_env,
)
from db_git.backends.mysql.operations import generation, validate_generation
from db_git.config import load_config
from db_git.db import parse_database_url, with_database_name
from db_git.errors import ConfigError, DbGitError


def test_mysql_url_preserves_credentials_options_and_database_replacement():
    url = (
        ""
        "mysql://us%40er:pa%3Ass%23word@[::1]:3307/my-app?ssl_mode=VERIFY_IDENTITY&ssl_ca=%2Ftmp%2FCA%20file.pem&connect_timeout=10"
    )
    params = parse_database_url(url)
    assert params["user"] == "us@er"
    assert params["password"] == "pa:ss#word"
    assert params["host"] == "::1"
    assert params["port"] == 3307
    assert params["ssl_ca"] == "/tmp/CA file.pem"
    rewritten = with_database_name(url, "branch")
    assert rewritten == url.replace("/my-app?", "/branch?")
    assert parse_database_url(rewritten)["dbname"] == "branch"
    assert get_backend(url).engine == "mysql"


@pytest.mark.parametrize(
    "url",
    [
        "mysql:///missing",
        "mysql://user@host",
        "mysql://user@host/mysql",
        "mysql://user:secret@host/app?unknown=secret",
        "mysql://user@host/app?ssl_mode=bad",
        "mysql://user@host/app?port=123",
        "mysql://user@host/app?connect_timeout=0",
        "mysql://user@host/app?ssl_mode=REQUIRED&ssl_mode=DISABLED",
        "mysql://user@host/app?ssl_mode=DISABLED&ssl_ca=/tmp/ca",
        "mysql://user@host/app#secret",
        "mysql://user:%xx@host/app",
        "mysql://user@host/app?ssl_cert=/tmp/cert",
        "mysql://user@host/has%2Fslash",
    ],
)
def test_mysql_rejects_ambiguous_urls_without_secrets(url):
    with pytest.raises(ConfigError) as error:
        parse_database_url(url)
    assert "secret" not in str(error.value)


def test_mysql_config_defaults_and_capabilities(tmp_path, monkeypatch):
    for key in os.environ:
        if key.startswith("DB_GIT_") or key == "DATABASE_URL":
            monkeypatch.delenv(key)
    config = load_config(
        {"database_url": "mysql://dev@localhost/app"}, project_root=tmp_path
    )
    assert (config.mode, config.strategy, config.on_active_connections) == (
        "per-branch",
        "mysqldump",
        "fail",
    )
    capabilities = get_backend(config.database_url).capabilities
    assert capabilities.modes == {"per-branch", "shared"}
    assert not capabilities.can_terminate_connections
    assert not capabilities.automatic_file_cleanup
    for override in (
        {"strategy": "template"},
        {"on_active_connections": "terminate"},
    ):
        with pytest.raises(ConfigError):
            load_config(
                {"database_url": config.database_url, **override}, project_root=tmp_path
            )


def test_generation_names_do_not_collide_after_sanitizing_or_case_folding():
    names = {
        generation("seed", branch)
        for branch in ["a/b", "a__b", "A__B", "x" * 500, "é", "retained"]
    }
    assert len(names) == 6
    for name in names:
        assert len(name) <= 64 and name == name.lower()
        validate_generation(name, "seed")
        with pytest.raises(DbGitError):
            validate_generation(name, "other_seed")


def test_option_file_private_escaped_and_removed(monkeypatch):
    monkeypatch.setenv("MYSQL_PWD", "inherited-secret")
    monkeypatch.setenv("MYSQL_TEST_LOGIN_FILE", "/tmp/inherited-login.cnf")
    params = {
        "host": "localhost",
        "port": 3306,
        "user": "dev",
        "password": 'secret"\\\n#comment',
        "ssl_mode": "REQUIRED",
    }
    with client_options(params) as args:
        assert params["password"] not in repr(args)
        path = Path(args[0].split("=", 1)[1])
        assert path.stat().st_mode & 0o777 == 0o600
        contents = path.read_text()
        assert "\\n#comment" in contents
        assert 'password="secret\\"\\\\\\n#comment"' in contents
        assert len(args) == 1
        assert subprocess_env()["MYSQL_TEST_LOGIN_FILE"] == os.devnull
        assert "MYSQL_PWD" not in subprocess_env()
    assert not path.exists()


@pytest.mark.parametrize(
    "version,comment",
    [
        ("10.11.6-MariaDB", "MariaDB"),
        ("8.0.32", "MariaDB"),
        ("5.7.44", "MySQL Community Server"),
        ("9.2.0", "MySQL Community Server"),
    ],
)
def test_server_version_explicitly_rejects_other_targets(version, comment):
    class FakeConnection:
        def execute(self, query):
            return self

        def fetchone(self):
            return version, comment

    with pytest.raises(ConfigError, match=r"8\.0 and 8\.4"):
        server_version(FakeConnection())


def test_object_rewrite_preserves_literals_comments_and_creation_security():
    from db_git.backends.mysql.objects import rewrite_definition

    sql = (
        "CREATE DEFINER=`old`@`host` PROCEDURE `p`() BEGIN SELECT "
        "'seed.users', `u`.name FROM `seed`.`users` AS `u`; /* "
        "seed.users */ END"
    )
    result = rewrite_definition(sql, "seed", "clone", "PROCEDURE", "")
    assert "DEFINER=CURRENT_USER" in result
    assert "FROM `clone`.`users` AS `u`" in result
    assert "'seed.users'" in result and "/* seed.users */" in result
    assert "`u`.name" in result


@pytest.mark.parametrize(
    "body",
    [
        "PREPARE stmt FROM 'SELECT * FROM seed.users'",
        "SELECT * FROM other.users",
        "SELECT other.func()",
        "SELECT * FROM seed.users AS other, other.users",
        "SELECT * FROM seed.users AS seed",
        "/*! SELECT * FROM seed.users */",
    ],
)
def test_unsafe_or_ambiguous_object_references_are_rejected(body):
    from db_git.backends.mysql.objects import rewrite_definition

    with pytest.raises(ConfigError):
        rewrite_definition(
            "CREATE DEFINER=`u`@`h` PROCEDURE p() " + body,
            "seed",
            "clone",
            "PROCEDURE",
            "",
        )


def test_event_rewrite_only_disables_schedule_not_words_inside_body():
    from db_git.backends.mysql.objects import rewrite_definition

    sql = (
        "CREATE DEFINER=`u`@`h` EVENT e ON SCHEDULE EVERY 1 DAY "
        "ENABLE DO INSERT INTO seed.log VALUES ('ENABLE DISABLE "
        "seed.log')"
    )
    rewritten = rewrite_definition(sql, "seed", "clone", "EVENT", "")
    assert "1 DAY DISABLE DO" in rewritten
    assert "INSERT INTO `clone`.log" in rewritten
    assert "'ENABLE DISABLE seed.log'" in rewritten


def test_ansi_quotes_and_backslash_modes_are_preserved():
    from db_git.backends.mysql.objects import rewrite_definition

    sql = (
        "CREATE DEFINER=`u`@`h` PROCEDURE p() SELECT 'a\\', \"seed"
        '"."users".name FROM "seed"."users"'
    )
    result = rewrite_definition(
        sql, "seed", "clone", "PROCEDURE", "ANSI_QUOTES,NO_BACKSLASH_ESCAPES"
    )
    assert "'a\\'" in result
    assert 'FROM `clone`."users"' in result


@pytest.mark.parametrize(
    "value", ["1.25", ".25", "1.", "1.25e-2", "1e+2", "0xff", "0b101"]
)
def test_numeric_literals_are_not_schema_references(value):
    from db_git.backends.mysql.objects import rewrite_definition

    sql = f"CREATE FUNCTION f() RETURNS DOUBLE DETERMINISTIC RETURN {value}"
    assert rewrite_definition(sql, "seed", "clone", "FUNCTION", "") == sql


@pytest.mark.parametrize(
    "body",
    [
        "SELECT 1 AS other; TRUNCATE TABLE other.t",
        "SELECT id FROM seed.users AS other; TRUNCATE TABLE other.t",
        "SELECT id FROM seed.users AS other; SELECT other.id",
        "SELECT 1 AS other; CREATE INDEX i ON other.t(id)",
        "CREATE INDEX i ON other.t(id)",
        "CREATE TABLE other.t AS SELECT id FROM seed.users AS other",
        "SELECT 1 AS other; CALL other.p()",
        "SELECT other.users.id FROM seed.users AS other",
        "DELETE users FROM users USING other.users",
        "SELECT 1 AS other; RENAME TABLE users TO other.t",
        "UPDATE IGNORE other.t SET id=1",
        "UPDATE LOW_PRIORITY other.t SET id=1",
        "INSERT other.t SELECT id FROM seed.users AS other",
        "INSERT IGNORE other.t SELECT id FROM seed.users AS other",
        "REPLACE other.t SELECT id FROM seed.users AS other",
        "UPDATE seed.users AS other, other.t SET other.id=1",
        "SELECT * FROM seed.users AS other JOIN seed.users AS u "
        "ON other.id=u.id, other.t",
    ],
)
def test_aliases_never_authorize_external_schema_objects(body):
    from db_git.backends.mysql.objects import rewrite_definition

    with pytest.raises(ConfigError, match="qualified references"):
        rewrite_definition(
            f"CREATE PROCEDURE p() BEGIN {body}; END", "seed", "clone", "PROCEDURE", ""
        )


def test_local_ddl_calls_and_table_aliases_remain_supported():
    from db_git.backends.mysql.objects import rewrite_definition

    sql = (
        "CREATE PROCEDURE p() BEGIN TRUNCATE TABLE seed.t; CALL seed.p2(); "
        "SELECT u.id FROM seed.users AS u ORDER BY u.id DESC; END"
    )
    rewritten = rewrite_definition(sql, "seed", "clone", "PROCEDURE", "")
    assert "TRUNCATE TABLE `clone`.t" in rewritten
    assert "CALL `clone`.p2()" in rewritten
    assert "FROM `clone`.users AS u ORDER BY u.id DESC" in rewritten


def test_lowercase_trigger_row_aliases_are_supported():
    from db_git.backends.mysql.objects import rewrite_definition

    sql = "CREATE TRIGGER t BEFORE INSERT ON seed.users FOR EACH ROW SET new.name='x'"
    assert "SET new.name='x'" in rewrite_definition(sql, "seed", "clone", "TRIGGER", "")


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM (other.t JOIN seed.users AS other ON 1=1)",
        "SELECT u.id FROM (SELECT id FROM seed.users) AS u",
    ],
)
def test_ambiguous_parenthesized_table_references_fail_closed(query):
    from db_git.backends.mysql.objects import rewrite_definition

    with pytest.raises(ConfigError, match="Parenthesized table references"):
        rewrite_definition(
            "CREATE PROCEDURE p() " + query, "seed", "clone", "PROCEDURE", ""
        )


def test_join_expression_commas_do_not_become_table_references():
    from db_git.backends.mysql.objects import rewrite_definition

    sql = (
        "CREATE PROCEDURE p() SELECT CONCAT(u.name, v.name) FROM seed.users AS u "
        "JOIN seed.users AS v ON CONCAT(u.name, v.name)='x', seed.users AS w "
        "WHERE u.id=w.id"
    )
    rewritten = rewrite_definition(sql, "seed", "clone", "PROCEDURE", "")
    assert rewritten.count("`clone`.users") == 3
    assert "CONCAT(u.name, v.name)" in rewritten
