# Command reference

Use `db-git --help` or `db-git <command> --help` for the complete option list.

## Initialize

```bash
db-git init --database-url postgresql://user:password@localhost:5432/myapp
```

Useful options:

```bash
db-git init \
  --database-url postgresql://user:password@localhost:5432/myapp \
  --mode per-branch \
  --strategy template \
  --on-active-connections terminate
```

Skip hook installation:

```bash
db-git init --database-url postgresql://localhost/myapp --no-hook
```


## Inspect State

```bash
db-git status
db-git list
db-git url
```


## Shared Mode Commands

Save the current branch database:

```bash
db-git save
```

Restore the current branch database:

```bash
db-git restore
```

Save or restore a specific branch:

```bash
db-git save main
db-git restore feature/auth
```


## Per-Branch Commands

Create a branch database before checking out the branch:

```bash
db-git create feature/auth
```

Choose the source explicitly:

```bash
db-git create feature/payments --from feature/auth
db-git create feature/clean-start --from main
```

`--from` copies the source branch's existing database and records that branch as
its origin. The configured default branch selects the seed. Other sources must
have recorded ownership and an existing database, including recorded legacy
names. An unavailable explicit source produces an error; there is no fallback.
The command creates a database, without checking out or creating a Git branch.
Existing target databases are never overwritten.

Without `--from`, `create` retains its current-branch behavior: copy the current
branch's recorded database when available, otherwise use the seed. `--from` is
available only in per-branch mode.

Build a replacement from the seed database, retaining the previous database for recovery:

```bash
db-git reset feature/auth
```

The default branch database cannot be reset because it is the seed for other
branch databases.


## Prune Deleted Branches

Preview stale snapshots or branch databases:

```bash
db-git prune --dry-run
```

Remove stale snapshots or branch databases from active use:

```bash
db-git prune --yes
```

PostgreSQL pruned data and MySQL shared snapshot archives are retained as recovery
copies. Use `db-git recover` to identify the prune operation, then
`db-git recover <operation-id> --discard` to release its retained storage.
SQLite files and MySQL database generations remain on disk or on the server;
pruning removes their ownership records and does not delete those databases.
See the [SQLite](../databases/sqlite.md) and [MySQL](../databases/mysql.md) guides
before manually removing retained data.


## Hook Management

Install or reinstall the checkout hook:

```bash
db-git hook install
```

Remove the checkout hook:

```bash
db-git hook remove
```

Temporarily disable db-git in the current worktree without removing the hook:

```bash
db-git disable
db-git enable
```

You can also skip hook behavior for a single checkout:

```bash
DB_GIT_SKIP=1 git checkout other-branch
```

## Checkpoints and history

Use `checkpoint`, `history`, and `restore --checkpoint` to manage explicit
checkpoints. See [checkpoint history](../guides/checkpoints.md) for names, IDs,
retention, and integrity checks.

## Applications, recovery, and diagnostics

- [`run -- <command>`](../guides/applications.md) launches your app with its database URL.
- [`recover`](../guides/recovery.md) inspects and resolves operation journals.
- [`doctor`](../troubleshooting.md) diagnoses configuration and database issues.
