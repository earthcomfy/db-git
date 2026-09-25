import subprocess
import sys
from pathlib import Path

import pytest

from db_git.errors import DbGitError
from db_git.files import atomic_write, local_lock
from db_git.recovery import (
    discard,
    finish,
    operations,
    require_recovered,
    rollback,
    run_operation,
)
from db_git.resources import FileResources


def replace_file(root, *, build=None):
    target = root / "snapshot.dump"
    metadata = root / "snapshot.meta"
    target.write_text("original")
    metadata.write_text("old metadata")
    return run_operation(
        root,
        FileResources(),
        action="save",
        kind="file",
        server="test",
        target=str(target),
        names=lambda identifier: (
            str(root / f"{identifier}.stage"),
            str(root / f"{identifier}.backup"),
        ),
        metadata=metadata,
        build=build or (lambda stage: Path(stage).write_text("replacement")),
        after=lambda _: "new metadata",
    )


def test_complete_backup_rollback_and_discard(tmp_path):
    op = replace_file(tmp_path)
    assert Path(op.target).read_text() == "replacement"
    assert Path(op.backup).read_text() == "original"
    rollback(tmp_path, FileResources(), op)
    assert Path(op.target).read_text() == "original"
    assert Path(op.stage).read_text() == "replacement"
    assert (tmp_path / "snapshot.meta").read_text() == "old metadata"
    discard(tmp_path, FileResources(), op)
    assert not operations(tmp_path)
    assert Path(op.target).read_text() == "original"


def test_build_failure_preserves_original_and_retains_partial_stage(tmp_path):
    def fail(stage):
        Path(stage).write_text("partial")
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError, match="disk full"):
        replace_file(tmp_path, build=fail)
    (op,) = operations(tmp_path)
    assert op.phase == "rolled_back"
    assert Path(op.target).read_text() == "original"
    assert Path(op.stage).read_text() == "partial"
    assert (tmp_path / "snapshot.meta").read_text() == "old metadata"


def test_metadata_failure_after_publication_rolls_back(tmp_path, monkeypatch):
    import db_git.recovery as recovery

    original = recovery.atomic_write

    def fail_new_metadata(path, text):
        if path.name == "snapshot.meta" and text == "new metadata":
            raise OSError("disk full")
        original(path, text)

    monkeypatch.setattr(recovery, "atomic_write", fail_new_metadata)
    with pytest.raises(OSError, match="disk full"):
        replace_file(tmp_path)
    assert (tmp_path / "snapshot.dump").read_text() == "original"
    assert (tmp_path / "snapshot.meta").read_text() == "old metadata"
    assert operations(tmp_path)[0].phase == "rolled_back"


