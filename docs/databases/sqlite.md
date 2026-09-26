# SQLite

## Setup

SQLite uses **per-branch mode** and the `backup` strategy. Start with an existing,
untracked local database file:

```bash
db-git init --database-url sqlite:///development.sqlite3
# Equivalent explicit options: --mode per-branch --strategy backup

git checkout -b feature/search   # hook creates a branch file from the current DB
db-git url                     # absolute SQLite URL for this branch
db-git run -- your-app-command  # passes that URL in DATABASE_URL

db-git create review --from feature/search
db-git reset feature/search
db-git recover
```

## Database files and URLs

SQLite support uses Python's built-in `sqlite3` library; it needs no database server
or external client tools. Your application must understand a SQLite connection URL
in `DATABASE_URL`. Relative `sqlite:///development.sqlite3` paths resolve against
the primary checkout, including when commands run in another worktree. Absolute
POSIX paths use four slashes, such as `sqlite:////home/me/project/dev.sqlite3`.
Percent-encode reserved filename characters (`%20`, `%23`, `%3F`, `%25`). In-memory
databases, URI query options, credentials, and nonlocal authorities are rejected.
Missing seed files are reported rather than silently created. Keep the seed and its
`-wal`, `-shm`, and `-journal` sidecars out of version control; initialization rejects
a tracked seed because Git could replace it during checkout.

## Backups and connections

The default branch uses the seed file. Other branches use files under the common
Git directory's `db-git/sqlite/branches/`, shared across worktrees. The backup API
copies a consistent view including committed WAL data, without copying or removing
the source's sidecars. An exclusive writer may block the copy; SQLite operations
never terminate application connections. `backup_timeout_ms` (default 5000, or
`DB_GIT_BACKUP_TIMEOUT_MS`) bounds backup work and retries.

## Reset and recovery

Reset and recovery select file generations. Reset backs up the seed into a new
file, then atomically updates the branch's recorded path. It preserves the previous
file. Connections already using that file can continue using it; restart applications
through `db-git run` after reset or rollback to select the intended generation.
Avoid caching branch URLs across resets. Interrupted operations block further
mutations and `run` until `recover --finish` or `recover --rollback` resolves them.
Recovery also refuses to overwrite ownership changes made by a later operation.

## Retention and limitations

SQLite currently does **not** support shared-mode save/restore or automatic file
deletion. `prune` removes stale ownership records but retains their files;
`recover --discard` removes the resolved journal while retaining SQLite files.
This avoids unlinking databases that an application may still have open. Stop all
applications before manually removing files, retain the currently recorded files,
and preserve files referenced by recovery journals. `doctor` checks SQLite integrity,
missing files, ownership, and the count of retained generations. This retention
policy can consume disk space; PostgreSQL cleanup behavior is unchanged.

See also [recovery](../guides/recovery.md) and [configuration](../reference/configuration.md).
