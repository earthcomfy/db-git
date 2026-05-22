from __future__ import annotations

import subprocess
import sys

import typer

from git_db.git import get_current_branch

from ._console import console


def resolve_with_prompt(
    flag_value: str | None,
    existing: object | None,
    prompt_text: str,
    required: bool = False,
) -> str:
    """
    Resolve a string value from flag, existing config, or prompt.
    """
    if flag_value is not None:
        return flag_value
    if existing:
        return str(existing)
    if required and sys.stdin.isatty():
        return typer.prompt(prompt_text)
    return ""


def resolve_choice(
    flag_value: str | None,
    existing: object | None,
    prompt_text: str,
    choices: dict[str, str],
    labels: dict[str, str],
    default: str,
) -> str:
    """
    Resolve a choice from flag, existing config, or interactive prompt.
    """
    if flag_value is not None:
        if flag_value in choices.values():
            return flag_value
        console.print(f"[red]Error:[/] Invalid value '{flag_value}'.")
        raise typer.Exit(1)

    reverse = {v: k for k, v in choices.items()}
    existing_key = reverse.get(str(existing)) if existing else None
    default_key = existing_key or default
    default_value = choices[default_key]

    if not sys.stdin.isatty():
        return (
            str(existing)
            if existing and str(existing) in choices.values()
            else default_value
        )

    console.print(f"  {prompt_text}:\n")
    for key, label in labels.items():
        marker = " (current)" if key == existing_key else ""
        console.print(f"    {key}. {label}{marker}")

    console.print()
    result = typer.prompt("  Choice", default=default_key)
    result = str(result).strip()
    console.print()

    if result in choices:
        return choices[result]
    if result in choices.values():
        return result
    return default_value


def confirm_prune(
    header: str,
    items: list[tuple[str, str | None]],
    yes: bool,
) -> bool:
    """
    Display items to be pruned and ask for confirmation.
    """
    console.print(header)
    for name, subtitle in items:
        if subtitle:
            console.print(f"  {name}  [dim]({subtitle})[/]")
        else:
            console.print(f"  {name}")
    console.print()

    if yes:
        return True

    if not sys.stdin.isatty():
        console.print(
            "[red]Error:[/] Refusing to prune in non-interactive mode. "
            "Pass [cyan]--yes[/] to proceed, or [cyan]--dry-run[/] to preview."
        )
        raise typer.Exit(1)

    return typer.confirm("This is irreversible. Continue?", default=False)


def detect_default_branch() -> str:
    """
    Detect the default branch name from git.
    """
    result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0:
        ref = result.stdout.strip()
        return ref.split("/")[-1]

    current = get_current_branch()
    return current or "main"
