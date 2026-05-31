from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

from git_db.errors import ConfigError

VALID_CONNECTION_POLICIES = {"terminate", "fail"}
VALID_MODES = {"shared", "per-branch"}
VALID_STRATEGIES = {"template", "pgdump"}

_CONFIG_COMMENTS: dict[str, str] = {
    "database_url": "Database connection URL.",
    "mode": (
        "How git-db manages databases across branches.\n"
        '# "shared": one database, snapshot/restore on switch\n'
        '# "per-branch": each branch gets its own database'
    ),
    "default_branch": (
        "The default branch whose database keeps the original name from DATABASE_URL.\n"
        "# Other branches get suffixed names (e.g., myapp__feature_auth)."
    ),
    "strategy": (
        "Snapshot strategy for cloning databases.\n"
        '# "template": uses CREATE DATABASE ... TEMPLATE '
        "(fast, requires CREATEDB privilege)\n"
        '# "pgdump": uses pg_dump/pg_restore (slower, works without special privileges)'
    ),
    "on_active_connections": (
        "What to do when active connections block a database operation.\n"
        '# "terminate": kill connections and proceed '
        "(needs superuser or pg_signal_backend)\n"
        '# "fail": stop with an error'
    ),
}


@dataclass
class GitDbConfig:
    """
    git-db configuration.
    """

    database_url: str = ""
    mode: str = "shared"
    default_branch: str = "main"
    strategy: str = ""
    on_active_connections: str = "terminate"
    snapshot_dir: Path = field(default_factory=lambda: Path(".git/git-db/snapshots"))
    max_snapshots: int = 20
    force_terminate_timeout_ms: int = 5000


def load_config(
    cli_overrides: dict[str, object] | None = None,
    project_root: Path | None = None,
) -> GitDbConfig:
    """
    Load configuration with precedence: defaults < .git-db.toml < env vars < CLI.
    """
    root = project_root or find_project_root()
    merged: dict[str, object] = {}

    # Layer 1: .git-db.toml
    if root:
        merged.update(load_dotfile_config(root))

    # Layer 3: environment variables
    merged.update(_load_env_vars())

    # Layer 4: CLI overrides
    if cli_overrides:
        for key, value in cli_overrides.items():
            if value is not None:
                merged[key] = value

    config = _build_config(merged)
    _validate_config(config)
    return config


def find_project_root() -> Path | None:
    """
    Walk up from cwd looking for a .git directory.
    """
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def load_dotfile_config(root: Path) -> dict[str, object]:
    """
    Read raw key/value pairs from .git-db.toml.

    Returns {} if absent or unparseable.
    """
    dotfile = root / ".git-db.toml"
    if not dotfile.exists():
        return {}
    try:
        with open(dotfile, "rb") as f:
            data = tomllib.load(f)
        return dict(data)
    except (tomllib.TOMLDecodeError, OSError):
        return {}


def _load_env_vars() -> dict[str, object]:
    """
    Load configuration from environment variables.
    """
    env_map: list[tuple[str, str]] = [
        ("DATABASE_URL", "database_url"),
        ("GIT_DB_DATABASE_URL", "database_url"),
        ("GIT_DB_MODE", "mode"),
        ("GIT_DB_STRATEGY", "strategy"),
        ("GIT_DB_ON_ACTIVE_CONNECTIONS", "on_active_connections"),
        ("GIT_DB_SNAPSHOT_DIR", "snapshot_dir"),
        ("GIT_DB_MAX_SNAPSHOTS", "max_snapshots"),
        ("GIT_DB_FORCE_TERMINATE_TIMEOUT_MS", "force_terminate_timeout_ms"),
    ]
    result: dict[str, object] = {}
    for env_key, config_key in env_map:
        value = os.environ.get(env_key)
        if value is not None:
            result[config_key] = value
    return result


def _build_config(merged: dict[str, object]) -> GitDbConfig:
    """
    Build a GitDbConfig from the merged configuration dict.
    """
    config = GitDbConfig()

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

    return config


def _validate_config(config: GitDbConfig) -> None:
    """
    Validate configuration values. Raises ConfigError on invalid values.
    """
    if not config.database_url:
        raise ConfigError(
            "No database URL configured. Run 'git-db init' or set DATABASE_URL."
        )

    if config.mode not in VALID_MODES:
        raise ConfigError(
            f"Invalid mode '{config.mode}'. "
            f"Must be one of: {', '.join(sorted(VALID_MODES))}."
        )

    if config.strategy not in VALID_STRATEGIES:
        raise ConfigError(
            "Strategy not configured. Run 'git-db init' to set up git-db."
        )

    if config.on_active_connections not in VALID_CONNECTION_POLICIES:
        raise ConfigError(
            f"Invalid on_active_connections '{config.on_active_connections}'. "
            f"Must be one of: {', '.join(sorted(VALID_CONNECTION_POLICIES))}."
        )


def write_config(project_root: Path, updates: dict[str, object]) -> None:
    """
    Write or update .git-db.toml with the given key-value pairs.
    """
    dotfile = project_root / ".git-db.toml"

    doc = tomlkit.parse(dotfile.read_text()) if dotfile.exists() else tomlkit.document()

    for key, value in updates.items():
        if key not in doc and key in _CONFIG_COMMENTS:
            doc.add(tomlkit.comment(_CONFIG_COMMENTS[key]))
        doc[key] = value

    dotfile.write_text(tomlkit.dumps(doc))


def ensure_config_ignored(project_root: Path) -> bool:
    """
    Ensure the local git-db config file is ignored by git.
    """
    gitignore = project_root / ".gitignore"
    entry = ".git-db.toml"

    if gitignore.exists():
        lines = gitignore.read_text().splitlines()
        if entry in {line.strip() for line in lines}:
            return False
        needs_leading_newline = bool(lines) and lines[-1] != ""
        with gitignore.open("a") as f:
            if needs_leading_newline:
                f.write("\n")
            f.write(f"# Local git-db configuration\n{entry}\n")
    else:
        gitignore.write_text(f"# Local git-db configuration\n{entry}\n")
    return True
