from __future__ import annotations

from pathlib import Path

from db_git.backends.mysql.backend import MySQLBackend
from db_git.backends.mysql.connections import check_clients
from db_git.config import ensure_config_ignored, load_config, write_config
from db_git.db import parse_database_url
from db_git.errors import ConfigError
from db_git.git import install_hook
from db_git.repository import require_safe_shared_mode

from ._console import console
from ._init_resources import has_resources, same_mysql_seed
from ._prompts import detect_default_branch


def initialize(
    root: Path,
    git_dir: Path,
    url: str,
    existing: dict[str, object],
    *,
    mode: str | None,
    strategy: str | None,
    policy: str | None,
    no_hook: bool,
) -> None:
    if strategy not in {None, "mysqldump"}:
        raise ConfigError("MySQL requires --strategy mysqldump.")
    if policy not in {None, "fail"}:
        raise ConfigError("MySQL requires --on-active-connections fail.")
    resources_exist = has_resources(root, git_dir, existing)
    if resources_exist and not same_mysql_seed(
        str(existing.get("database_url") or ""), url
    ):
        raise ConfigError(
            "Existing db-git resources belong to another seed or engine. "
            "Keep their configuration for recovery; initialize MySQL in a "
            "separate repository."
        )
    selected_mode = mode or str(existing.get("mode") or "per-branch")
    require_safe_shared_mode(selected_mode)
    if resources_exist and selected_mode != str(existing.get("mode") or selected_mode):
        raise ConfigError(
            "Existing MySQL resources must keep their mode for recovery. "
            "Use a separate repository for a different mode."
        )
    updates: dict[str, object] = {
        "database_url": url,
        "mode": selected_mode,
        "strategy": "mysqldump",
        "on_active_connections": "fail",
        "default_branch": existing.get("default_branch") or detect_default_branch(root),
    }
    config = load_config(updates, project_root=root)
    backend = MySQLBackend()
    version = backend.get_engine_version(url)
    check_clients()
    backend.check_permissions(url)
    params = backend.apply_url_defaults(parse_database_url(url))
    if not backend.database_exists(url, str(params["dbname"])):
        raise ConfigError("MySQL seed database does not exist.")
    backend.detect_strategy(config)
    write_config(root, updates)
    ensure_config_ignored(root)
    if not no_hook:
        install_hook(git_dir)
    console.print(
        f"Initialized MySQL {version} in {selected_mode} mode.\n"
        f"Configuration: {root / '.db-git.toml'}",
        markup=False,
    )
    console.print(
        "Restore, reset, and rollback select a database generation. Restart "
        "applications through db-git run afterward. Previous "
        "databases are retained for manual cleanup."
    )
    console.print(
        "Start your application: db-git run -- <command>\n"
        "Show its connection URL: db-git url\n"
        "Check your setup: db-git doctor",
        markup=False,
    )
