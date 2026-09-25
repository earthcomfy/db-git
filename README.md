# db-git

[![CI](https://github.com/earthcomfy/db-git/actions/workflows/test.yml/badge.svg)](https://github.com/earthcomfy/db-git/actions/workflows/test.yml)
[![Python](https://img.shields.io/pypi/pyversions/db-git.svg)](https://pypi.org/project/db-git/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Keep your database in sync with your git branches.

`db-git` is a developer tool for projects where database state follows code
changes: schema migrations, seed data, experimental feature work, and branch
switching during reviews. It installs a git `post-checkout` hook and keeps your
local database aligned with the branch you are working on.

> Status: PostgreSQL is supported today; support for additional database
> engines is planned.

## Features

- Automatic database handling on `git checkout`
- Two workflows:
  - `shared`: one database, saved and restored per branch
  - `per-branch`: one database per branch
- PostgreSQL support today, with plans for more database backends
- Two PostgreSQL snapshot strategies:
  - `template`: fast database clones using `CREATE DATABASE ... TEMPLATE`
  - `pgdump`: portable snapshots using `pg_dump` and `pg_restore`
- Manual `save`, `restore`, `create`, `reset`, `list`, `status`, `prune`, and `recover`
  commands
- Read-only `doctor` diagnostics with actionable text and JSON reports
- Staged replacements, retained recovery copies, and interrupted-operation recovery
- Safe hook behavior: checkout is never blocked by db-git failures
- Rich terminal output and local state stored under `.git/db-git/`

## Quick Start

```bash
uv tool install db-git # or pip install db-git
```

Run this from inside a git repository:

```bash
db-git init --database-url postgresql://postgres:postgres@localhost:5432/myapp
```

Interactive init will ask:

- Whether to use `shared` or `per-branch` mode
- Whether to use `template` or `pgdump` strategy
- What to do when active connections block database operations
- Whether to install the git `post-checkout` hook

After setup, switch branches normally:

```bash
git checkout feature/auth
```

In `shared` mode, db-git saves the previous branch database and restores the
new branch snapshot if one exists.

In `per-branch` mode, db-git creates or selects a database named from the
current branch, for example:

```text
myapp__feature__auth__hf8c7f316292f
```

Because the database name changes per branch, your application server also
needs to connect to the branch database. For example, when working on
`feature/auth`, point your app's `DATABASE_URL` at the branch database.
New names include a stable hash so branches such as `feature/foo-bar` and
`feature/foo_bar` cannot share a database just because their readable names match.

`db-git url` prints the full connection URL for the current branch, so you can
hand it straight to your app or client:

```bash
export DATABASE_URL=$(db-git url)
psql "$(db-git url)"
```

## Choosing a Mode

### Shared Mode

Shared mode keeps one database name from `DATABASE_URL`.

Use this when:

- You want one familiar local database name
- You want branch-specific snapshots
- You are comfortable with db-git dropping and restoring that local database
  during branch switches

### Per-Branch Mode

Per-branch mode creates a separate database for each branch. The configured
default branch keeps the original database name and acts as the seed database.

Use this when:

- You want branch databases to persist independently
- You prefer creating new databases over repeatedly restoring one shared
  database

## Choosing a Strategy

### template

The `template` strategy uses PostgreSQL database cloning:

```sql
CREATE DATABASE target TEMPLATE source;
```

It is usually fast, but requires sufficient PostgreSQL privileges and can be
blocked by active connections to the source or target database.

### pgdump

The `pgdump` strategy uses `pg_dump` and `pg_restore`.

It is slower than `template`, but can be a better fit when template cloning is
not available. It requires PostgreSQL client tools to be installed locally.
Both strategies require `CREATEDB` for cloning and staged restores. Shared-mode
replacement also requires ownership of the working database.

## Commands

### Initialize

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

### Inspect State

```bash
db-git status
db-git list
db-git url
```

### Shared Mode Commands

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

### Per-Branch Commands

Create a branch database before checking out the branch:

```bash
db-git create feature/auth
```

Build a replacement from the seed database, retaining the previous database for recovery:

```bash
db-git reset feature/auth
```

The default branch database cannot be reset because it is the seed for other
branch databases.

### Prune Deleted Branches

Preview stale snapshots or branch databases:

```bash
db-git prune --dry-run
```

Remove stale snapshots or branch databases from active use:

```bash
db-git prune --yes
```

Pruned data is retained as a recovery copy. To permanently free its storage, use
`db-git recover` to identify the prune operation, then
`db-git recover <operation-id> --discard`.


### Hook Management

Install or reinstall the checkout hook:

```bash
db-git hook install
```

Remove the checkout hook:

```bash
db-git hook remove
```

Temporarily disable db-git without removing the hook:

```bash
db-git disable
db-git enable
```

You can also skip hook behavior for a single checkout:

```bash
DB_GIT_SKIP=1 git checkout other-branch
```

### Diagnose setup and connection problems

```bash
db-git doctor
db-git doctor --json
db-git doctor --json --strict
```

`doctor` checks configuration, the active Git hook (including `core.hooksPath`),
disabled switching, local storage access, recovery journals, both the maintenance
and configured database connections, role permissions, PostgreSQL client versions,
and recorded databases/snapshots. It only reads files and database catalogs;
it does not create, replace, delete, or terminate anything.

Each finding has an ID, status (`ok`, `warning`, `error`, or `skipped`), message,
and suggested remedy when needed. JSON includes `schema_version: 1`, `ok`,
`exit_code`, and a `checks` array. Reports omit connection URLs, passwords, and
raw driver errors. `--database-url` overrides the configured connection for the
checks; otherwise normal file/environment precedence applies.

| Exit code | Meaning |
| --- | --- |
| `0` | No errors; warnings may still be present |
| `1` | A diagnostic failed, or a warning was found with `--strict` |
| `2` | Invalid configuration/URL or command-line usage |

Local checks continue when the server is unreachable. Connections use a
five-second timeout per connection attempt, and diagnostic queries use a
five-second statement timeout. Permission checks inspect catalogs and source
SELECT privileges; they do not prove that every extension or restored object
will succeed. Inspect failures before enabling automatic switching.

## Configuration

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
| `mode` | `shared` or `per-branch` | `shared` |
| `default_branch` | Seed branch for per-branch mode | `main` |
| `strategy` | `template` or `pgdump` | required |
| `on_active_connections` | `terminate` or `fail` | `terminate` |
| `snapshot_dir` | Shared-mode snapshot metadata/dump directory | `.git/db-git/snapshots` |
| `max_snapshots` | Snapshot count kept by prune logic | `20` |
| `force_terminate_timeout_ms` | Active connection termination timeout | `5000` |

Configuration precedence:

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
DB_GIT_FORCE_TERMINATE_TIMEOUT_MS
```

`DB_GIT_DATABASE_URL` takes precedence over `DATABASE_URL`.

### PostgreSQL connection options

PostgreSQL URLs are parsed using libpq, preserving options such as `sslmode`,
`sslrootcert`, `connect_timeout`, `application_name`, `options`, and `service`
through maintenance connections and `pg_dump`/`pg_restore`. Unix socket hosts,
IPv6, multiple hosts, and percent-encoded credentials/database names are accepted.
Use PostgreSQL/libpq options; unknown options produce an error instead of being
silently discarded.

```toml
database_url = "postgresql://dev@localhost/myapp?sslmode=require&connect_timeout=5"
```

`db-git url` percent-encodes branch database names and preserves connection options.
If the original URL has a `dbname` query parameter, it is updated along with the
path. Encode spaces as `%20`; libpq treats `+` literally.

An explicit database name remains required. Maintenance operations use the
`postgres` database with the same connection settings. Ordinary URLs retain the
historical defaults (`postgres`, `localhost`, port `5432`) when neither the URL
nor corresponding environment variables specify them. Service URLs let libpq
resolve those settings from the service file. When using services, unset inherited
`PGHOST`, `PGHOSTADDR`, and `PGPORT`: psycopg may resolve these before reading the
service file; `doctor` warns about this combination.

Client commands pass ordinary URL passwords through `PGPASSWORD`, keeping them
out of process arguments. For `sslpassword`, or a password used with `service`,
put credentials in a protected libpq service file and reference `?service=NAME`.
Inline credentials in these combinations are rejected for client operations,
because a service password takes precedence over `PGPASSWORD`.

Relative `snapshot_dir` paths resolve from the repository root, including when
commands run in a subdirectory. Malformed TOML is an error, and snapshot limits
and termination timeouts must be positive.

### Existing databases and snapshots

Existing branch databases keep the names recorded in `.git/db-git/state.json`.
Existing snapshots keep their names when their metadata identifies the exact
branch. No automatic database rename or snapshot migration is required. Use
`db-git url` instead of constructing database names in application scripts.

Keep the state and snapshot metadata when upgrading. If multiple branches are
already recorded against the same database, db-git refuses operations that rely
on that ambiguous ownership. Back up that database and reconcile its state
records before proceeding; changing names cannot recover data previously lost
to a collision.

### Recoverable operations

Saves, restores, branch creation, resets, and pruning use a durable operation
journal. Replacements are fully built before publication. Database replacements
are switched with transactional renames; dump files and JSON metadata use staged
writes. The previous copy is retained. Ordinary failures attempt automatic
rollback; interrupted operations block further mutations until resolved.

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

# Permanently remove retained backups/staging data for a resolved operation.
# The active target is kept.
db-git recover <operation-id> --discard
```

Recovery actions ask for confirmation; add `--yes` for scripts. `--finish` is
available only after the replacement reached the ready stage. An interrupted
rollback must be resumed with `--rollback`; interrupted disposal with `--discard`.
If later work changed a resource or its metadata, recovery refuses to overwrite it.

`db-git status` shows how many recovery records are retained. Backups consume
local disk space or databases on the same PostgreSQL server until explicitly
discarded; pruning alone does not free that retained storage. Keep the journals
until their recovery resources have been resolved and discarded.

Concurrent db-git writers are rejected with a retry message. Process locks protect
local state, and PostgreSQL advisory locks protect operations on the same configured
base database, including operations from another repository. These locks coordinate
db-git processes; active application connections follow `on_active_connections`.

### Recover after a failed branch switch

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

### Active connections block an operation

Stop your development server, database console, migration watcher, or GUI
client, then retry.

Alternatively, configure:

```toml
on_active_connections = "terminate"
```

Your PostgreSQL user may need superuser privileges or membership in
`pg_signal_backend` to terminate sessions owned by other users.

### Temporarily skip db-git

```bash
db-git disable
git checkout some-branch
db-git enable
```

Or for one command:

```bash
DB_GIT_SKIP=1 git checkout some-branch
```

### Show full tracebacks

```bash
DB_GIT_DEBUG=1 db-git status
```

## Development

Install dependencies:

```bash
uv sync --group dev
```

Run checks:

```bash
uv run ruff check .
uv run mypy src tests
uv run pytest tests/unit
```

Run the full nox suite:

```bash
nox
```

## License

MIT
