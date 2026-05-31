from __future__ import annotations

from typing import Annotated

import typer
from rich.panel import Panel
from rich.table import Table

from git_db.backends import DatabaseBackend, SnapshotStrategy, get_backend
from git_db.config import GitDbConfig, load_config
from git_db.db import parse_database_url
from git_db.errors import GitDbError
from git_db.git import get_current_branch, get_git_dir, list_branches
from git_db.state import load_state
from git_db.storage import (
    branch_db_name,
    has_snapshot,
    identify_stale_snapshots,
    list_snapshots,
    snapshot_db_name,
    snapshot_dump_path,
)

from ._common import check_enabled, debug_enabled, require_init
from ._console import app, console
from ._format import format_age, format_size, mask_url
from ._prompts import confirm_prune


@app.command("list")
def list_cmd(
    database_url: Annotated[
        str | None, typer.Option("--database-url", envvar="DATABASE_URL")
    ] = None,
) -> None:
    """
    Show all stored snapshots or branch databases.
    """
    require_init()
    try:
        config = load_config(cli_overrides={"database_url": database_url})
    except GitDbError as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(1) from e

    if config.mode == "per-branch":
        _list_per_branch(config)
        return

    snapshots = list_snapshots(config.snapshot_dir)
    if not snapshots:
        console.print("No snapshots found.")
        return

    table = Table(title="Snapshots")
    table.add_column("Branch", style="cyan", no_wrap=True)
    table.add_column("Strategy", style="green")
    table.add_column("Size", justify="right", style="magenta")
    table.add_column("Status", justify="center")
    table.add_column("Age", justify="right")
    table.add_column("Database", style="dim")

    backend = get_backend(config.database_url)

    for s in snapshots:
        table.add_row(
            s.branch,
            s.strategy,
            format_size(s.file_size_bytes),
            _shared_snapshot_status(config, backend, s.branch, s.strategy),
            format_age(s.created_at),
            s.database,
        )

    console.print(table)


@app.command()
def prune(
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip confirmation prompt.")
    ] = False,
    database_url: Annotated[
        str | None, typer.Option("--database-url", envvar="DATABASE_URL")
    ] = None,
) -> None:
    """
    Remove snapshots or branch databases for deleted branches.

    Snapshots are pruned in shared mode; branch databases in per-branch mode.
    """
    require_init()
    try:
        config = load_config(cli_overrides={"database_url": database_url})

        if config.mode == "per-branch":
            _prune_per_branch(config, dry_run, yes)
            return

        existing = list_branches()
        stale = identify_stale_snapshots(
            config.snapshot_dir,
            config.max_snapshots,
            existing,
        )

        if not stale:
            console.print("Nothing to prune.")
            return

        if dry_run:
            for meta in stale:
                console.print(f"  [dim]Would remove:[/] {meta.branch}")
            return

        if not confirm_prune(
            "The following snapshots will be removed:",
            [(m.branch, None) for m in stale],
            yes,
        ):
            return

        backend = get_backend(config.database_url)
        strategy = backend.detect_strategy(config)
        pruned = 0

        for meta in stale:
            try:
                strategy.cleanup(meta.branch, config.snapshot_dir, config)
                console.print(f"  Pruned: {meta.branch}")
                pruned += 1
            except Exception as e:
                console.print(f"  [yellow]Failed to prune {meta.branch}:[/] {e}")

        if pruned:
            console.print(f"\nRemoved {pruned} snapshot(s).")
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


@app.command()
def status(
    database_url: Annotated[
        str | None, typer.Option("--database-url", envvar="DATABASE_URL")
    ] = None,
) -> None:
    """
    Show current strategy, database, branch, and snapshot info.
    """
    require_init()
    try:
        config = load_config(cli_overrides={"database_url": database_url})

        current_branch = get_current_branch() or "(detached)"
        backend = get_backend(config.database_url)
        version = backend.get_engine_version(config.database_url)
        detected = backend.detect_strategy(config)

        if config.mode == "per-branch":
            _status_per_branch(config, current_branch, backend, version, detected)
            return

        snapshots = list_snapshots(config.snapshot_dir)
        has_current = (
            has_snapshot(config.snapshot_dir, current_branch)
            if current_branch != "(detached)"
            else False
        )

        current_status = "[green]yes[/]" if has_current else "[dim]no snapshot[/]"
        enabled_status = check_enabled()
        summary = (
            f"  Branch:     [cyan]{current_branch}[/]\n"
            f"  Database:   {mask_url(config.database_url)}\n"
            f"  Engine:     [cyan]{backend.engine} {version}[/]\n"
            f"  Strategy:   [green]{detected.name}[/]\n"
            f"  Snapshots:  {len(snapshots)}\n"
            f"  Current:    {current_status}\n"
            f"  Enabled:    {enabled_status}"
        )
        console.print(Panel(summary, title="git-db status", border_style="blue"))
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


