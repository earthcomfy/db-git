# Checkpoint history

## Create and restore checkpoints

Shared PostgreSQL and MySQL projects can keep immutable checkpoints alongside
their usual branch snapshots. Create one before a migration or a data experiment:

```bash
db-git checkpoint before-migration
db-git checkpoint                    # unnamed checkpoint with a generated ID
db-git history                       # all branches, newest first
db-git history main --json           # full IDs and metadata for scripts
db-git restore --checkpoint before-migration
db-git restore main --checkpoint <id>
```

Checkpoints capture the current working database, labeled with the current branch
or an explicit `checkpoint --branch <branch>`. Names are unique within a branch;
an existing name cannot be overwritten. Full IDs and unambiguous ID prefixes of at
least eight characters also work for restore. Snapshot data stays outside Git.

Creating or restoring a checkpoint does not replace the branch's ordinary
snapshot. Ordinary `save` and checkout continue to maintain that latest snapshot;
they do **not** automatically create checkpoints. A checkpoint remains available
after restore, and the previous working database is retained by the recovery
journal. MySQL applications must restart through `db-git run` after restoration.
PostgreSQL checkpoints remember whether they used `template` or `pgdump`, even if
the strategy for new snapshots changes later.

## Retention

Retention is explicit and separate from ordinary `prune`:

```bash
db-git history --prune --keep 5 --dry-run
db-git history --prune --keep 5 --yes
db-git history main --prune --keep 5 --include-named --yes
```

This keeps at least the newest five checkpoints **per branch**. Older named
checkpoints are additionally protected unless `--include-named` is supplied.
The default is 20; `--keep` cannot be less than one. Deleting a Git branch does
not delete its checkpoints. `max_snapshots` continues to govern ordinary branch
snapshots, independently of checkpoint retention.

Pruning retires checkpoints through recoverable operations. Use `db-git recover`
to inspect these records, `--rollback` to return a retired checkpoint to history,
and `--discard` to remove resolved journals and retained snapshot backups. Disk
space is not reclaimed until backup disposal. A retired name stays reserved while
recovery records could bring it back; discard those records before reusing it.
MySQL working database generations remain subject to manual cleanup as before.

## Integrity and recovery

Restore and retention verify file checksums or PostgreSQL database identities.
Missing or changed checkpoints block these actions, so a damaged newest copy
cannot silently cause older copies to be pruned. `doctor` also checks history.
Interrupted checkpoint publication uses the same `recover --finish`/`--rollback`
workflow as other snapshots. Per-branch mode, including SQLite, continues to use
branch copies and reset rather than these shared-mode checkpoint commands.
