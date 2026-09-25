"""Capture stored objects separately so restore never runs triggers during data load."""

from __future__ import annotations

import re
from dataclasses import dataclass

from db_git.backends.mysql.connections import Connection, identifier
from db_git.errors import ConfigError, DatabaseError


@dataclass
class Token:
    start: int
    end: int
    value: str
    kind: str


def tokens(sql: str, mode: str = "") -> list[Token]:
    """Lex identifiers without rewriting quoted data, comments, or SQL strings."""
    result = []
    index = 0
    while index < len(sql):
        start = index
        char = sql[index]
        if char.isspace():
            index += 1
            continue
        if sql.startswith("/*", index):
            if sql.startswith("/*!", index):
                raise ConfigError(
                    "Executable comments in stored objects are unsupported."
                )
            end = sql.find("*/", index + 2)
            if end < 0:
                raise ConfigError("Unterminated stored-object comment.")
            index = end + 2
            continue
        if char == "#" or (
            sql.startswith("--", index)
            and (index + 2 == len(sql) or sql[index + 2].isspace())
        ):
            end = sql.find("\n", index)
            index = len(sql) if end < 0 else end + 1
            continue
        if char in "`'\"":
            quote = char
            kind = (
                "identifier"
                if char == "`" or (char == '"' and "ANSI_QUOTES" in mode.split(","))
                else "string"
            )
            index += 1
            value = ""
            while index < len(sql):
                if sql[index] == quote:
                    if index + 1 < len(sql) and sql[index + 1] == quote:
                        value += quote
                        index += 2
                        continue
                    index += 1
                    break
                if (
                    sql[index] == "\\"
                    and kind == "string"
                    and "NO_BACKSLASH_ESCAPES" not in mode.split(",")
                ):
                    value += sql[index : index + 2]
                    index += 2
                else:
                    value += sql[index]
                    index += 1
            else:
                raise ConfigError("Unterminated stored-object quoted value.")
            result.append(Token(start, index, value, kind))
            continue
        # A decimal point belongs to its literal, not a schema qualifier.
        number = re.match(
            r"(?:0[xX][0-9a-fA-F]+|0[bB][01]+|"
            r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(?![\w$])",
            sql[index:],
        )
        if number:
            index += len(number[0])
            result.append(Token(start, index, number[0], "number"))
            continue
        match = re.match(r"[\w$]+", sql[index:])
        if match:
            value = match[0]
            index += len(value)
            result.append(Token(start, index, value, "word"))
        else:
            index += 1
            result.append(Token(start, index, char, "symbol"))
    return result


