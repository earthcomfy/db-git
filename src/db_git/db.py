from __future__ import annotations

from urllib.parse import quote, unquote, urlencode, urlparse, urlunparse

import psycopg
from psycopg.conninfo import conninfo_to_dict

from db_git.errors import ConfigError


def parse_database_url(url: str) -> dict[str, str | int | None]:
    """Parse a supported engine URL; preserve PostgreSQL options through libpq."""
    if urlparse(url).scheme == "sqlite":
        from db_git.backends.sqlite.urls import database_path

        return {"dbname": str(database_path(url))}
    if urlparse(url).scheme == "mysql":
        from db_git.backends.mysql.urls import parse_url

        return parse_url(url)
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
    if parsed.scheme == "sqlite":
        from pathlib import Path

        from db_git.backends.sqlite.urls import database_url

        return database_url(Path(dbname))
    # libpq query parameters override the URI path, so replace dbname there too.
    query = "&".join(
        f"{part.split('=', 1)[0]}={quote(dbname, safe='')}"
        if unquote(part.split("=", 1)[0]) == "dbname"
        else part
        for part in parsed.query.split("&")
    )
    rewritten = urlunparse(
        parsed._replace(path=f"/{quote(dbname, safe='')}", query=query)
    )
    # urllib treats PostgreSQL as an unknown scheme and drops an empty authority.
    if not parsed.netloc:
        rewritten = f"{parsed.scheme}://{rewritten[len(parsed.scheme) + 1 :]}"
    return rewritten


def with_connection_defaults(url: str, params: dict[str, str | int]) -> str:
    """Make implicit backend defaults explicit for applications using the URL."""
    original = parse_database_url(url)
    missing = {
        key: params[key]
        for key in ("host", "port", "user")
        if original.get(key) is None and key in params
    }
    if not missing:
        return url
    # Preserve existing query bytes (libpq treats '+' literally).
    before_fragment, separator, fragment = url.partition("#")
    joiner = "&" if "?" in before_fragment else "?"
    return (
        before_fragment
        + joiner
        + urlencode(missing, quote_via=quote)
        + (separator + fragment if separator else "")
    )
