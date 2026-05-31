from __future__ import annotations

import sys
from enum import StrEnum
from typing import Annotated

import typer
from rich.panel import Panel

from git_db.backends import get_backend
from git_db.backends.postgresql.backend import PgPermissions
from git_db.config import (
    find_project_root,
    load_config,
    load_dotfile_config,
    write_config,
)
from git_db.db import parse_database_url
from git_db.errors import DatabaseError, GitDbError
from git_db.git import get_git_dir, install_hook
from git_db.storage import ensure_snapshot_dir

from ._common import debug_enabled
from ._console import app, console
from ._prompts import (
    detect_default_branch,
    resolve_choice,
    resolve_with_prompt,
)


class ModeChoice(StrEnum):
    shared = "shared"
    per_branch = "per-branch"


class StrategyChoice(StrEnum):
    template = "template"
    pgdump = "pgdump"


class ConnectionPolicyChoice(StrEnum):
    terminate = "terminate"
    fail = "fail"


@app.command()
def init(
    database_url: Annotated[
        str | None, typer.Option("--database-url", envvar="DATABASE_URL")
    ] = None,
    mode: Annotated[ModeChoice | None, typer.Option("--mode")] = None,
    strategy: Annotated[StrategyChoice | None, typer.Option("--strategy")] = None,
    on_active_connections: Annotated[
        ConnectionPolicyChoice | None, typer.Option("--on-active-connections")
    ] = None,
    no_hook: Annotated[bool, typer.Option("--no-hook")] = False,
) -> None:
    """
    Initialize git-db in the current repo.
    """
    git_dir = get_git_dir()
    if git_dir is None:
        console.print(
            "[red]Error:[/] Not inside a git repository. "
            "Run this from a git project root."
        )
        raise typer.Exit(1)

    try:
        project_root = find_project_root()
        if project_root is None:
            console.print("[red]Error:[/] Could not find project root.")
            raise typer.Exit(1)

        existing_config = load_dotfile_config(project_root)
        is_reinit = (project_root / ".git-db.toml").exists()
        if is_reinit:
            console.print("[dim]Updating existing configuration.[/]\n")

        resolved_url = resolve_with_prompt(
            flag_value=database_url,
            existing=existing_config.get("database_url"),
            prompt_text="Database URL",
            required=True,
        )
        if not resolved_url:
            console.print(
                "[red]Error:[/] No database URL configured. "
                "Pass --database-url or set DATABASE_URL."
            )
            raise typer.Exit(1)

        backend = get_backend(resolved_url)
        params = backend.apply_url_defaults(parse_database_url(resolved_url))
        permissions: PgPermissions | None = None
        version: int | None = None

        try:
            version = backend.get_engine_version(resolved_url)
        except DatabaseError as e:
            console.print(
                f"[red]Error:[/] Could not connect to database: {e}\n"
                "  Fix the database URL or database server, then rerun "
                "[cyan]git-db init[/].\n"
            )
            raise typer.Exit(1) from e

        console.print(
            f"  Detected: [cyan]PostgreSQL {version}[/] "
            f"at {params['host']}:{params['port']}"
        )
        console.print(f"  Database:  [cyan]{params['dbname']}[/]")
        console.print(f"  User:      [cyan]{params['user']}[/]")

        result = backend.check_permissions(resolved_url)
        assert isinstance(result, PgPermissions)
        permissions = result
        console.print("\n  Checking permissions...")
        console.print(
            f"    CREATEDB:          "
            f"{'[green]Yes[/]' if permissions.can_createdb else '[red]No[/]'}"
        )
        console.print(
            f"    Superuser:         "
            f"{'[green]Yes[/]' if permissions.is_superuser else '[red]No[/]'}"
        )
        sig_label = (
            "[green]Yes[/]" if permissions.has_pg_signal_backend else "[red]No[/]"
        )
        console.print(f"    pg_signal_backend: {sig_label}")
        console.print()

        resolved_mode = resolve_choice(
            flag_value=mode.value if mode is not None else None,
            existing=existing_config.get("mode"),
            prompt_text="How should git-db manage database state across branches?",
            choices={"1": "shared", "2": "per-branch"},
            labels={
                "1": "Single database: snapshot/restore on switch",
                "2": "Per-branch databases: each branch gets its own DB",
            },
            default="1",
        )

        resolved_default_branch = existing_config.get("default_branch", "")
        if resolved_mode == "per-branch" and not resolved_default_branch:
            resolved_default_branch = detect_default_branch()

        strategy_labels = {
            "1": "template: fast, uses CREATE DATABASE ... TEMPLATE",
            "2": "pgdump: uses pg_dump/pg_restore",
        }
        strategy_default = "1"

        if permissions and not permissions.can_createdb:
            strategy_labels["1"] += " [red](requires CREATEDB)[/]"

        resolved_strategy = resolve_choice(
            flag_value=strategy.value if strategy is not None else None,
            existing=existing_config.get("strategy"),
            prompt_text="Snapshot strategy",
            choices={"1": "template", "2": "pgdump"},
            labels=strategy_labels,
            default=strategy_default,
        )

        if (
            resolved_strategy == "template"
            and permissions
            and not permissions.can_createdb
        ):
            console.print(
                "[yellow]Warning:[/] Your user lacks CREATEDB privilege. "
                "Template strategy will fail.\n"
                "  Grant it with: ALTER ROLE {user} CREATEDB;\n"
                "  Or switch to pgdump strategy.\n"
            )

        policy_labels = {
            "1": "terminate: kill connections and proceed",
            "2": "fail: stop with an error",
        }
        resolved_policy = resolve_choice(
            flag_value=(
                on_active_connections.value
                if on_active_connections is not None
                else None
            ),
            existing=existing_config.get("on_active_connections"),
            prompt_text="When active connections prevent database operations",
            choices={"1": "terminate", "2": "fail"},
            labels=policy_labels,
            default="1",
        )

        if (
            resolved_policy == "terminate"
            and permissions
            and not permissions.is_superuser
            and not permissions.has_pg_signal_backend
        ):
            console.print(
                "[yellow]Warning:[/] Your user lacks superuser and "
                "pg_signal_backend privileges.\n"
                "  Terminate may fail silently on connections owned "
                "by other users.\n"
            )

        install_hook_flag = not no_hook
        if not no_hook and mode is None and sys.stdin.isatty():
            install_hook_flag = typer.confirm(
                "Install git post-checkout hook?", default=True
            )

        config_updates: dict[str, object] = {
            "database_url": resolved_url,
            "mode": resolved_mode,
            "strategy": resolved_strategy,
            "on_active_connections": resolved_policy,
        }
        if resolved_mode == "per-branch":
            config_updates["default_branch"] = resolved_default_branch

        write_config(project_root, config_updates)
        console.print("[dim]Configuration saved to .git-db.toml[/]\n")

        hook_status = "[dim]skipped[/]"
        if install_hook_flag:
            install_hook(git_dir)
            ensure_snapshot_dir(
                load_config(
                    cli_overrides={"database_url": resolved_url},
                    project_root=project_root,
                ).snapshot_dir
            )
            hook_status = "[green]installed[/]"

        summary = (
            f"  Engine:     [cyan]{backend.engine}[/]\n"
            f"  Version:    [cyan]{version or 'unknown'}[/]\n"
            f"  Mode:       [green]{resolved_mode}[/]\n"
            f"  Strategy:   [green]{resolved_strategy}[/]\n"
            f"  Policy:     [green]{resolved_policy}[/]\n"
            f"  Hook:       {hook_status}"
        )
        if resolved_mode == "per-branch":
            summary += f"\n  Default:    [cyan]{resolved_default_branch}[/]"

        console.print(Panel(summary, title="git-db initialized", border_style="green"))

    except GitDbError as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(1) from e
    except typer.Exit:
        raise
    except Exception as e:
        if debug_enabled():
            raise
        console.print(
            f"[red]Error:[/] Unexpected error: {e}\n"
            "[dim]Set GIT_DB_DEBUG=1 to see the full traceback.[/]"
        )
        raise typer.Exit(1) from e
