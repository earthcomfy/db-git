from __future__ import annotations

import json
from dataclasses import asdict
from typing import Annotated

import typer
from rich.table import Table

from db_git.config import load_config
from db_git.errors import DbGitError
from db_git.git import get_current_branch
from db_git.history import (
    create_checkpoint,
    list_checkpoints,
    prune_checkpoints,
    require_history,
    retention_plan,
)

from ._common import require_init
from ._console import app, console
from ._format import format_age, format_size


@app.command()
def checkpoint(
    name: Annotated[
        str | None,
        typer.Argument(help="Optional immutable name, unique within this branch."),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option(
            "--branch",
            help="Branch label for this working copy; defaults to the current branch.",
        ),
    ] = None,
) -> None:
    """Save an immutable shared-mode checkpoint without replacing a branch snapshot."""
    require_init()
    try:
        config = load_config()
        require_history(config)
        branch = branch or get_current_branch()
        if branch is None:
            raise DbGitError("HEAD is detached. Specify --branch for the checkpoint.")
        meta = create_checkpoint(config, branch, name)
        console.print(
            f"Created checkpoint {meta.checkpoint_id} for '{branch}'"
            + (f" ({name})" if name else ""),
            markup=False,
        )
        console.print(
            "Inspect with db-git history; restore with "
            "db-git restore --checkpoint <name-or-id>.",
            markup=False,
        )
    except (DbGitError, OSError) as e:
        console.print(f"Error: {e}", markup=False)
        raise typer.Exit(1) from e


@app.command()
def history(
    branch: Annotated[
        str | None,
        typer.Argument(help="Filter by branch; omitted lists all checkpoints."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print history as JSON (read-only).")
    ] = False,
    prune: Annotated[
        bool,
        typer.Option(
            "--prune", help="Retire older checkpoints through recoverable operations."
        ),
    ] = False,
    keep: Annotated[
        int | None,
        typer.Option(
            "--keep",
            min=1,
            help="Newest checkpoints to keep per branch; defaults to 20 when pruning.",
        ),
    ] = None,
    include_named: Annotated[
        bool,
        typer.Option(
            "--include-named",
            help="Allow pruning named checkpoints too; newest copies still stay.",
        ),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip retention confirmation.")
    ] = False,
) -> None:
    """Inspect checkpoint history, or explicitly prune older copies with --prune."""
    require_init()
    try:
        config = load_config()
        require_history(config)
        if not prune and (keep is not None or include_named or dry_run or yes):
            raise DbGitError("Retention options require --prune.")
        if prune and json_output:
            raise DbGitError(
                "Use --json to inspect history, or --prune to apply retention."
            )
        entries = list_checkpoints(config.snapshot_dir, branch)
        if json_output:
            typer.echo(
                json.dumps(
                    {"schema_version": 1, "checkpoints": [asdict(m) for m in entries]},
                    indent=2,
                )
            )
            return
        if prune:
            limit = keep if keep is not None else 20
            selected = retention_plan(entries, limit, include_named)
            if not selected:
                console.print(
                    "No checkpoints eligible for pruning. Named checkpoints are "
                    "protected unless --include-named is used."
                )
                return
            for meta in selected:
                console.print(
                    f"Would retire: {meta.checkpoint_id}  {meta.branch}  "
                    f"{meta.checkpoint_name or '(unnamed)'}",
                    markup=False,
                )
            if dry_run:
                return
            if not yes and not typer.confirm(
                "Retire these checkpoints, keeping recoverable backups?"
            ):
                return
            prune_checkpoints(
                config,
                {m.checkpoint_id for m in selected if m.checkpoint_id is not None},
                keep=limit,
                branch=branch,
                include_named=include_named,
            )
            console.print(
                f"Retired {len(selected)} checkpoint(s). Backups remain until "
                "explicitly discarded through db-git recover."
            )
            return
        if not entries:
            console.print(
                "No checkpoints yet. Create one with db-git checkpoint [name]."
            )
            return
        table = Table(title="Checkpoint history (newest first)")
        for column in ("ID", "Branch", "Name", "Strategy", "Size", "Age"):
            table.add_column(column, no_wrap=column == "ID")
        for meta in entries:
            table.add_row(
                (meta.checkpoint_id or "")[:12],
                meta.branch,
                meta.checkpoint_name or "(unnamed)",
                meta.strategy,
                format_size(meta.file_size_bytes),
                format_age(meta.created_at),
            )
        console.print(table)
        console.print(
            "Named checkpoints are protected from retention by default. "
            "Use --json for complete identifiers."
        )
    except (DbGitError, OSError) as e:
        console.print(f"Error: {e}", markup=False)
        raise typer.Exit(1) from e
