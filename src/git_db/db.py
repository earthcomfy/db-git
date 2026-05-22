from __future__ import annotations

from urllib.parse import unquote, urlparse


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
