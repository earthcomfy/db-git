from __future__ import annotations

from pathlib import Path

from db_git.storage import (
    SnapshotMetadata,
    branch_db_name,
    has_snapshot,
    identify_stale_snapshots,
    list_snapshots,
    make_metadata,
    metadata_path,
    read_metadata,
    sanitize_branch_name,
    snapshot_db_name,
    snapshot_dump_path,
    write_metadata,
)


class TestBranchDbName:
    def test_default_branch_returns_seed_name(self):
        assert branch_db_name("main", "myapp", "main") == "myapp"

    def test_feature_branch_gets_suffix(self):
        assert branch_db_name("develop", "myapp", "main") == "myapp__develop"

    def test_slashes_sanitized(self):
        result = branch_db_name("feature/auth", "myapp", "main")
        assert result == "myapp__feature__auth"

    def test_truncation_on_long_branch(self):
        long_branch = "a" * 100
        result = branch_db_name(long_branch, "db", "main", max_length=20)
        assert len(result) <= 20
        assert "__h" in result

    def test_long_dbname_uses_hash(self):
        long_dbname = "a" * 62
        result = branch_db_name("feat", long_dbname, "main", max_length=63)
        assert len(result) <= 63
        assert "__h" in result

    def test_hash_is_deterministic(self):
        r1 = branch_db_name("feature/auth", "long_db_name_here", "main", max_length=25)
        r2 = branch_db_name("feature/auth", "long_db_name_here", "main", max_length=25)
        assert r1 == r2

    def test_different_branches_get_different_hashes(self):
        r1 = branch_db_name("feature/a", "mydb", "main", max_length=20)
        r2 = branch_db_name("feature/b", "mydb", "main", max_length=20)
        assert r1 != r2

    def test_master_as_default(self):
        assert branch_db_name("master", "myapp", "master") == "myapp"
        assert branch_db_name("main", "myapp", "master") == "myapp__main"


