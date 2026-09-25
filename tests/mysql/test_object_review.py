from __future__ import annotations

from decimal import Decimal

import pytest
from tests.mysql.test_mysql import command, selected

from db_git.backends.mysql.connections import identifier
from db_git.backends.mysql.objects import capture_objects
from db_git.errors import ConfigError


def test_decimal_routine_and_view_clone_with_table_aliases(project):
    seed = identifier(project[1]["dbname"])
    conn = project[4]
    conn.execute(
        f"CREATE FUNCTION {seed}.fraction() RETURNS DECIMAL(5,2) "
        "DETERMINISTIC RETURN 1.25"
    )
    conn.execute(
        f"CREATE VIEW {seed}.amounts AS "
        f"SELECT u.id, 1.25 AS amount FROM {seed}.users AS u"
    )
    conn.execute(
        f"CREATE PROCEDURE {seed}.clear_local() BEGIN TRUNCATE TABLE {seed}.users; END"
    )
    conn.execute(
        f"CREATE TRIGGER {seed}.suffix_name BEFORE INSERT ON {seed}.users "
        "FOR EACH ROW SET new.name=CONCAT(new.name, '!')"
    )
    command(project, "create", "feature")
    clone = identifier(selected(project))
    assert conn.execute(f"SELECT {clone}.fraction()").fetchone() == (Decimal("1.25"),)
    assert conn.execute(f"SELECT id, amount FROM {clone}.amounts").fetchone() == (
        1,
        Decimal("1.25"),
    )
    conn.execute(f"CALL {clone}.clear_local()")
    conn.execute(f"INSERT INTO {clone}.users(name) VALUES('clone')")
    assert conn.execute(f"SELECT name FROM {clone}.users").fetchone() == ("clone!",)
    assert conn.execute(f"SELECT name FROM {seed}.users").fetchone() == ("seed é 🐈",)


@pytest.mark.parametrize(
    "alias", ["SELECT 1 AS other", "SELECT id FROM {seed}.users AS other"]
)
def test_column_or_previous_statement_alias_cannot_hide_external_ddl(project, alias):
    seed = project[1]["dbname"]
    conn = project[4]
    # MySQL accepts deferred table references in stored routines. The object must
    # be refused at capture, before any generated database can select this routine.
    conn.execute(
        f"CREATE PROCEDURE {identifier(seed)}.unsafe_ddl() BEGIN "
        + alias.format(seed=identifier(seed))
        + "; TRUNCATE TABLE other.t; END"
    )
    with pytest.raises(ConfigError, match="qualified references"):
        capture_objects(conn, seed)


@pytest.mark.parametrize(
    "body",
    [
        "UPDATE IGNORE other.t SET id=1",
        "UPDATE LOW_PRIORITY other.t SET id=1",
        "INSERT other.t SELECT id FROM {seed}.users AS other",
        "INSERT IGNORE other.t SELECT id FROM {seed}.users AS other",
        "REPLACE other.t SELECT id FROM {seed}.users AS other",
        "UPDATE {seed}.users AS other, other.t SET other.id=1",
        "SELECT * FROM {seed}.users AS other JOIN {seed}.users AS u "
        "ON other.id=u.id, other.t",
    ],
)
def test_mysql_dml_modifiers_and_optional_into_cannot_hide_external_schema(
    project, body
):
    seed = project[1]["dbname"]
    conn = project[4]
    conn.execute(
        f"CREATE PROCEDURE {identifier(seed)}.unsafe_dml() "
        + body.format(seed=identifier(seed))
    )
    with pytest.raises(ConfigError, match="qualified references"):
        capture_objects(conn, seed)
