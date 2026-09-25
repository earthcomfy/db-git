from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlsplit

from db_git.errors import ConfigError

SYSTEM_DATABASES = {"mysql", "sys", "information_schema", "performance_schema"}
OPTIONS = {"ssl_mode", "ssl_ca", "ssl_cert", "ssl_key", "connect_timeout"}


def validate_database(name: str) -> str:
    # Use MySQL's portable database-name subset; reject path/option delimiters.
    if (
        not re.fullmatch(r"[A-Za-z0-9_$-]{1,64}", name)
        or name.lower() in SYSTEM_DATABASES
    ):
        raise ConfigError(
            "MySQL database names must contain 1-64 ASCII letters, digits, _, $, "
            "or - and must not name a system database."
        )
    return name


def parse_url(url: str) -> dict[str, str | int | None]:
    try:
        if any(ord(char) < 32 for char in url):
            raise ValueError
        parsed = urlsplit(url)
        if (
            parsed.scheme != "mysql"
            or not parsed.hostname
            or parsed.username is None
            or parsed.fragment
            or re.search(r"%(?![0-9A-Fa-f]{2})", url)
        ):
            raise ValueError
        name = validate_database(unquote(parsed.path.removeprefix("/")))
        result: dict[str, str | int | None] = {
            "host": parsed.hostname,
            "port": parsed.port or 3306,
            "user": unquote(parsed.username),
            "password": unquote(parsed.password or ""),
            "dbname": name,
        }
        seen = set()
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key not in OPTIONS or key in seen or not value:
                raise ValueError
            seen.add(key)
            result[key] = value
        if result.get("ssl_mode", "REQUIRED") not in {
            "DISABLED",
            "REQUIRED",
            "VERIFY_CA",
            "VERIFY_IDENTITY",
        }:
            raise ValueError
        if result.get("ssl_mode") == "DISABLED" and any(
            key in result for key in ("ssl_ca", "ssl_cert", "ssl_key")
        ):
            raise ValueError
        if bool(result.get("ssl_cert")) != bool(result.get("ssl_key")):
            raise ValueError
        timeout = int(str(result.get("connect_timeout", 5)))
        if not 1 <= timeout <= 60 or not result["user"]:
            raise ValueError
        if any("\0" in str(value) for value in result.values()):
            raise ValueError
        result["connect_timeout"] = timeout
        result.setdefault("ssl_mode", "REQUIRED")
        return result
    except (ValueError, ConfigError) as e:
        # Never include the URL or driver parser error: either may contain secrets.
        raise ConfigError(
            "Invalid MySQL URL. Use mysql://user:password@host:port/database; "
            "supported options: ssl_mode, ssl_ca, ssl_cert, ssl_key, connect_timeout."
        ) from e