@pytest.mark.parametrize("action", ["finish", "rollback"])
@pytest.mark.parametrize(
    "cut", ["building", "ready", "first_rename", "published", "metadata_written"]
)
def test_process_crash_can_be_recovered(tmp_path, cut, action):
    code = """
import os, sys
from pathlib import Path
from tests.unit.test_recovery import replace_file
import db_git.recovery as recovery
import db_git.resources as resources
root, cut = Path(sys.argv[1]), sys.argv[2]
if cut == 'building':
    identity = resources.FileResources.identity
    def crash(self, name):
        result = identity(self, name)
        if name.endswith('.stage') and result is not None:
            os._exit(77)
        return result
    resources.FileResources.identity = crash
elif cut == 'metadata_written':
    apply = recovery.apply_metadata
    def crash(record, text):
        apply(record, text)
        os._exit(77)
    recovery.apply_metadata = crash
elif cut == 'ready':
    resources.FileResources.publish = lambda *args: os._exit(77)
elif cut == 'published':
    recovery.apply_metadata = lambda *args: os._exit(77)
else:
    original = resources.os.replace
    def crash(source, target):
        original(source, target)
        if str(source).endswith('snapshot.dump'):
            os._exit(77)
    resources.os.replace = crash
replace_file(root)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), cut], capture_output=True, text=True
    )
    assert result.returncode == 77, result.stderr
    with pytest.raises(DbGitError, match="Interrupted"):
        require_recovered(tmp_path)
    (op,) = operations(tmp_path)
    if action == "finish" and cut == "building":
        with pytest.raises(DbGitError, match="not ready"):
            finish(tmp_path, FileResources(), op)
    if action == "finish" and cut != "building":
        finish(tmp_path, FileResources(), op)
        assert Path(op.target).read_text() == "replacement"
        assert (tmp_path / "snapshot.meta").read_text() == "new metadata"
    else:
        rollback(tmp_path, FileResources(), op)
        assert Path(op.target).read_text() == "original"
        assert (tmp_path / "snapshot.meta").read_text() == "old metadata"
    require_recovered(tmp_path)
    discard(tmp_path, FileResources(), op)
    assert not operations(tmp_path)


def test_atomic_write_failure_keeps_original(tmp_path, monkeypatch):
    import db_git.files as files

    target = tmp_path / "state.json"
    target.write_text("original")

    def fail(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(files.os, "replace", fail)
    with pytest.raises(OSError):
        atomic_write(target, "replacement")
    assert target.read_text() == "original"
    assert list(tmp_path.iterdir()) == [target]


def test_lock_excludes_other_processes_and_allows_nested_calls(tmp_path):
    code = (
        "from pathlib import Path; import sys; from db_git.files import local_lock;\n"
        "with local_lock(Path(sys.argv[1])): pass"
    )
    with local_lock(tmp_path), local_lock(tmp_path):
        result = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True
        )
        assert result.returncode != 0
        assert "Another db-git operation" in result.stderr
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True
    )
    assert result.returncode == 0


def test_old_recovery_cannot_overwrite_later_metadata(tmp_path):
    op = replace_file(tmp_path)
    (tmp_path / "snapshot.meta").write_text("later work")
    with pytest.raises(DbGitError, match="later work"):
        rollback(tmp_path, FileResources(), op)
    assert Path(op.target).read_text() == "replacement"


def test_discard_refuses_changed_backup(tmp_path):
    op = replace_file(tmp_path)
    Path(op.backup).write_text("some other data")
    with pytest.raises(DbGitError, match="identity changed"):
        discard(tmp_path, FileResources(), op)
    assert Path(op.backup).read_text() == "some other data"


@pytest.mark.parametrize("action", ["rollback", "discard"])
def test_recovery_itself_can_resume_after_crash(tmp_path, action):
    code = """
import os, sys
from pathlib import Path
from tests.unit.test_recovery import replace_file
from db_git.recovery import rollback, discard
from db_git.resources import FileResources
root, action = Path(sys.argv[1]), sys.argv[2]
op = replace_file(root)
store = FileResources()
if action == 'rollback':
    move = store._move
    def crash(pairs):
        move(pairs[:1])
        os._exit(77)
    store._move = crash
    rollback(root, store, op)
else:
    remove = store.remove
    def crash(name, identity):
        remove(name, identity)
        if name == op.backup:
            os._exit(77)
    store.remove = crash
    discard(root, store, op)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), action],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 77, result.stderr
    (op,) = operations(tmp_path)
    with pytest.raises(DbGitError, match="Interrupted"):
        require_recovered(tmp_path)
    if action == "rollback":
        assert op.phase == "rolling_back"
        with pytest.raises(DbGitError):
            discard(tmp_path, FileResources(), op)
        rollback(tmp_path, FileResources(), op)
        assert Path(op.target).read_text() == "original"
    else:
        assert op.phase == "discarding"
        with pytest.raises(DbGitError):
            rollback(tmp_path, FileResources(), op)
        assert Path(op.target).read_text() == "replacement"
    discard(tmp_path, FileResources(), op)
    assert not operations(tmp_path)
