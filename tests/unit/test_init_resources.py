from pathlib import Path

import pytest

from db_git.cli._init_resources import has_resources, same_mysql_seed


@pytest.mark.parametrize(
    "resource",
    ["mysql-active.json", "branch.meta.json", "branch.dump", ".operations/record.json"],
)
@pytest.mark.parametrize("location", ["default", "relative", "absolute"])
def test_resources_detected_in_configured_or_default_snapshot_storage(
    tmp_path: Path, resource: str, location: str
):
    git_dir = tmp_path / ".git"
    existing: dict[str, object] = {"snapshot_dir": "custom-snapshots"}
    directory = tmp_path / "custom-snapshots"
    if location == "default":
        directory = git_dir / "db-git/snapshots"
    elif location == "absolute":
        existing["snapshot_dir"] = str(directory)
    artifact = directory / resource
    artifact.parent.mkdir(parents=True)
    artifact.write_text("retained")
    assert has_resources(tmp_path, git_dir, existing)


def test_empty_storage_allows_reinitialization(tmp_path: Path):
    assert not has_resources(tmp_path, tmp_path / ".git", {})


@pytest.mark.parametrize(
    "current",
    [
        "mysql://user:new-password@localhost/seed",
        "mysql://other-user:password@localhost:3306/seed?ssl_mode=VERIFY_IDENTITY",
        "mysql://user:password@LOCALHOST/seed?connect_timeout=10",
    ],
)
def test_mysql_credentials_and_options_do_not_change_seed(current: str):
    assert same_mysql_seed("mysql://user:password@localhost/seed", current)


@pytest.mark.parametrize(
    "current",
    [
        "mysql://user:password@localhost/other",
        "mysql://user:password@elsewhere/seed",
        "mysql://user:password@localhost:3307/seed",
        "postgresql://user:password@localhost/seed",
    ],
)
def test_different_server_or_database_remains_a_different_seed(current: str):
    assert not same_mysql_seed("mysql://user:password@localhost/seed", current)
