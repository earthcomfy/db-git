from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from db_git.errors import ConfigError


def database_path(url: str, root: Path | None = None) -> Path:
    """Accept sqlite:///relative.db or sqlite:////absolute/path.db, without options."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "sqlite"
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not url.startswith("sqlite:///")
    ):
        raise ConfigError(
            "Use sqlite:///relative.db or sqlite:////absolute/path.db without "
            "credentials, query options, or fragments."
        )
    name = unquote(parsed.path[1:])
    if not name or name == ":memory:" or "\x00" in name or name.startswith("file:"):
        raise ConfigError(
            "SQLite requires a persistent database file; "
            "memory/URI databases are unsupported."
        )
    path = Path(name)
    return (path if path.is_absolute() else (root or Path.cwd()) / path).resolve()


def database_url(path: Path) -> str:
    return "sqlite:///" + quote(str(path.resolve()), safe="/:")