def _list_per_branch(config: GitDbConfig) -> None:
    """
    List branch databases in per-branch mode.
    """
    git_dir = get_git_dir()
    if git_dir is None:
        console.print("[red]Error:[/] Not inside a git repository.")
        raise typer.Exit(1)

    backend = get_backend(config.database_url)
    manager = backend.branch_db_manager(config)
    entries = manager.list(git_dir)

    if not entries:
        console.print("No branch databases found.")
        return

    table = Table(title="Branch Databases")
    table.add_column("Branch", style="cyan", no_wrap=True)
    table.add_column("Database", style="green")
    table.add_column("Status", justify="center")
    table.add_column("Created From", style="dim")
    table.add_column("Age", justify="right")

    for branch_name, entry, exists in entries:
        status_str = "[green]exists[/]" if exists else "[red]missing[/]"
        table.add_row(
            branch_name,
            entry.db_name,
            status_str,
            entry.created_from,
            format_age(entry.created_at),
        )

    params = backend.apply_url_defaults(parse_database_url(config.database_url))
    seed_name = str(params["dbname"])
    table.add_row(
        f"{config.default_branch} (seed)",
        seed_name,
        "[green]seed[/]",
        "n/a",
        "n/a",
    )

    console.print(table)


def _prune_per_branch(config: GitDbConfig, dry_run: bool, yes: bool) -> None:
    """
    Prune stale branch databases in per-branch mode.
    """
    git_dir = get_git_dir()
    if git_dir is None:
        console.print("[red]Error:[/] Not inside a git repository.")
        raise typer.Exit(1)

    existing_branches = list_branches()
    state = load_state(git_dir)

    if not state.databases:
        console.print("No branch databases to prune.")
        return

    stale = [
        (branch, entry)
        for branch, entry in state.databases.items()
        if branch not in existing_branches
    ]

    if not stale:
        console.print("Nothing to prune.")
        return

    if dry_run:
        for branch_name, entry in stale:
            console.print(f"  [dim]Would drop:[/] {entry.db_name} ({branch_name})")
        return

    if not confirm_prune(
        "The following branch databases will be dropped:",
        [
            (entry.db_name, f"{branch_name}, created from {entry.created_from}")
            for branch_name, entry in stale
        ],
        yes,
    ):
        return

    backend = get_backend(config.database_url)
    manager = backend.branch_db_manager(config)

    pruned = 0
    for branch_name, entry in stale:
        try:
            manager.drop(entry.db_name, branch_name, git_dir)
            console.print(f"  Dropped: {entry.db_name} ({branch_name})")
            pruned += 1
        except GitDbError as e:
            console.print(f"  [yellow]Failed to drop {entry.db_name}:[/] {e}")

    if pruned:
        console.print(f"\nDropped {pruned} database(s).")


def _status_per_branch(
    config: GitDbConfig,
    current_branch: str,
    backend: DatabaseBackend,
    version: int,
    detected: SnapshotStrategy,
) -> None:
    """
    Show status in per-branch mode.
    """
    git_dir = get_git_dir()

    params = backend.apply_url_defaults(parse_database_url(config.database_url))
    dbname = str(params["dbname"])

    if current_branch != "(detached)":
        current_db = branch_db_name(
            current_branch,
            dbname,
            config.default_branch,
            backend.max_identifier_length,
        )
        db_exists = backend.branch_db_manager(config).exists(current_db)
        db_status = "[green]exists[/]" if db_exists else "[dim]not created[/]"
    else:
        current_db = "(detached)"
        db_status = "[dim]N/A[/]"

    total_dbs = 0
    if git_dir:
        state = load_state(git_dir)
        total_dbs = len(state.databases)

    count_warning = ""
    if total_dbs > 20:
        count_warning = " [yellow](consider running git-db prune)[/]"

    enabled_status = check_enabled()
    summary = (
        f"  Mode:       [green]per-branch[/]\n"
        f"  Branch:     [cyan]{current_branch}[/]\n"
        f"  Database:   [cyan]{current_db}[/]  {db_status}\n"
        f"  Seed:       {dbname}\n"
        f"  Default:    {config.default_branch}\n"
        f"  Engine:     [cyan]{backend.engine} {version}[/]\n"
        f"  Strategy:   [green]{detected.name}[/]\n"
        f"  Databases:  {total_dbs}{count_warning}\n"
        f"  Enabled:    {enabled_status}"
    )
    console.print(Panel(summary, title="git-db status", border_style="blue"))


def _shared_snapshot_status(
    config: GitDbConfig,
    backend: DatabaseBackend,
    branch: str,
    strategy_name: str,
) -> str:
    """
    Return whether shared-mode snapshot storage still exists.
    """
    if strategy_name == "pgdump":
        return (
            "[green]exists[/]"
            if snapshot_dump_path(config.snapshot_dir, branch).exists()
            else "[red]missing[/]"
        )

    if strategy_name != "template":
        return "[dim]unknown[/]"

    params = backend.apply_url_defaults(parse_database_url(config.database_url))
    dbname = str(params["dbname"])
    name = snapshot_db_name(branch, dbname, backend.max_identifier_length)
    try:
        return (
            "[green]exists[/]"
            if backend.database_exists(config.database_url, name)
            else "[red]missing[/]"
        )
    except Exception:
        return "[yellow]unknown[/]"
