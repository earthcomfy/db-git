from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

from db_git.errors import ConfigError
from db_git.repository import (
    common_git_directory,
    configuration_root,
    git_directory,
    worktree_root,
)

VALID_CONNECTION_POLICIES = {"terminate", "fail"}
VALID_MODES = {"shared", "per-branch"}
VALID_STRATEGIES = {"template", "pgdump", "backup"}

_CONFIG_COMMENTS: dict[str, str] = {
    "database_url": (
        "Default branch: seed connection URL (PostgreSQL) or file URL (SQLite).\n"
        "# New branch databases can be copied from this seed\n"
        "# or from another branch's recorded database."
    ),
    "mode": (
        "How db-git manages databases across branches.\n"
        '# "shared": PostgreSQL snapshot/restore into one database on switch\n'
        '# "per-branch": each branch gets its own database'
    ),
    "default_branch": (
        "The default branch whose database keeps the original name from database_url.\n"
        "# PostgreSQL uses hashed branch names; SQLite uses unique file generations."
    ),
    "strategy": (
        "Strategy for cloning databases.\n"
        '# "backup": SQLite online backup into a new branch file\n'
        '# "template": uses CREATE DATABASE ... TEMPLATE '
        "(fast, requires CREATEDB privilege)\n"
        '# "pgdump": uses pg_dump/pg_restore (slower, restore/clone requires CREATEDB)'
    ),
    "on_active_connections": (
        "What to do when active connections block a database operation.\n"
        '# "terminate": kill connections and proceed '
        "(PostgreSQL only; needs superuser or pg_signal_backend)\n"
        '# "fail": stop with an error (required for SQLite)'
    ),
}


@dataclass
class DbGitConfig:
    """
    db-git configuration.
    """

    database_url: str = ""
    mode: str = "shared"
    default_branch: str = "main"
    strategy: str = ""
    on_active_connections: str = "terminate"
    snapshot_dir: Path = field(default_factory=lambda: Path(".git/db-git/snapshots"))
    max_snapshots: int = 20
    force_terminate_timeout_ms: int = 5000
    backup_timeout_ms: int = 5000


def load_config(
    cli_overrides: dict[str, object] | None = None,
    project_root: Path | None = None,
) -> DbGitConfig:
    """
    Load defaults, then apply file settings, DB_GIT_* overrides, and CLI values.

    DATABASE_URL supplies the seed URL only when the file omits database_url.
    DB_GIT_DATABASE_URL and explicit CLI values can override that seed.
    """
    project = project_root or find_project_root()
    root = configuration_root(project) if project else None
    merged: dict[str, object] = {}

    # Read project settings before applying explicit overrides.
    if root:
        merged.update(load_dotfile_config(root))

    # Application DATABASE_URL must not replace a configured seed connection.
    if "database_url" not in merged and "DATABASE_URL" in os.environ:
        merged["database_url"] = os.environ["DATABASE_URL"]

    # Dedicated db-git environment overrides.
    merged.update(_load_env_vars())

    # Explicit CLI values have highest precedence; omitted options change nothing.
    if cli_overrides:
        for key, value in cli_overrides.items():
            if value is not None:
                merged[key] = value

    config = _build_config(merged)
    if root and "snapshot_dir" not in merged:
        git_dir = git_directory(root)
        if git_dir:
            config.snapshot_dir = common_git_directory(git_dir) / "db-git" / "snapshots"
    if root and not config.snapshot_dir.is_absolute():
        config.snapshot_dir = root / config.snapshot_dir
    if config.database_url.startswith("sqlite:"):
        from db_git.backends.sqlite.urls import database_path, database_url

        config.database_url = database_url(database_path(config.database_url, root))
        if "mode" not in merged:
            config.mode = "per-branch"
        if "strategy" not in merged:
            config.strategy = "backup"
        if "on_active_connections" not in merged:
            config.on_active_connections = "fail"
    _validate_config(config)
    return config


def find_project_root() -> Path | None:
    """Resolve the current checkout root through Git (including linked worktrees)."""
    return worktree_root()


def load_dotfile_config(root: Path) -> dict[str, object]:
    """
    Read raw key/value pairs from .db-git.toml.

    Returns {} if absent; refuses malformed or unreadable configuration.
    """
    dotfile = configuration_root(root) / ".db-git.toml"
    if not dotfile.exists():
        return {}
    try:
        with open(dotfile, "rb") as f:
            data = tomllib.load(f)
        return dict(data)
    except (tomllib.TOMLDecodeError, OSError, UnicodeError) as e:
        raise ConfigError(
            "Cannot read .db-git.toml. Check TOML syntax and permissions."
        ) from e