def rewrite_definition(sql: str, source: str, target: str, kind: str, mode: str) -> str:
    items = tokens(sql, mode)
    edits: list[tuple[int, int, str]] = []
    # SHOW CREATE normalizes object headers. Rebind the definer to the cloning
    # account without importing another user's privileges or orphaned ownership.
    header = next(
        (
            i
            for i, t in enumerate(items)
            if t.kind == "word" and t.value.upper() == kind
        ),
        None,
    )
    if header is None:
        raise ConfigError("Invalid stored-object definition.")
    for i, token in enumerate(items[:header]):
        if token.value.upper() == "DEFINER" and items[i + 1].value == "=":
            end = i + 2
            # SHOW CREATE emits a quoted user, @, and a quoted host.
            if end + 2 >= header or items[end + 1].value != "@":
                raise ConfigError("Invalid stored-object definer.")
            edits.append((items[end].start, items[end + 2].end, "CURRENT_USER"))
            break
    # Only table aliases qualify columns. Column aliases cannot authorize schema
    # references, and aliases must not leak between statements in a routine.
    statement_ids = []
    statement = 0
    for token in items:
        statement_ids.append(statement)
        if token.value == ";":
            statement += 1
    aliases: dict[int, set[str]] = {}
    unsafe_alias_statements: set[int] = set()
    table_positions: set[int] = set()
    # Resolve common table references/aliases. Unknown dotted qualifiers are
    # rejected; silently retaining another database would break branch isolation.
    table_keywords = {
        "FROM",
        "JOIN",
        "UPDATE",
        "INSERT",
        "REPLACE",
        "INTO",
        "REFERENCES",
        "CALL",
        "USING",
    }
    object_keywords = {
        "TABLE",
        "TABLES",
        "VIEW",
        "TRIGGER",
        "EVENT",
        "PROCEDURE",
        "FUNCTION",
    }
    boundaries = table_keywords | {
        "SELECT",
        "WHERE",
        "SET",
        "VALUES",
        "ON",
        "RETURN",
        "GROUP",
        "ORDER",
        "HAVING",
    }
    depth = 0
    table_clauses = {0: False}
    for i, token in enumerate(items):
        word = token.value.upper() if token.kind == "word" else ""
        current = statement_ids[i]
        local_aliases = aliases.setdefault(current, set())
        if token.value == ";":
            depth = 0
            table_clauses = {0: False}
        elif token.value == "(":
            depth += 1
            table_clauses.setdefault(depth, False)
        elif token.value == ")":
            table_clauses.pop(depth, None)
            depth = max(0, depth - 1)
        # DDL has additional object-name positions. Never interpret its dotted
        # names as column aliases. Simple local DDL still redirects to the clone;
        # ambiguous DDL mixed with aliased queries is refused.
        if i > header and word in {
            "CREATE",
            "ALTER",
            "DROP",
            "TRUNCATE",
            "RENAME",
            "GRANT",
            "REVOKE",
            "HANDLER",
            "DESCRIBE",
        }:
            unsafe_alias_statements.add(current)
        if word in {"FROM", "JOIN", "UPDATE", "USING", "TABLE", "TABLES"}:
            table_clauses[depth] = True
        elif word in {
            "SELECT",
            "WHERE",
            "SET",
            "VALUES",
            "RETURN",
            "GROUP",
            "ORDER",
            "HAVING",
            "LIMIT",
            "UNION",
        }:
            table_clauses[depth] = False
        # JOIN's ON expression does not end a table list. Parenthesis depth keeps
        # commas in its function calls separate from commas introducing tables.
        is_table_separator = token.value == "," and table_clauses.get(depth, False)
        start = None
        if word in table_keywords | object_keywords or is_table_separator:
            start = i + 1
        if start is not None and word in {"UPDATE", "INSERT", "REPLACE"}:
            # MySQL permits priority/IGNORE modifiers and makes INSERT/REPLACE's
            # INTO optional. These words cannot become a fictitious table alias.
            while (
                start < len(items)
                and items[start].kind == "word"
                and items[start].value.upper()
                in {"LOW_PRIORITY", "HIGH_PRIORITY", "DELAYED", "IGNORE", "INTO"}
            ):
                start += 1
        if (
            start is not None
            and start < len(items)
            and items[start].value == "("
            and (word in {"FROM", "JOIN", "UPDATE", "USING"} or is_table_separator)
        ):
            raise ConfigError(
                "Parenthesized table references in stored objects are unsupported; "
                "use simple table references with explicit aliases."
            )
        if (
            start is None
            or start >= len(items)
            or items[start].kind not in {"word", "identifier"}
        ):
            continue
        table_positions.add(start)
        end = start
        if start + 2 < len(items) and items[start + 1].value == ".":
            end = start + 2
        if word not in {"FROM", "JOIN", "UPDATE", "USING"} and not is_table_separator:
            continue
        local_aliases.add(items[end].value)
        candidate_index = end + 1
        if (
            candidate_index < len(items)
            and items[candidate_index].value.upper() == "AS"
        ):
            candidate_index += 1
        if candidate_index < len(items):
            candidate = items[candidate_index]
            if candidate.kind == "identifier" or (
                candidate.kind == "word"
                and candidate.value.upper()
                not in boundaries
                | {
                    "AS",
                    "LIMIT",
                    "UNION",
                    "LEFT",
                    "RIGHT",
                    "INNER",
                    "OUTER",
                    "CROSS",
                    "FOR",
                    "LOCK",
                    "USE",
                    "FORCE",
                    "IGNORE",
                }
            ):
                local_aliases.add(candidate.value)
    for i, token in enumerate(items):
        if (
            token.kind in {"word", "identifier"}
            and i + 1 < len(items)
            and items[i + 1].value == "."
            and (i == 0 or items[i - 1].value != ".")
        ):
            is_call = i + 3 < len(items) and items[i + 3].value == "("
            current = statement_ids[i]
            three_part = i + 3 < len(items) and items[i + 3].value == "."
            is_alias = token.value in aliases.get(current, set()) or (
                kind == "TRIGGER" and token.value.upper() in {"NEW", "OLD"}
            )
            if token.value != source and (
                i in table_positions
                or is_call
                or three_part
                or current in unsafe_alias_statements
                or not is_alias
            ):
                raise ConfigError(
                    "Cross-database or unresolved qualified references in stored "
                    "objects cannot be cloned; use local tables and explicit "
                    "aliases."
                )
        if token.kind == "word" and token.value.upper() in {"PREPARE", "EXECUTE"}:
            raise ConfigError(
                "Dynamic SQL in stored objects cannot be safely redirected to a clone."
            )
        if token.kind not in {"word", "identifier"} or token.value != source:
            continue
        if i + 1 < len(items) and items[i + 1].value == ".":
            edits.append((token.start, token.end, identifier(target)))
        elif i > header:
            # A local alias/variable with the database's name makes its qualified
            # references ambiguous. Refuse instead of changing their meaning.
            raise ConfigError(
                "A stored object uses the source database name as a local "
                "identifier; rename that alias or variable before cloning."
            )
    if kind == "EVENT":
        # Event status precedes DO; tokens inside the body must remain untouched.
        do = next(
            (
                i
                for i in range(header + 1, len(items))
                if items[i].kind == "word" and items[i].value.upper() == "DO"
            ),
            None,
        )
        if do is None:
            raise ConfigError("Invalid event definition.")
        status = next(
            (
                i
                for i in range(header + 1, do)
                if items[i].kind == "word"
                and items[i].value.upper() in {"ENABLE", "DISABLE"}
            ),
            None,
        )
        completion = next(
            (
                i
                for i in range(header + 1, do)
                if items[i].kind == "word" and items[i].value.upper() == "COMPLETION"
            ),
            None,
        )
        if completion is not None and items[completion + 1].value.upper() == "NOT":
            # Preserve expired one-time events for inspection after snapshot restore.
            edits.append(
                (items[completion + 1].start, items[completion + 2].end, "PRESERVE")
            )
        if status is None:
            edits.append((items[do].start, items[do].start, "DISABLE "))
        else:
            end = status
            if status + 2 < do and items[status + 1].value.upper() == "ON":
                end += 2  # DISABLE ON SLAVE / REPLICA becomes DISABLE.
            edits.append((items[status].start, items[end].end, "DISABLE"))
    for start, end, replacement in sorted(edits, reverse=True):
        sql = sql[:start] + replacement + sql[end:]
    return sql