class TestStorage:
    def _create_snapshot(
        self, snapshot_dir: Path, branch: str, created_at: str
    ) -> None:
        meta = SnapshotMetadata(
            branch=branch,
            database="db",
            strategy="pgdump",
            created_at=created_at,
            engine="postgresql",
            engine_version="16",
            db_git_version="0.1.0",
            file_size_bytes=100,
        )
        write_metadata(snapshot_dir, meta)
        dump = snapshot_dump_path(snapshot_dir, branch)
        dump.write_text("fake dump")

    def test_slash_replaced(self):
        assert sanitize_branch_name("feature/auth") == "feature__auth"

    def test_long_name_truncated(self):
        result = sanitize_branch_name("a" * 100)
        assert len(result) <= 63

    def test_empty_string_becomes_unnamed(self):
        assert sanitize_branch_name("") == "unnamed"

    def test_basic(self):
        assert snapshot_db_name("main", "myapp") == "_dbgit_myapp_main"

    def test_truncates_long_branch_to_fit(self):
        result = snapshot_db_name("a" * 100, "db")
        assert len(result) <= 63
        assert result.startswith("_dbgit_db_")

    def test_dump_path(self, tmp_path: Path):
        assert (
            snapshot_dump_path(tmp_path, "feature/auth")
            == tmp_path / "feature__auth.dump"
        )

    def test_metadata_path(self, tmp_path: Path):
        assert (
            metadata_path(tmp_path, "feature/auth")
            == tmp_path / "feature__auth.meta.json"
        )

    def test_write_and_read(self, tmp_path: Path):
        meta = make_metadata(
            branch="main",
            database="myapp",
            strategy="pgdump",
            engine="postgresql",
            engine_version="16",
        )
        write_metadata(tmp_path, meta)
        loaded = read_metadata(tmp_path, "main")

        assert loaded is not None
        assert loaded.branch == "main"
        assert loaded.database == "myapp"
        assert loaded.strategy == "pgdump"
        assert loaded.engine == "postgresql"
        assert loaded.engine_version == "16"
        assert loaded.file_size_bytes is None

    def test_read_nonexistent_returns_none(self, tmp_path: Path):
        assert read_metadata(tmp_path, "nonexistent") is None

    def test_write_creates_directory(self, tmp_path: Path):
        nested = tmp_path / "deep" / "nested"
        meta = make_metadata(
            branch="main",
            database="db",
            strategy="pgdump",
            engine="postgresql",
            engine_version="16",
        )
        write_metadata(nested, meta)
        assert (nested / "main.meta.json").exists()

    def test_with_file_size(self, tmp_path: Path):
        meta = make_metadata(
            branch="feature/auth",
            database="myapp",
            strategy="pgdump",
            engine="postgresql",
            engine_version="16",
            file_size_bytes=12345,
        )
        write_metadata(tmp_path, meta)
        loaded = read_metadata(tmp_path, "feature/auth")

        assert loaded is not None
        assert loaded.file_size_bytes == 12345

    def test_nonexistent_dir(self, tmp_path: Path):
        assert list_snapshots(tmp_path / "nope") == []

    def test_multiple_snapshots(self, tmp_path: Path):
        for branch in ["main", "feature/auth", "develop"]:
            meta = make_metadata(
                branch=branch,
                database="db",
                strategy="pgdump",
                engine="postgresql",
                engine_version="16",
            )
            write_metadata(tmp_path, meta)

        snapshots = list_snapshots(tmp_path)
        assert len(snapshots) == 3
        branches = {s.branch for s in snapshots}
        assert branches == {"main", "feature/auth", "develop"}

    def test_exists(self, tmp_path: Path):
        meta = make_metadata(
            branch="main",
            database="db",
            strategy="pgdump",
            engine="postgresql",
            engine_version="16",
        )
        write_metadata(tmp_path, meta)
        assert has_snapshot(tmp_path, "main") is True

    def test_not_exists(self, tmp_path: Path):
        assert has_snapshot(tmp_path, "main") is False

    def test_identifies_stale_branches(self, tmp_path: Path):
        self._create_snapshot(tmp_path, "main", "2026-01-01T00:00:00Z")
        self._create_snapshot(tmp_path, "deleted-branch", "2026-01-02T00:00:00Z")
        self._create_snapshot(tmp_path, "develop", "2026-01-03T00:00:00Z")

        stale = identify_stale_snapshots(
            tmp_path, max_snapshots=10, existing_branches=["main", "develop"]
        )
        assert [s.branch for s in stale] == ["deleted-branch"]

    def test_identifies_oldest_over_limit(self, tmp_path: Path):
        self._create_snapshot(tmp_path, "old", "2026-01-01T00:00:00Z")
        self._create_snapshot(tmp_path, "mid", "2026-01-02T00:00:00Z")
        self._create_snapshot(tmp_path, "new", "2026-01-03T00:00:00Z")

        stale = identify_stale_snapshots(
            tmp_path,
            max_snapshots=2,
            existing_branches=["old", "mid", "new"],
        )
        assert [s.branch for s in stale] == ["old"]

    def test_stale_removed_before_count_enforcement(self, tmp_path: Path):
        self._create_snapshot(tmp_path, "main", "2026-01-01T00:00:00Z")
        self._create_snapshot(tmp_path, "stale", "2026-01-02T00:00:00Z")
        self._create_snapshot(tmp_path, "develop", "2026-01-03T00:00:00Z")

        stale = identify_stale_snapshots(
            tmp_path,
            max_snapshots=2,
            existing_branches=["main", "develop"],
        )
        assert [s.branch for s in stale] == ["stale"]

    def test_empty_dir(self, tmp_path: Path):
        assert (
            identify_stale_snapshots(tmp_path, max_snapshots=5, existing_branches=[])
            == []
        )

    def test_does_not_delete_anything(self, tmp_path: Path):
        self._create_snapshot(tmp_path, "gone", "2026-01-01T00:00:00Z")
        dump = snapshot_dump_path(tmp_path, "gone")
        assert dump.exists()

        stale = identify_stale_snapshots(
            tmp_path, max_snapshots=10, existing_branches=[]
        )
        assert len(stale) == 1
        assert dump.exists()
        assert has_snapshot(tmp_path, "gone")