def _load_env_vars() -> dict[str, object]:
    """
    Load dedicated DB_GIT_* overrides; load_config handles DATABASE_URL fallback.
    """
    env_map: list[tuple[str, str]] = [
        ("DB_GIT_DATABASE_URL", "database_url"),
        ("DB_GIT_MODE", "mode"),
        ("DB_GIT_STRATEGY", "strategy"),
        ("DB_GIT_ON_ACTIVE_CONNECTIONS", "on_active_connections"),
        ("DB_GIT_SNAPSHOT_DIR", "snapshot_dir"),
        ("DB_GIT_MAX_SNAPSHOTS", "max_snapshots"),
        ("DB_GIT_FORCE_TERMINATE_TIMEOUT_MS", "force_terminate_timeout_ms"),
        ("DB_GIT_BACKUP_TIMEOUT_MS", "backup_timeout_ms"),
    ]
    result: dict[str, object] = {}
    for env_key, config_key in env_map:
        value = os.environ.get(env_key)
        if value is not None:
            result[config_key] = value
    return result


def _build_config(merged: dict[str, object]) -> DbGitConfig:
    """
    Apply merged settings to the DbGitConfig defaults.
    """
    config = DbGitConfig()

    if "database_url" in merged:
        config.database_url = str(merged["database_url"])

    if "mode" in merged:
        config.mode = str(merged["mode"])

    if "default_branch" in merged:
        config.default_branch = str(merged["default_branch"])

    if "strategy" in merged:
        config.strategy = str(merged["strategy"])

    if "on_active_connections" in merged:
        config.on_active_connections = str(merged["on_active_connections"])

    if "snapshot_dir" in merged:
        config.snapshot_dir = Path(str(merged["snapshot_dir"]))

    if "max_snapshots" in merged:
        try:
            config.max_snapshots = int(str(merged["max_snapshots"]))
        except ValueError as e:
            raise ConfigError(
                f"Invalid max_snapshots value: {merged['max_snapshots']}"
            ) from e

    if "force_terminate_timeout_ms" in merged:
        try:
            config.force_terminate_timeout_ms = int(
                str(merged["force_terminate_timeout_ms"])
            )
        except ValueError as e:
            raise ConfigError(
                f"Invalid force_terminate_timeout_ms: "
                f"{merged['force_terminate_timeout_ms']}"
            ) from e

    if "backup_timeout_ms" in merged:
        try:
            config.backup_timeout_ms = int(str(merged["backup_timeout_ms"]))
        except ValueError as e:
            raise ConfigError(
                "Invalid backup_timeout_ms; use a positive integer."
            ) from e
    return config


def _validate_config(config: DbGitConfig) -> None:
    """
    Validate configuration values. Raises ConfigError on invalid values.
    """
    if not config.database_url:
        raise ConfigError(
            "No database URL configured. Run 'db-git init' or set DATABASE_URL."
        )

    if (
        config.max_snapshots < 1
        or config.force_terminate_timeout_ms < 1
        or config.backup_timeout_ms < 1
    ):
        raise ConfigError(
            "max_snapshots, force_terminate_timeout_ms, and backup_timeout_ms "
            "must be positive."
        )

    if config.mode not in VALID_MODES:
        raise ConfigError(
            f"Invalid mode '{config.mode}'. "
            f"Must be one of: {', '.join(sorted(VALID_MODES))}."
        )

    if config.strategy not in VALID_STRATEGIES:
        raise ConfigError(
            "Strategy not configured. Run 'db-git init' to set up db-git."
        )

    if config.database_url.startswith("sqlite:"):
        if config.mode != "per-branch" or config.strategy != "backup":
            raise ConfigError(
                "SQLite supports per-branch mode with strategy='backup'; "
                "shared mode is unsupported."
            )
        if config.on_active_connections != "fail":
            raise ConfigError(
                "SQLite cannot terminate application connections; "
                "use on_active_connections='fail'."
            )
    elif config.strategy == "backup":
        raise ConfigError("The backup strategy is only supported by SQLite.")

    if config.on_active_connections not in VALID_CONNECTION_POLICIES:
        raise ConfigError(
            f"Invalid on_active_connections '{config.on_active_connections}'. "
            f"Must be one of: {', '.join(sorted(VALID_CONNECTION_POLICIES))}."
        )


def write_config(project_root: Path, updates: dict[str, object]) -> None:
    """
    Write or update .db-git.toml with the given key-value pairs.
    """
    dotfile = configuration_root(project_root) / ".db-git.toml"

    doc = tomlkit.parse(dotfile.read_text()) if dotfile.exists() else tomlkit.document()

    for key, value in updates.items():
        if key not in doc and key in _CONFIG_COMMENTS:
            doc.add(tomlkit.comment(_CONFIG_COMMENTS[key]))
        doc[key] = value

    dotfile.write_text(tomlkit.dumps(doc))


def ensure_config_ignored(project_root: Path) -> bool:
    """
    Ensure the local db-git config file is ignored by git.
    """
    gitignore = configuration_root(project_root) / ".gitignore"
    entry = ".db-git.toml"

    if gitignore.exists():
        lines = gitignore.read_text().splitlines()
        if entry in {line.strip() for line in lines}:
            return False
        needs_leading_newline = bool(lines) and lines[-1] != ""
        with gitignore.open("a") as f:
            if needs_leading_newline:
                f.write("\n")
            f.write(f"# Local db-git configuration\n{entry}\n")
    else:
        gitignore.write_text(f"# Local db-git configuration\n{entry}\n")
    return True
