"""Check effective grants, including active roles and schema-level grant patterns."""

from __future__ import annotations

import re

from db_git.backends.mysql.connections import Connection
from db_git.errors import ConfigError

SOURCE = {"SELECT", "SHOW VIEW", "TRIGGER", "EVENT"}
DESTINATION = {
    "CREATE",
    "INSERT",
    "ALTER",
    "DROP",
    "INDEX",
    "REFERENCES",
    "CREATE VIEW",
    "CREATE ROUTINE",
    "ALTER ROUTINE",
    "EXECUTE",
    "TRIGGER",
    "EVENT",
}


def schema_matches(pattern: str, name: str) -> bool:
    expression = ""
    escaped = False
    for char in pattern:
        if escaped:
            expression += re.escape(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "%":
            expression += ".*"
        elif char == "_":
            expression += "."
        else:
            expression += re.escape(char)
    if escaped:
        expression += re.escape("\\")
    return re.fullmatch(expression, name) is not None


def check_permissions(conn: Connection, source: str, destination: str) -> None:
    roles = conn.execute(
        "SELECT ROLE_NAME, ROLE_HOST FROM information_schema.ENABLED_ROLES"
    ).fetchall()
    if roles:
        placeholders = ", ".join("%s@%s" for _ in roles)
        rows = conn.execute(
            "SHOW GRANTS FOR CURRENT_USER USING " + placeholders,
            tuple(value for row in roles for value in row),
        ).fetchall()
    else:
        rows = conn.execute("SHOW GRANTS").fetchall()
    if any(str(row[0]).startswith("REVOKE ") for row in rows):
        raise ConfigError(
            "MySQL partial revokes require explicit grant reconciliation "
            "before cloning."
        )
    literal_schemas = bool(conn.execute("SELECT @@partial_revokes").fetchone()[0])
    global_grants: set[str] = set()
    source_grants: set[str] = set()
    destination_grants: set[str] = set()
    for (grant,) in rows:
        match = re.match(r"GRANT (.+?) ON (\*|`(?:``|[^`])+`)\.\* TO ", grant)
        if not match:
            continue
        privileges = {item.strip() for item in match[1].split(",")}
        schema = match[2]
        if schema == "*":
            global_grants.update(privileges)
            source_grants.update(privileges)
            destination_grants.update(privileges)
        else:
            pattern = schema[1:-1].replace("``", "`")
            if (
                (pattern == source)
                if literal_schemas
                else schema_matches(pattern, source)
            ):
                source_grants.update(privileges)
            if (
                (pattern == destination)
                if literal_schemas
                else schema_matches(pattern, destination)
            ):
                destination_grants.update(privileges)
    missing = []
    if "BACKUP_ADMIN" not in global_grants:
        missing.append("global BACKUP_ADMIN")
    if not global_grants.intersection({"SELECT", "SHOW_ROUTINE", "ALL PRIVILEGES"}):
        missing.append(
            "global SHOW_ROUTINE (or SELECT) for complete routine definitions"
        )
    for label, required, actual in (
        ("source", SOURCE, source_grants),
        ("destination", DESTINATION, destination_grants),
    ):
        if "ALL PRIVILEGES" not in actual:
            missing.extend(
                f"{label} {privilege}" for privilege in sorted(required - actual)
            )
    if missing:
        raise ConfigError(
            "MySQL privileges missing: "
            + ", ".join(missing)
            + ". Grant schema privileges on the seed and managed-generation "
            "prefix; activate roles by default."
        )
