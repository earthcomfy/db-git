from __future__ import annotations

import shutil

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
    """Unit tests: no Docker."""
    _install(session)
    session.run("pytest", "tests/unit", "-q", *session.posargs)


@nox.session(python=PYTHONS)
@nox.parametrize("pg_image", PG_IMAGES)
def integration(session: nox.Session, pg_image: str) -> None:
    """Integration + E2E tests against a Postgres container."""
    if shutil.which("docker") is None:
        session.skip("docker not available")
    _install(session)
    session.env["GITDB_TEST_PG_IMAGE"] = pg_image
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