def capture_objects(conn: Connection, source: str) -> list[dict[str, str]]:
    catalogs = [
        (
            "FUNCTION",
            "ROUTINES",
            "ROUTINE_NAME",
            "ROUTINE_SCHEMA",
            "AND ROUTINE_TYPE='FUNCTION'",
            "ROUTINE_NAME",
        ),
        (
            "PROCEDURE",
            "ROUTINES",
            "ROUTINE_NAME",
            "ROUTINE_SCHEMA",
            "AND ROUTINE_TYPE='PROCEDURE'",
            "ROUTINE_NAME",
        ),
        ("VIEW", "VIEWS", "TABLE_NAME", "TABLE_SCHEMA", "", "TABLE_NAME"),
        (
            "TRIGGER",
            "TRIGGERS",
            "TRIGGER_NAME",
            "TRIGGER_SCHEMA",
            "",
            "EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_ORDER",
        ),
        ("EVENT", "EVENTS", "EVENT_NAME", "EVENT_SCHEMA", "", "EVENT_NAME"),
    ]
    objects: list[dict[str, str]] = []
    for kind, catalog, column, schema, condition, order in catalogs:
        names = conn.execute(
            f"SELECT {column} FROM information_schema.{catalog} "
            f"WHERE {schema}=%s {condition} ORDER BY {order}",
            (source,),
        ).fetchall()
        for (name,) in names:
            # Views expose normalized SQL rather than a creation sql_mode.
            # Capture with default quoting and replay in the same mode: view
            # literals use backslash escapes even under NO_BACKSLASH_ESCAPES.
            view_mode = (
                conn.execute("SELECT @@sql_mode").fetchone()[0]
                if kind == "VIEW"
                else None
            )
            try:
                if view_mode is not None:
                    conn.execute("SET SESSION sql_mode=''")
                cursor = conn.execute(
                    f"SHOW CREATE {kind} {identifier(source)}.{identifier(name)}"
                )
                row = cursor.fetchone()
                columns = [d[0].lower() for d in cursor.description]
            finally:
                if view_mode is not None:
                    conn.execute("SET SESSION sql_mode=%s", (view_mode,))
            values = dict(zip(columns, row, strict=True))
            ddl = values.get("create " + kind.lower()) or values.get(
                "sql original statement"
            )
            if not ddl:
                raise DatabaseError(
                    "Stored-object definition is not visible; check SHOW_ROUTINE "
                    "privileges."
                )
            mode = str(values.get("sql_mode", ""))
            # Validate now, before allocating a destination database.
            rewrite_definition(str(ddl), source, "dbgit_validation", kind, mode)
            objects.append(
                {
                    "kind": kind,
                    "name": str(name),
                    "sql": str(ddl),
                    "sql_mode": mode,
                    "charset": str(values["character_set_client"]),
                    "collation": str(values["collation_connection"]),
                    "time_zone": str(values.get("time_zone", "+00:00")),
                    "database_collation": str(values.get("database collation", "")),
                }
            )
    # Views can depend on other views. MySQL exposes those edges without parsing SQL.
    views = {obj["name"]: obj for obj in objects if obj["kind"] == "VIEW"}
    edges = conn.execute(
        "SELECT VIEW_NAME, TABLE_NAME, TABLE_SCHEMA FROM "
        "information_schema.VIEW_TABLE_USAGE WHERE VIEW_SCHEMA=%s",
        (source,),
    ).fetchall()
    dependencies: dict[str, set[str]] = {name: set() for name in views}
    for view, table, schema in edges:
        if schema != source:
            raise ConfigError(
                "Views referencing other databases cannot be cloned in isolation."
            )
        if table in views:
            dependencies[view].add(table)
    sorted_views = []
    while dependencies:
        ready = sorted(name for name, deps in dependencies.items() if not deps)
        if not ready:
            raise ConfigError("View dependency cycle prevents cloning.")
        for name in ready:
            sorted_views.append(views[name])
            del dependencies[name]
        for deps in dependencies.values():
            deps.difference_update(ready)
    return (
        [o for o in objects if o["kind"] in {"FUNCTION", "PROCEDURE"}]
        + sorted_views
        + [o for o in objects if o["kind"] in {"TRIGGER", "EVENT"}]
    )


