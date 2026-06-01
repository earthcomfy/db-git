from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlparse, urlunparse


def format_age(iso_timestamp: str) -> str:
    """
    Return human-readable age like '2h ago', '3d ago'.
    """
    try:
        created = datetime.fromisoformat(iso_timestamp)
        now = datetime.now(UTC)
        delta = now - created
        seconds = int(delta.total_seconds())

        if seconds < 60:
            return "just now"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        if seconds < 604800:
            return f"{seconds // 86400}d ago"
        return f"{seconds // 604800}w ago"
    except (ValueError, TypeError):
        return "unknown"


def format_size(size_bytes: int | None) -> str:
    """
    Return human-readable size like '1.2 MB', '340 KB'.
    """
    if size_bytes is None:
        return "template"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def mask_url(url: str) -> str:
    """
    Replace password in database URL with ****.
    """
    parsed = urlparse(url)
    if parsed.password:
        netloc = f"{parsed.username}:****@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        masked = parsed._replace(netloc=netloc)
        return urlunparse(masked)
    return url
