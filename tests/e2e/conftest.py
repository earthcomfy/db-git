from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests._pg_helpers import build_url


@pytest.fixture
def cli_env(git_repo: Path, pg_info: dict) -> Iterator[dict]:
    """
    Full CLI integration environment.
    """
    db_url = build_url(pg_info)
    old_cwd = os.getcwd()
    os.chdir(git_repo)
    try:
        yield {
            "repo": git_repo,
            "db_url": db_url,
            "pg_info": pg_info,
            "subprocess_env": {**os.environ, "DATABASE_URL": db_url},
        }
    finally:
        os.chdir(old_cwd)
