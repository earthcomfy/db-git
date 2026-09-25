from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from db_git.backends.mysql.connections import (
    Connection,
    check_clients,
    client_options,
    identifier,
    subprocess_env,
)
from db_git.backends.mysql.objects import capture_objects, restore_objects
from db_git.backends.mysql.operations import (
    MARKER,
    MySQLResources,
    generation,
    server_identity,
    validate_generation,
)
from db_git.backends.mysql.permissions import check_permissions
from db_git.errors import ConfigError, DatabaseError


def validate_source(conn: Connection, source: str, seed: str) -> None:
    if source != seed:
        validate_generation(source, seed)
        if MySQLResources(conn).identity(source) in {None, "unmanaged"}:
            raise DatabaseError("Source MySQL generation is missing or unowned.")
    rows = conn.execute(
        "SELECT TABLE_NAME, TABLE_TYPE, ENGINE FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=%s",
        (source,),
    ).fetchall()
    for name, table_type, engine in rows:
        if name.lower() == MARKER and source == seed:
            raise ConfigError(
                "MySQL seed uses the reserved __dbgit_generation table name."
            )
        if table_type != "VIEW" and engine != "InnoDB":
            raise ConfigError(
                "MySQL cloning requires InnoDB tables; other storage engines "
                "are unsupported."
            )
    if conn.execute(
        "SELECT 1 FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA=%s AND REFERENCED_TABLE_SCHEMA IS NOT NULL "
        "AND REFERENCED_TABLE_SCHEMA<>%s LIMIT 1",
        (source, source),
    ).fetchone():
        raise ConfigError("MySQL cloning does not support cross-database foreign keys.")


def capture(
    conn: Connection, params: dict[str, str | int], source: str, archive: Path
) -> None:
    """Write a self-contained archive: transactional table dump plus stored objects."""
    check_clients()
    seed = str(params["dbname"])
    check_permissions(conn, source, generation(seed, "check"))
    with (
        tempfile.TemporaryDirectory(prefix="dbgit-mysql-dump-") as directory,
        client_options(params) as options,
    ):
        dump = Path(directory) / "data.sql"
        conn.execute("SET SESSION lock_wait_timeout=5")
        conn.execute("LOCK INSTANCE FOR BACKUP")
        try:
            validate_source(conn, source, seed)
            schema = conn.execute(
                "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
                "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s",
                (source,),
            ).fetchone()
            if schema is None:
                raise DatabaseError("Source MySQL database does not exist.")
            objects = capture_objects(conn, source)
            ignored = [
                MARKER,
                *(obj["name"] for obj in objects if obj["kind"] == "VIEW"),
            ]
            with dump.open("wb") as output:
                result = subprocess.run(
                    [
                        "mysqldump",
                        *options,
                        "--single-transaction",
                        "--skip-lock-tables",
                        "--set-gtid-purged=OFF",
                        "--no-tablespaces",
                        "--skip-triggers",
                        "--skip-routines",
                        "--skip-events",
                        "--skip-add-locks",
                        "--hex-blob",
                        "--column-statistics=0",
                        *[f"--ignore-table={source}.{name}" for name in ignored],
                        "--",
                        source,
                    ],
                    stdout=output,
                    stderr=subprocess.PIPE,
                    env=subprocess_env(),
                )
            if result.returncode:
                raise DatabaseError(
                    "mysqldump failed; source preserved. Check client "
                    "compatibility and privileges."
                )
            if objects != capture_objects(conn, source):
                raise DatabaseError(
                    "Stored objects changed during the dump; retry after schema "
                    "changes finish."
                )
            manifest = {
                "format": 1,
                "server": server_identity(conn, seed),
                "source": source,
                "charset": schema[0],
                "collation": schema[1],
                "objects": objects,
            }
        finally:
            conn.execute("UNLOCK INSTANCE")
        # Both members are published together by the recovery journal.
        with open(
            archive, "xb", opener=lambda name, flags: os.open(name, flags, 0o600)
        ) as output:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as bundle:
                bundle.write(dump, "data.sql")
                bundle.writestr(
                    "manifest.json", json.dumps(manifest, ensure_ascii=False)
                )
            output.flush()
            os.fsync(output.fileno())


def restore_archive(
    conn: Connection, params: dict[str, str | int], archive: Path, destination: str
) -> None:
    check_clients()
    seed = str(params["dbname"])
    validate_generation(destination, seed)
    try:
        with zipfile.ZipFile(archive) as bundle:
            if sorted(bundle.namelist()) != ["data.sql", "manifest.json"]:
                raise ValueError("unexpected archive members")
            manifest = json.loads(bundle.read("manifest.json"))
            if manifest["format"] != 1 or manifest["server"] != server_identity(
                conn, seed
            ):
                raise ValueError("wrong archive server or format")
            source = manifest["source"]
            if source != seed:
                validate_generation(source, seed)
            check_permissions(conn, seed, destination)
            # Validate every definition before allocating a database. Replay uses
            # SHOW CREATE statements, not a lossy search/replace of SQL dump data.
            from db_git.backends.mysql.objects import rewrite_definition

            for obj in manifest["objects"]:
                rewrite_definition(
                    obj["sql"], source, destination, obj["kind"], obj["sql_mode"]
                )
            with tempfile.TemporaryDirectory(
                prefix="dbgit-mysql-restore-"
            ) as directory:
                dump = Path(directory) / "data.sql"
                with bundle.open("data.sql") as input_file, dump.open("wb") as output:
                    shutil.copyfileobj(input_file, output)
                conn.execute("SET SESSION lock_wait_timeout=5")
                conn.execute(
                    f"CREATE DATABASE {identifier(destination)} "
                    f"CHARACTER SET {identifier(manifest['charset'])} "
                    f"COLLATE {identifier(manifest['collation'])}"
                )
                conn.execute(
                    f"CREATE TABLE {identifier(destination)}.{identifier(MARKER)} "
                    "(reserved TINYINT PRIMARY KEY) ENGINE=InnoDB COMMENT=%s",
                    ("dbgit:" + destination.rsplit("_", 1)[1],),
                )
                with client_options(params) as options, dump.open("rb") as input_file:
                    result = subprocess.run(
                        [
                            "mysql",
                            *options,
                            "--binary-mode",
                            "--local-infile=0",
                            "--init-command=SET SESSION lock_wait_timeout=5",
                            "--",
                            destination,
                        ],
                        stdin=input_file,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        env=subprocess_env(),
                    )
                if result.returncode:
                    raise DatabaseError(
                        "MySQL restore failed; previous database preserved. Inspect "
                        "db-git recover."
                    )
                restore_objects(
                    conn,
                    manifest["objects"],
                    source,
                    destination,
                    manifest["collation"],
                )
    except (zipfile.BadZipFile, KeyError, TypeError, ValueError) as e:
        raise DatabaseError(
            "Invalid MySQL snapshot archive or server identity; preserve "
            "it for inspection."
        ) from e


def clone(
    conn: Connection, params: dict[str, str | int], source: str, destination: str
) -> None:
    with tempfile.TemporaryDirectory(prefix="dbgit-mysql-clone-") as directory:
        archive = Path(directory) / "snapshot.dump"
        capture(conn, params, source, archive)
        restore_archive(conn, params, archive, destination)
