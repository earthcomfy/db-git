from __future__ import annotations

import contextlib
import importlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from git_db.errors import ConfigError

if TYPE_CHECKING:
    from git_db.config import GitDbConfig
    from git_db.state import BranchDbEntry

_BACKEND_REGISTRY: dict[str, type[DatabaseBackend]] = {}
_BUILTIN_BACKENDS: dict[str, str] = {
    "postgresql": "git_db.backends.postgresql.backend",
}


class DbCursor(Protocol):
    """
    Minimal cursor protocol for database query results.
    """

    def fetchone(self) -> tuple[Any, ...] | None: ...
    def fetchall(self) -> list[tuple[Any, ...]]: ...


class DbConnection(Protocol):
    """
    Minimal connection protocol returned by connect_maintenance.
    """

    def execute(self, query: Any, params: Any = None) -> DbCursor: ...
    def close(self) -> None: ...


@runtime_checkable
class SnapshotStrategy(Protocol):
    """
    Protocol for snapshot save/restore strategies.
    """

    name: str

    def save(
        self,
        db_url: str,
        branch: str,
        snapshot_dir: Path,
        config: GitDbConfig,
    ) -> None: ...

    def restore(
        self,
        db_url: str,
        branch: str,
        snapshot_dir: Path,
        config: GitDbConfig,
    ) -> None: ...

    def cleanup(
        self,
        branch: str,
        snapshot_dir: Path,
        config: GitDbConfig,
    ) -> None: ...


class BranchDbManager(Protocol):
    """
    Protocol for per-branch database operations.
    """

    def exists(self, name: str) -> bool: ...

    def create(
        self,
        target: str,
        source: str,
        branch: str,
        created_from: str,
        git_dir: Path,
    ) -> None: ...

    def drop(
        self,
        name: str,
        branch: str,
        git_dir: Path,
    ) -> None: ...

    def list(
        self,
        git_dir: Path,
    ) -> list[tuple[str, BranchDbEntry, bool]]: ...


@runtime_checkable
class DatabaseBackend(Protocol):
    """
    Protocol for database engine backends.
    """

    engine: str
    max_identifier_length: int

    def apply_url_defaults(
        self, params: dict[str, str | int | None]
    ) -> dict[str, str | int]: ...

    def get_engine_version(self, url: str) -> int: ...

    def detect_strategy(self, config: GitDbConfig) -> SnapshotStrategy: ...

    def branch_db_manager(self, config: GitDbConfig) -> BranchDbManager: ...

    def connect_maintenance(self, params: dict[str, str | int]) -> DbConnection: ...

    def build_subprocess_env(self, params: dict[str, str | int]) -> dict[str, str]: ...

    def check_permissions(self, url: str) -> object: ...

    def database_exists(self, url: str, name: str) -> bool: ...


def register_backend(scheme: str, cls: type[DatabaseBackend]) -> None:
    """
    Register a backend class for a URL scheme.
    """
    _BACKEND_REGISTRY[scheme] = cls


def get_backend(url: str) -> DatabaseBackend:
    """
    Auto-detect and instantiate the correct backend from a database URL.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme or "postgresql"

    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql"

    if scheme not in _BACKEND_REGISTRY:
        _try_import_backend(scheme)

    if scheme not in _BACKEND_REGISTRY:
        supported = ", ".join(sorted(_BACKEND_REGISTRY.keys())) or "none"
        raise ConfigError(
            f"Unknown database scheme '{scheme}'. Supported: {supported}."
        )

    return _BACKEND_REGISTRY[scheme]()


def _try_import_backend(scheme: str) -> None:
    """
    Attempt to import a built-in backend module to trigger registration.
    """
    module_name = _BUILTIN_BACKENDS.get(scheme)
    if module_name:
        with contextlib.suppress(ImportError):
            importlib.import_module(module_name)


__all__ = [
    "BranchDbManager",
    "DatabaseBackend",
    "SnapshotStrategy",
    "get_backend",
    "register_backend",
]
