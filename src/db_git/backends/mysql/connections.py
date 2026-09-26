from __future__ import annotations

import os
import re
import shutil
import ssl
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from db_git.errors import ConfigError, DatabaseError, ToolNotFoundError


def identifier(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


class Connection:
    """Small driver adapter; raw server messages never reach normal CLI output."""

    def __init__(self, params: dict[str, str | int]) -> None:
        try:
            import pymysql
        except ImportError as e:
            raise ConfigError(
                "MySQL requires the optional driver: install 'db-git[mysql]'."
            ) from e
        mode = str(params.get("ssl_mode", "REQUIRED"))
        context = None
        if mode != "DISABLED":
            try:
                context = ssl.create_default_context(
                    cafile=str(params["ssl_ca"]) if params.get("ssl_ca") else None
                )
                context.check_hostname = mode == "VERIFY_IDENTITY"
                if mode == "REQUIRED":
                    context.verify_mode = ssl.CERT_NONE
                if params.get("ssl_cert"):
                    context.load_cert_chain(
                        str(params["ssl_cert"]), str(params["ssl_key"])
                    )
            except (OSError, ValueError) as e:
                raise ConfigError("Cannot load MySQL TLS certificate settings.") from e
        try:
            self.raw = pymysql.connect(
                host=str(params["host"]),
                port=int(params["port"]),
                user=str(params["user"]),
                password=str(params.get("password", "")),
                charset="utf8mb4",
                autocommit=True,
                connect_timeout=int(params.get("connect_timeout", 5)),
                read_timeout=30,
                write_timeout=30,
                ssl=context,
            )
            # PyMySQL may fall back on servers without TLS; enforce the URL policy.
            if mode != "DISABLED":
                row = self.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'").fetchone()
                if row is None or not row[1]:
                    self.close()
                    raise DatabaseError("MySQL server did not establish required TLS.")
        except pymysql.MySQLError as e:
            raise DatabaseError(
                "MySQL connection failed; check server, TLS, and credentials."
            ) from e

    def execute(self, query: Any, params: Any = None) -> Any:
        import pymysql

        try:
            cursor = self.raw.cursor()
            cursor.execute(query, params)
            # PyMySQL's default cursor buffers results and has no server-side handle.
            return cursor
        except pymysql.MySQLError as e:
            code = e.args[0] if e.args and isinstance(e.args[0], int) else "unknown"
            hint = {
                1205: "lock wait timed out; finish concurrent schema changes and retry",
                1419: (
                    "stored-function creation is restricted by binary logging; "
                    "ask the server administrator about "
                    "log_bin_trust_function_creators or the required "
                    "administrative privilege"
                ),
            }.get(
                code if isinstance(code, int) else 0,
                "check privileges and server state",
            )
            raise DatabaseError(f"MySQL query failed (error {code}); {hint}.") from e

    def close(self) -> None:
        self.raw.close()


def server_version(conn: Connection) -> str:
    row = conn.execute("SELECT VERSION(), @@version_comment").fetchone()
    version = str(row[0])
    if "mariadb" in (version + str(row[1])).lower() or not re.match(
        r"^8\.(0|4)\.", version
    ):
        raise ConfigError(
            "Supported MySQL servers: 8.0 and 8.4. MariaDB is not supported."
        )
    return version


def check_clients() -> None:
    for tool in ("mysqldump", "mysql"):
        if shutil.which(tool) is None:
            raise ToolNotFoundError(f"MySQL requires '{tool}' on PATH.")
        try:
            result = subprocess.run(
                [tool, "--no-defaults", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise ToolNotFoundError(f"Cannot run MySQL client '{tool}'.") from e
        if result.returncode or "mariadb" in result.stdout.lower():
            raise ConfigError(
                f"Use Oracle MySQL clients for '{tool}'; MariaDB is not supported."
            )


def subprocess_env() -> dict[str, str]:
    # Option files provide all connection settings; inherited MYSQL_PWD must not win.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("MYSQL_")
    }
    # MySQL 8.0 clients lack --no-login-paths. Redirect the login file to the
    # null device so saved login paths cannot override our private option file.
    env["MYSQL_TEST_LOGIN_FILE"] = os.devnull
    return env


@contextmanager
def client_options(params: dict[str, str | int]) -> Iterator[list[str]]:
    """Keep credentials out of argv and environment; use an owner-only option file."""

    def escape(value: str | int) -> str:
        return (
            '"'
            + str(value)
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
            + '"'
        )

    mapping = {
        "host": "host",
        "port": "port",
        "user": "user",
        "password": "password",
        "ssl_mode": "ssl-mode",
        "ssl_ca": "ssl-ca",
        "ssl_cert": "ssl-cert",
        "ssl_key": "ssl-key",
    }
    with tempfile.TemporaryDirectory(prefix="dbgit-mysql-") as directory:
        path = Path(directory) / "client.cnf"
        with open(
            path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)
        ) as file:
            file.write("[client]\nprotocol=TCP\ndefault-character-set=utf8mb4\n")
            for key, option in mapping.items():
                if key in params:
                    file.write(f"{option}={escape(params[key])}\n")
            # mysqldump does not implement connect-timeout; keep it out of [client].
            file.write("[mysql]\n")
            file.write(f"connect-timeout={int(params.get('connect_timeout', 5))}\n")
        yield [f"--defaults-file={path}"]
