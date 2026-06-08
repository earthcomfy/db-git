from __future__ import annotations

from urllib.parse import unquote, urlparse, urlunparse


def parse_database_url(url: str) -> dict[str, str | int | None]:
    """
    Parse a database connection URL into component parts.
    """
    parsed = urlparse(url)
    return {
        "user": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
        "host": parsed.hostname or None,
        "port": parsed.port,
        "dbname": parsed.path.lstrip("/") or None,
    }


def with_database_name(url: str, dbname: str) -> str:
    """
    Return url with its database name replaced by dbname.

    Only the database name is swapped; credentials, host, port, and query
    string are preserved as-is.
    """
    parsed = urlparse(url)
    return urlunparse(parsed._replace(path=f"/{dbname}"))
