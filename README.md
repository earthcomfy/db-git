# db-git

[![CI](https://github.com/earthcomfy/db-git/actions/workflows/test.yml/badge.svg)](https://github.com/earthcomfy/db-git/actions/workflows/test.yml)
[![Python](https://img.shields.io/pypi/pyversions/db-git.svg)](https://pypi.org/project/db-git/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Keep your database in sync with your git branches.

`db-git` is a developer tool for projects where database state follows code
changes: schema migrations, seed data, experimental feature work, and branch
switching during reviews. It installs a git `post-checkout` hook and keeps your
local database aligned with the branch you are working on.

> Status: PostgreSQL supports shared and per-branch modes. SQLite supports
> per-branch databases using its online backup API. MySQL 8.0/8.4 supports
> both modes using recoverable database generations.

## Features

- Automatic database handling on `git checkout`
- Two workflows:
  - `shared`: one database, saved and restored per branch
  - `per-branch`: one database per branch
- PostgreSQL and MySQL support, plus SQLite per-branch databases
- Two PostgreSQL snapshot strategies:
  - `template`: fast database clones using `CREATE DATABASE ... TEMPLATE`
  - `pgdump`: portable snapshots using `pg_dump` and `pg_restore`
- Manual `save`, `restore`, `create`, `reset`, `list`, `status`, `prune`, and `recover`
  commands
- Launch applications with `run -- <command>` and clone with `create --from <branch>`
- Read-only `doctor` diagnostics with actionable text and JSON reports
- Staged replacements, retained recovery copies, and interrupted-operation recovery
- Safe hook behavior: checkout is never blocked by db-git failures
- Git worktrees in per-branch mode, with shared ownership and recovery records
- Rich terminal output and local state stored in Git’s common directory

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

Launch your app with the current branch's database:

```bash
db-git run -- npm run dev
db-git run -- python manage.py migrate
```

`db-git url` also prints the full connection URL for use with other tools:

```bash
export DATABASE_URL="$(db-git url)"
psql "$(db-git url)"
```

Your configured seed URL stays separate from the application's `DATABASE_URL`,
so subsequent db-git commands keep targeting the correct seed.

## MySQL

Install the optional driver and Oracle MySQL's `mysql` and `mysqldump` clients:

```bash
uv tool install 'db-git[mysql]' # or pip install 'db-git[mysql]'
db-git init --database-url 'mysql://dev:password@localhost:3306/myapp'
git checkout -b feature/auth
db-git run -- npm run dev
```

MySQL supports **per-branch and shared modes**, the `mysqldump` strategy, and the
`fail` connection policy. Per-branch is the default. Supported server targets are
**MySQL 8.0 and 8.4**; MariaDB and other server versions are rejected.

For snapshots with one active working database, choose shared mode in a fresh
repository (existing resources must retain their mode for recovery):

```bash
db-git init --database-url 'mysql://dev:password@localhost:3306/myapp' --mode shared
db-git save main
db-git restore main
db-git run -- npm run dev
```

Shared checkout saves the previous branch's working database and restores the
new branch's snapshot when available. Each restore builds a **fresh database
first**, then atomically selects its URL. It does not replace the configured seed
under the same name. `db-git url`, `run`, and `status` resolve the active generation;
the configured `database_url` remains the management/seed connection. Restart
applications through `db-git run` after restore, reset, rollback, or branch switch.
Existing connections continue using their previous databases.

`db-git run -- <command>` supplies `DATABASE_URL` to the process it starts. Your
application must read that variable; an application-specific configuration or
dotenv loader that overrides it will still connect to its own configured database.
The command does not rewrite `.env` files or restart an already-running server.
After checkout or restore, stop that server and run the command again. Use
`db-git url` when configuring a database GUI; it prints credentials, so treat its
output as a connection secret. Both `url` and `run` refuse unresolved operations
or a missing/invalid managed database.

Cloning and snapshots preserve InnoDB tables, indexes, internal foreign keys,
binary data, views (in dependency order), procedures, functions, triggers, and
events. Stored programs retain their creation SQL mode; views use MySQL's
normalized definitions. Character settings and applicable time zone/database
collation settings are preserved. Qualified source references point to the clone;
string values and comments remain unchanged. Definers become the cloning account,
while SQL SECURITY DEFINER/INVOKER behavior is retained. Data loads before triggers
are created. Copied events are always **DISABLED** and **ON COMPLETION PRESERVE**;
enable them manually only when you intend scheduled work to run in that database.
The source's event status is unchanged.

Non-InnoDB tables, cross-database foreign keys/references, dynamic SQL, executable
comments, and ambiguous schema-name aliases in stored objects are rejected rather
than copied with references to the wrong database. Use explicit aliases when
qualified references cannot be resolved. Stored-definition validation supports a
conservative SQL subset: parenthesized table groups, derived-table references,
and qualified aliases inside DDL statements are refused. Ordinary joins, table
aliases, scalar subqueries, and simple local DDL are supported. Snapshot archives
bundle table data and
stored-object definitions; incomplete or invalid archives are never published as
the working database.

Per-branch `prune` removes ownership records. Shared `prune` removes snapshot
archives through recoverable file operations; `recover --discard` releases their
retained backup files. Database generations are **never automatically dropped**.
Stop applications and review ownership/recovery records before manually deleting
retained databases. Failed restores can leave unselected generations for inspection.

Permissions may be granted directly or through **default active roles**. Required
source-schema privileges are `SELECT`, `SHOW VIEW`, `TRIGGER`, and `EVENT`.
Destination generations need `CREATE`, `INSERT`, `ALTER`, `DROP`, `INDEX`,
`REFERENCES`, `CREATE VIEW`, `CREATE ROUTINE`, `ALTER ROUTINE`, `EXECUTE`, `TRIGGER`,
and `EVENT`; grant source privileges there too if you will clone or snapshot those
generations. The managed schema prefix is `_dbgit_` followed by the first 12 hex
digits of SHA-256 of the seed database name, then `_`; grant on that prefix with
an appropriate MySQL database grant pattern. `BACKUP_ADMIN` and `SHOW_ROUTINE`
(or global `SELECT`) remain global requirements. Partial revokes require manual
grant reconciliation. `doctor` checks grants without changing them. On servers
with binary logging, stored-function creation may additionally require the
server administrator to configure `log_bin_trust_function_creators` or grant
MySQL's required administrative privilege; db-git does not change server settings.

A backup lock blocks table-schema changes during the transactional dump while
allowing data writes. Stored-object definitions are checked again after the dump
to detect concurrent changes. Lock acquisition fails after five seconds; db-git
never terminates application connections. Multiple worktrees require per-branch
mode, as with PostgreSQL.

URLs require an explicit host, user, and database. Supported options: `ssl_mode`
(`REQUIRED`, `VERIFY_CA`, `VERIFY_IDENTITY`, or `DISABLED`), `ssl_ca`, `ssl_cert`,
`ssl_key`, and `connect_timeout` (driver/`mysql` client, 1–60 seconds). TLS encryption
is required by default; use `VERIFY_IDENTITY` with a trusted CA for server identity
verification. Connection options survive branch URL rewriting. Passwords use a
temporary owner-only client option file, never command arguments or inherited
`MYSQL_PWD`. Keep the reserved `__dbgit_generation` table intact for recovery.
Generated database names fit MySQL's 64-character limit.

## SQLite

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

The default branch uses the seed file. Other branches use files under the common
Git directory's `db-git/sqlite/branches/`, shared across worktrees. The backup API
copies a consistent view including committed WAL data, without copying or removing
the source's sidecars. An exclusive writer may block the copy; SQLite operations
never terminate application connections. `backup_timeout_ms` (default 5000, or
`DB_GIT_BACKUP_TIMEOUT_MS`) bounds backup work and retries.

**Reset and recovery select file generations.** Reset backs up the seed into a new
file, then atomically updates the branch's recorded path. It preserves the previous
file. Connections already using that file can continue using it; restart applications
through `db-git run` after reset or rollback to select the intended generation.
Avoid caching branch URLs across resets. Interrupted operations block further
mutations and `run` until `recover --finish` or `recover --rollback` resolves them.
Recovery also refuses to overwrite ownership changes made by a later operation.

SQLite currently does **not** support shared-mode save/restore or automatic file
deletion. `prune` removes stale ownership records but retains their files;
`recover --discard` removes the resolved journal while retaining SQLite files.
This avoids unlinking databases that an application may still have open. Stop all
applications before manually removing files, retain the currently recorded files,
and preserve files referenced by recovery journals. `doctor` checks SQLite integrity,
missing files, ownership, and the count of retained generations. This retention
policy can consume disk space; PostgreSQL cleanup behavior is unchanged.

## Git Worktrees

Use **per-branch mode** to work on several branches at once:

```bash
# Initialize once in your primary checkout.
db-git init --database-url postgresql://localhost/myapp --mode per-branch

git worktree add ../myapp-feature -b feature/auth
cd ../myapp-feature
db-git run -- npm run dev
```

With the hook installed, `git worktree add` creates the new branch's database from
seed if it does not already exist. An existing recorded branch database is reused.
The primary checkout and linked worktree can run applications simultaneously with
separate databases. Databases belong to **branches**, so forcing the same branch
into two worktrees does not give it two databases. PostgreSQL's existing connection
policy still applies to cloning and reset operations; template cloning may need to
stop connections to its source. Use `on_active_connections = "fail"` to refuse
those operations instead of terminating sessions.

To start from another branch's data, create the database before adding its worktree:

```bash
db-git create feature/review --from feature/auth
git worktree add ../myapp-review -b feature/review
```

All worktrees use the primary checkout's `.db-git.toml`. `init` from a linked
worktree also updates that shared file. Relative `snapshot_dir` settings resolve
against the primary checkout, even from nested directories in another worktree.
A conflicting `.db-git.toml` in a linked checkout is rejected; reconcile it with
the primary file before continuing. Environment and CLI overrides retain their
normal precedence. Keep the primary checkout available while using linked ones.

| Resource | Scope |
| --- | --- |
| Configuration | Primary checkout's `.db-git.toml` |
| Branch ownership | Common Git directory: `db-git/state.json` |
| Branch operation journals and lock | Common Git directory: `db-git/operations/` |
| Default snapshot directory | Common Git directory: `db-git/snapshots/` |
| Disable marker and checkout/rebase state | Current worktree's Git directory |

`list`, `create --from`, and `recover` see the same ownership and recovery records
from every checkout. An interrupted operation blocks mutations and `run` across
worktrees until recovered. `disable` and `enable` affect only the current worktree.
Older versions' worktree-local ownership or recovery files are detected and
preserved; reconcile those records with the common state before proceeding.
Existing database names are never automatically changed.

Hook installation and removal respect `core.hooksPath`, preserve an existing hook
as an adjacent `.legacy` file, and restore it on removal. Legacy hooks still run
when db-git is disabled. With Git's default hooks directory, one installation
serves every worktree. An absolute `core.hooksPath` is also shared. A **relative**
`core.hooksPath` resolves within each checkout, so run `db-git hook install` in
each worktree; if its initial checkout had no hook, run `db-git create` there too.
Removing a shared hook affects every worktree that uses it.

Shared mode cannot safely represent several checked-out branches in one working
database. With multiple worktrees, db-git refuses shared-mode snapshot changes,
automatic switching, `run`, and `enable`; `doctor` reports the conflict. Existing
snapshots and recovery records remain available. Use per-branch mode or remove
extra worktrees before resuming shared mode.

## Choosing a Mode

### Shared Mode

PostgreSQL shared mode keeps the name from the configured `database_url`.
MySQL shared mode selects a fresh working database URL on restore; restart apps
through `db-git run` afterward.

Use this when:

- You want one active working database and branch-specific snapshots
- You want branch-specific snapshots
- You restart applications after restoring or switching branches

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

### Run an application or migration

```bash
db-git run -- npm run dev
db-git run -- python manage.py migrate
db-git run -- psql
```

`run` resolves the current branch once, verifies its database exists, then launches
the command with `DATABASE_URL` set to that database. In shared mode it uses the
configured database on PostgreSQL or the active generation on MySQL. The child also receives `DB_GIT_DATABASE_URL` containing the
seed URL, so nested db-git commands retain the management connection.

The command inherits your working directory, input/output, and remaining
environment. On POSIX it replaces the wrapper process, preserving signals and
the application's exit status. A missing executable exits `127`; an executable
that cannot be started exits `126`. Arguments are passed directly without shell
expansion. Use `sh -c '...'` explicitly if you need shell syntax.

`run` requires initialization and an existing database; it does not automatically
clone one. It refuses untracked branch databases, interrupted recovery operations,
detached HEAD in per-branch mode, and disabled switching in shared mode. Create or
recover the database first. Stop and restart your app when you switch branches;
a running process keeps the URL selected at launch.

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

Temporarily disable db-git in the current worktree without removing the hook:

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
Both `url` and `run` make implicit host, port, and user defaults explicit in the
application URL, so applications connect with the same settings as db-git.
Service URLs continue to use their service-file defaults.
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

Relative `snapshot_dir` paths resolve from the primary checkout root, including when
commands run in a subdirectory. Malformed TOML is an error, and snapshot limits
and termination timeouts must be positive.

### Existing databases and snapshots

Existing branch databases keep the names recorded in `db-git/state.json` under
Git’s common directory (`.git/db-git/state.json` in an ordinary checkout).
Existing snapshots keep their names when their metadata identifies the exact
branch. No automatic database rename or snapshot migration is required. Use
`db-git url` instead of constructing database names in application scripts.

Keep the state and snapshot metadata when upgrading. If multiple branches are
already recorded against the same database, db-git refuses operations that rely
on that ambiguous ownership. Back up that database and reconcile its state
records before proceeding; changing names cannot recover data previously lost
to a collision.

### Recoverable operations

Database changes use a durable operation journal. Replacements are fully built
before publication. PostgreSQL database replacements use transactional renames;
dump files and JSON metadata use staged writes. SQLite publishes new branch files
and updates ownership, retaining earlier generations. Ordinary failures attempt
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
uv run mypy src
uv run pytest tests/unit tests/sqlite
```

For MySQL development, install its optional driver and Oracle MySQL clients, then
run the Docker-backed workflow and recovery tests against each supported server:

```bash
uv sync --group dev --extra mysql
DB_GIT_TEST_MYSQL_IMAGE=mysql:8.0 uv run pytest tests/mysql
DB_GIT_TEST_MYSQL_IMAGE=mysql:8.4 uv run pytest tests/mysql
```

CI runs both MySQL workflows, stored-object cloning, and recovery tests on
Python 3.12 and 3.13 against MySQL 8.0 and 8.4.

Run the full nox suite:

```bash
nox
```

## License

MIT
