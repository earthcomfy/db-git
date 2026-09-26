# db-git

[![CI](https://github.com/earthcomfy/db-git/actions/workflows/test.yml/badge.svg)](https://github.com/earthcomfy/db-git/actions/workflows/test.yml)
[![Python](https://img.shields.io/pypi/pyversions/db-git.svg)](https://pypi.org/project/db-git/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/earthcomfy/db-git/blob/main/LICENSE)

Keep your database in sync with your Git branches.

`db-git` is a developer tool for schema migrations, seed data, experimental
features, and branch switching during reviews. Its `post-checkout` hook keeps
your local database aligned with the branch you are working on.

**[Documentation](https://earthcomfy.github.io/db-git/)** ·
[Browse docs in GitHub](https://github.com/earthcomfy/db-git/tree/main/docs) ·
[Release notes](https://github.com/earthcomfy/db-git/blob/main/CHANGELOG.md)

## Features

- Automatic database handling on Git checkout.
- **Shared mode:** save and restore branch-specific snapshots.
- **Per-branch mode:** keep an independent database for each branch.
- Launch applications and migrations with `db-git run -- <command>`.
- Named checkpoints, history, and explicit retention in shared mode.
- Git worktrees in per-branch mode.
- Read-only diagnostics and recoverable database operations.
- Checkout still completes if database handling fails, with recovery guidance.

## Supported databases

| Database | Modes | Strategy |
| --- | --- | --- |
| PostgreSQL | Shared and per-branch | `template` or `pgdump` |
| MySQL 8.0 / 8.4 | Shared and per-branch | `mysqldump` |
| SQLite | Per-branch | Online backup |

See the [database guides](https://earthcomfy.github.io/db-git/#choose-your-database)
for required privileges, client tools, and engine-specific limitations.

## Quick start

Requires Python 3.12+, Git, and an existing local development database.
For PostgreSQL:

```bash
uv tool install db-git # or pip install db-git

# Run inside your application's Git repository.
db-git init --database-url postgresql://localhost/myapp --mode per-branch

git checkout -b feature/auth
db-git run -- npm run dev # replace with your application's command
```

Install the hook during initialization. Your application must read `DATABASE_URL`;
restart it through `db-git run` after switching branches. The default branch keeps
the seed database, while other branches receive separate copies.

For MySQL, install `uv tool install 'db-git[mysql]'` and follow the
[MySQL guide](https://github.com/earthcomfy/db-git/blob/main/docs/databases/mysql.md).
For SQLite, follow the [SQLite guide](https://github.com/earthcomfy/db-git/blob/main/docs/databases/sqlite.md).

## Learn more

- [Choose shared or per-branch mode](https://github.com/earthcomfy/db-git/blob/main/docs/concepts/modes.md)
- [Run applications and migrations](https://github.com/earthcomfy/db-git/blob/main/docs/guides/applications.md)
- [Work with Git worktrees](https://github.com/earthcomfy/db-git/blob/main/docs/guides/worktrees.md)
- [Save checkpoints and inspect history](https://github.com/earthcomfy/db-git/blob/main/docs/guides/checkpoints.md)
- [Command reference](https://github.com/earthcomfy/db-git/blob/main/docs/reference/commands.md)
- [Configuration](https://github.com/earthcomfy/db-git/blob/main/docs/reference/configuration.md)
- [Troubleshooting](https://github.com/earthcomfy/db-git/blob/main/docs/troubleshooting.md)
- [Recovery](https://github.com/earthcomfy/db-git/blob/main/docs/guides/recovery.md)

## Contributing

See the [development guide](https://github.com/earthcomfy/db-git/blob/main/docs/contributing/development.md)
for dependencies and tests, and the
[documentation guide](https://github.com/earthcomfy/db-git/blob/main/docs/contributing/documentation.md)
for local preview and publishing.

## License

[MIT](https://github.com/earthcomfy/db-git/blob/main/LICENSE)
