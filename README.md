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
- Manual `save`, `restore`, `create`, `reset`, `list`, `status`, and `prune`
  commands
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
myapp__feature__auth
```

Because the database name changes per branch, your application server also
needs to connect to the branch database. For example, when working on
`feature/auth`, point your app's `DATABASE_URL` at `myapp__feature__auth`.

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

Drop and recreate a branch database from the seed database:

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

Remove stale snapshots or branch databases:

```bash
db-git prune --yes
```

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
