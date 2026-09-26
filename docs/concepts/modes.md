# Choose a mode

## Shared Mode

PostgreSQL shared mode keeps the name from the configured `database_url`.
MySQL shared mode selects a fresh working database URL on restore; restart apps
through `db-git run` afterward.

Use this when:

- You want one active working database and branch-specific snapshots
- You restart applications after restoring or switching branches

## Per-Branch Mode

Per-branch mode creates a separate database for each branch. The configured
default branch keeps the original database name and acts as the seed database.

Use this when:

- You want branch databases to persist independently
- You prefer creating new databases over repeatedly restoring one shared
  database

SQLite supports only per-branch mode. Multiple Git worktrees also require
per-branch mode. See [worktrees](../guides/worktrees.md) for setup and shared state.
