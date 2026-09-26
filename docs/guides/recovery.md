# Recover interrupted operations

Database changes use a durable operation journal. Replacements are fully built
before publication. PostgreSQL database replacements use transactional renames;
dump files and JSON metadata use staged writes. SQLite publishes new branch files
and updates ownership, retaining earlier generations. MySQL builds fresh database
generations and publishes their selected URLs, preserving the old databases.
Ordinary failures attempt
automatic rollback; interrupted operations block further mutations until resolved.

Inspect recovery records (newest first):

```bash
db-git recover
```

Use an operation ID from that output:

```bash
# Restore the previous database/snapshot and metadata.
# The replaced data remains available as the staged recovery copy.
db-git recover <operation-id> --rollback

# Finish publishing a replacement that was fully built before interruption.
db-git recover <operation-id> --finish

# PostgreSQL: remove retained backups/staging data; keep the active target.
# MySQL: remove retained snapshot files; keep database generations.
# SQLite: remove the resolved journal; keep every database file.
db-git recover <operation-id> --discard
```

Recovery actions ask for confirmation; add `--yes` for scripts. `--finish` is
available only after the replacement reached the ready stage. An interrupted
rollback must be resumed with `--rollback`; interrupted disposal with `--discard`.
If later work changed a resource or its metadata, recovery refuses to overwrite it.

`db-git status` shows how many recovery records are retained. Backups consume
local disk space or databases on the same PostgreSQL server until explicitly
discarded. SQLite files and MySQL generations remain until manually removed;
pruning or discarding their journals does not free retained storage. Keep the journals
until their recovery resources have been resolved and discarded.

Concurrent db-git writers are rejected with a retry message. Process locks protect
local state. PostgreSQL advisory locks and MySQL named locks protect operations on
the same configured seed database, including operations from another repository. These locks coordinate
db-git processes; active application connections follow `on_active_connections`.

## Recover after a failed branch switch

Git checkout still completes when database handling fails. Shared-mode switching
is disabled during the save/restore sequence and re-enabled only after success.
This also protects the gap between save and restore if the process exits abruptly.

Start with `db-git recover`. Finish or roll back any interrupted operation before
running another mutation. Once recovery is resolved, align the working database
with the branch Git checked out. If saving failed, preserve the working data under
the branch you left, for example `db-git save main`, before restoring the intended
branch snapshot with `db-git restore feature/auth`. If that branch has no snapshot,
explicitly choose the saved state it should start from.

Run `db-git enable` once the database matches the intended branch. It refuses to
re-enable automatic switching while an interrupted operation remains unresolved.
