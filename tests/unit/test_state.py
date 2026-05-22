from __future__ import annotations

import json

from git_db.state import (
    BranchDbEntry,
    GitDbState,
    get_branch_db,
    load_state,
    record_branch_db,
    remove_branch_db,
    save_state,
)


class TestState:
    def test_load_missing_file_returns_empty(self, tmp_path):
        state = load_state(tmp_path)
        assert state.mode == "per-branch"
        assert state.databases == {}

    def test_save_and_load_round_trip(self, tmp_path):
        state = GitDbState(
            mode="per-branch",
            databases={
                "feature/auth": BranchDbEntry(
                    db_name="myapp__feature__auth",
                    created_at="2026-04-07T12:00:00+00:00",
                    created_from="main",
                ),
            },
        )
        save_state(tmp_path, state)
        loaded = load_state(tmp_path)
        assert loaded.mode == "per-branch"
        assert "feature/auth" in loaded.databases
        entry = loaded.databases["feature/auth"]
        assert entry.db_name == "myapp__feature__auth"
        assert entry.created_from == "main"

    def test_save_creates_directory(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        save_state(git_dir, GitDbState())
        assert (git_dir / "git-db" / "state.json").exists()

    def test_record_branch_db_adds_entry(self, tmp_path):
        record_branch_db(tmp_path, "feature/x", "myapp__feature__x", "main")
        entry = get_branch_db(tmp_path, "feature/x")
        assert entry is not None
        assert entry.db_name == "myapp__feature__x"
        assert entry.created_from == "main"

    def test_record_branch_db_updates_existing(self, tmp_path):
        record_branch_db(tmp_path, "feature/x", "myapp__feature__x", "main")
        record_branch_db(tmp_path, "feature/x", "myapp__feature__x", "develop")
        entry = get_branch_db(tmp_path, "feature/x")
        assert entry is not None
        assert entry.created_from == "develop"

    def test_remove_branch_db(self, tmp_path):
        record_branch_db(tmp_path, "feature/x", "myapp__feature__x", "main")
        remove_branch_db(tmp_path, "feature/x")
        assert get_branch_db(tmp_path, "feature/x") is None

    def test_remove_nonexistent_branch_is_no_op(self, tmp_path):
        remove_branch_db(tmp_path, "nonexistent")
        state = load_state(tmp_path)
        assert state.databases == {}

    def test_get_branch_db_returns_none_for_missing(self, tmp_path):
        assert get_branch_db(tmp_path, "nonexistent") is None

    def test_load_malformed_json_returns_empty(self, tmp_path):
        state_dir = tmp_path / "git-db"
        state_dir.mkdir(parents=True)
        (state_dir / "state.json").write_text("not json")
        state = load_state(tmp_path)
        assert state.databases == {}

    def test_state_file_is_valid_json(self, tmp_path):
        record_branch_db(tmp_path, "feat", "myapp__feat", "main")
        state_file = tmp_path / "git-db" / "state.json"
        data = json.loads(state_file.read_text())
        assert "databases" in data
        assert "feat" in data["databases"]
