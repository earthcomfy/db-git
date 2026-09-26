# Configuration

`db-git init` writes `.db-git.toml` at the repository root.

Example:

```toml
database_url = "postgresql://postgres:postgres@localhost:5432/myapp"
mode = "per-branch"
default_branch = "main"
strategy = "template"
on_active_connections = "terminate"
```

Supported configuration keys:

| Key | Description | Default |
| --- | --- | --- |
| `database_url` | Database connection URL | required |
| `mode` | PostgreSQL/MySQL: either mode; SQLite: `per-branch` | PostgreSQL: `shared`; SQLite/MySQL: `per-branch` |
| `default_branch` | Seed branch for per-branch mode | `main` |
| `strategy` | PostgreSQL: `template` or `pgdump`; SQLite: `backup`; MySQL: `mysqldump` | required for PostgreSQL; SQLite/MySQL use engine defaults |
| `on_active_connections` | PostgreSQL: `terminate` or `fail`; SQLite/MySQL: `fail` | PostgreSQL: `terminate`; SQLite/MySQL: `fail` |
| `snapshot_dir` | Shared-mode snapshot metadata/dump directory | `db-git/snapshots` under Git’s common directory |
| `max_snapshots` | Snapshot count kept by prune logic | `20` |
| `backup_timeout_ms` | SQLite backup deadline in milliseconds | `5000` |
| `force_terminate_timeout_ms` | PostgreSQL connection termination timeout | `5000` |

Configuration precedence for settings other than the seed URL:

1. Built-in defaults
2. `.db-git.toml`
3. Environment variables
4. CLI options

Environment variables:

```bash
DATABASE_URL
DB_GIT_DATABASE_URL
DB_GIT_MODE
DB_GIT_STRATEGY
DB_GIT_ON_ACTIVE_CONNECTIONS
DB_GIT_SNAPSHOT_DIR
DB_GIT_MAX_SNAPSHOTS
DB_GIT_BACKUP_TIMEOUT_MS
DB_GIT_FORCE_TERMINATE_TIMEOUT_MS
```

Seed URL precedence, highest first:

1. Explicit `--database-url`
2. `DB_GIT_DATABASE_URL`
3. `database_url` in `.db-git.toml`
4. `DATABASE_URL`, only when no seed is configured

**Changed behavior:** exporting an application `DATABASE_URL` no longer overrides
an initialized project's seed. To deliberately change the management connection,
use `DB_GIT_DATABASE_URL` or `--database-url`. Initialization still accepts
`DATABASE_URL` as a starting value when there is no saved seed; reinitialization
preserves the saved seed unless explicitly overridden.


See engine-specific URL details for [PostgreSQL](../databases/postgresql.md#connection-options),
[MySQL](../databases/mysql.md), and [SQLite](../databases/sqlite.md).
