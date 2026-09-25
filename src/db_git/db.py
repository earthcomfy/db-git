from __future__ import annotations

from urllib.parse import quote, unquote, urlparse, urlunparse

import psycopg
from psycopg.conninfo import conninfo_to_dict

from db_git.errors import ConfigError


def parse_database_url(url: str) -> dict[str, str | int | None]:
    """Parse PostgreSQL URLs with libpq, including its query options and escaping."""
    try:
        if urlparse(url).scheme not in {"postgres", "postgresql"}:
            raise ConfigError("Expected a postgres:// or postgresql:// URL.")
        params = conninfo_to_dict(url)
    except (psycopg.Error, ValueError) as e:
        # libpq errors can contain the input URL or option values (credentials).
        raise ConfigError(
            "Invalid PostgreSQL URL. Check escaping, port, and libpq option names."
        ) from e
    result: dict[str, str | int | None] = dict.fromkeys(
        ("user", "password", "host", "port", "dbname")
    )
    result.update(params)
    port = result.get("port")
    if isinstance(port, str) and port.isdecimal():
        result["port"] = int(port)
    return result


def with_database_name(url: str, dbname: str) -> str:
    """Replace the effective database while preserving connection options."""
    parsed = urlparse(url)
    # libpq query parameters override the URI path, so replace dbname there too.
    query = "&".join(
        f"{part.split('=', 1)[0]}={quote(dbname, safe='')}"
        if unquote(part.split("=", 1)[0]) == "dbname"
        else part
        for part in parsed.query.split("&")
    )
    return urlunparse(parsed._replace(path=f"/{quote(dbname, safe='')}", query=query))
