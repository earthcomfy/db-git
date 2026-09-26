# db-git

Keep your database in sync with your Git branches.

`db-git` connects your local database workflow to Git. Switch branches, try a
migration, or review a feature with the database state that belongs to that work.
The checkout hook handles switching; `db-git run` connects your application to the
selected database.

[Get started](getting-started/installation.md){ .md-button .md-button--primary }
[Choose a mode](concepts/modes.md){ .md-button }

## Choose your database

| Database | Modes | Strategy | Guide |
| --- | --- | --- | --- |
| PostgreSQL | Shared and per-branch | `template`, `pgdump` | [PostgreSQL](databases/postgresql.md) |
| MySQL 8.0 / 8.4 | Shared and per-branch | `mysqldump` | [MySQL](databases/mysql.md) |
| SQLite | Per-branch | Online backup | [SQLite](databases/sqlite.md) |

Use local development databases. Each engine has different connection, privilege,
and recovery requirements; read its guide before initializing.

## Find your workflow

- **New to db-git?** Follow [your first database branch](getting-started/quickstart.md).
- **Working on several branches?** Set up [Git worktrees](guides/worktrees.md).
- **Trying a migration?** Create a [checkpoint](guides/checkpoints.md) in shared mode.
- **Connecting an application?** Learn how [`db-git run`](guides/applications.md) selects its URL.
- **Something failed?** Start with [doctor](troubleshooting.md) and [recovery](guides/recovery.md).
- **Looking up an option?** Browse [commands](reference/commands.md) and [configuration](reference/configuration.md).

## How switching works

In **shared mode**, db-git saves the database for the branch you leave and restores
the destination branch's snapshot when one exists. In **per-branch mode**, each
branch has its own database and db-git creates or selects it on checkout.

Restart your application through `db-git run -- <command>` after switching.
An already-running process keeps its original connection.

Database changes use staged replacements and recovery journals. A checkout still
completes if database handling fails; inspect the failure before continuing work.
See [recovery](guides/recovery.md) for retained copies and interrupted operations.

## Project

[Source code](https://github.com/earthcomfy/db-git) ·
[Issues](https://github.com/earthcomfy/db-git/issues) ·
[Release notes](https://github.com/earthcomfy/db-git/blob/main/CHANGELOG.md) ·
[MIT license](https://github.com/earthcomfy/db-git/blob/main/LICENSE)