def restore_objects(
    conn: Connection,
    objects: list[dict[str, str]],
    source: str,
    destination: str,
    collation: str,
) -> None:
    settings = conn.execute(
        "SELECT @@sql_mode, @@character_set_client, @@collation_connection, @@time_zone"
    ).fetchone()
    conn.execute(f"USE {identifier(destination)}")
    try:
        for obj in objects:
            conn.execute(
                "SET SESSION sql_mode=%s, character_set_client=%s, "
                "collation_connection=%s, time_zone=%s",
                (obj["sql_mode"], obj["charset"], obj["collation"], obj["time_zone"]),
            )
            # Routines/triggers retain their original database collation at creation.
            object_collation = obj["database_collation"] or collation
            conn.execute(
                f"ALTER DATABASE {identifier(destination)} "
                f"COLLATE {identifier(object_collation)}"
            )
            ddl = rewrite_definition(
                obj["sql"], source, destination, obj["kind"], obj["sql_mode"]
            )
            from pymysql.charset import charset_by_name

            conn.execute(ddl.encode(charset_by_name(obj["charset"]).encoding))
    finally:
        conn.execute(
            "SET SESSION sql_mode=%s, character_set_client=%s, "
            "collation_connection=%s, time_zone=%s",
            settings,
        )
        conn.execute(
            f"ALTER DATABASE {identifier(destination)} COLLATE {identifier(collation)}"
        )
