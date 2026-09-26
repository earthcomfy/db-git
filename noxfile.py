from __future__ import annotations

import os
import shutil
from pathlib import Path

import nox

PYTHONS = ["3.12", "3.13"]
PG_IMAGES = [
    "postgres:13",
    "postgres:14",
    "postgres:15",
    "postgres:16",
    "postgres:17",
]

nox.options.default_venv_backend = "uv"
nox.options.reuse_existing_virtualenvs = True


def _install(session: nox.Session) -> None:
    """Sync the project + dev dependency group into the session venv."""
    session.run_install(
        "uv",
        "sync",
        "--group",
        "dev",
        env={"UV_PROJECT_ENVIRONMENT": session.virtualenv.location},
    )


@nox.session(python=PYTHONS)
def unit(session: nox.Session) -> None:
    """Unit and SQLite workflow tests: no Docker."""
    _install(session)
    session.run("pytest", "tests/unit", "tests/sqlite", "-q", *session.posargs)


@nox.session(python=PYTHONS)
@nox.parametrize("pg_image", PG_IMAGES)
def integration(session: nox.Session, pg_image: str) -> None:
    """Integration + E2E tests against a Postgres container."""
    if shutil.which("docker") is None:
        session.skip("docker not available")
    _install(session)
    session.env["DB_GIT_TEST_PG_IMAGE"] = pg_image
    # Debian/Ubuntu's generic pg_wrapper can select a newer installed client.
    # Use the matrix version directly when its versioned binaries are available.
    client_dir = Path("/usr/lib/postgresql") / pg_image.split(":", 1)[1] / "bin"
    if client_dir.is_dir():
        session.env["PATH"] = f"{client_dir}{os.pathsep}{os.environ['PATH']}"
    for tool in ("pg_dump", "pg_restore"):
        executable = str(client_dir / tool) if client_dir.is_dir() else tool
        session.run(executable, "--version", external=True)
    session.run(
        "pytest",
        "tests/integration",
        "tests/e2e",
        "-q",
        *session.posargs,
    )


@nox.session(python="3.12")
def types(session: nox.Session) -> None:
    """mypy over the source tree."""
    _install(session)
    session.run("mypy", "src/")


@nox.session(python="3.12")
def lint(session: nox.Session) -> None:
    """ruff check + format verify."""
    _install(session)
    session.run("ruff", "check", "src/", "tests/")
    session.run("ruff", "format", "--check", "src/", "tests/")


@nox.session(python=PYTHONS)
@nox.parametrize("mysql_image", ["mysql:8.0", "mysql:8.4"])
def mysql(session: nox.Session, mysql_image: str) -> None:
    """MySQL shared/per-branch workflows, stored objects, and recovery."""
    if shutil.which("docker") is None:
        session.skip("docker not available")
    _install(session)
    session.install("PyMySQL[rsa]>=1.1.1")
    session.env["DB_GIT_TEST_MYSQL_IMAGE"] = mysql_image
    session.run("mysql", "--version", external=True)
    session.run("mysqldump", "--version", external=True)
    session.run("pytest", "tests/mysql", "-q", *session.posargs)
