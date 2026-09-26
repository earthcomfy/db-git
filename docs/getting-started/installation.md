# Installation

db-git requires **Python 3.12 or newer**, Git, and an existing development database.
Run db-git commands inside your project's Git repository.

## Install a published release

```bash
uv tool install db-git
# Or:
pip install db-git
```

For MySQL, include the optional driver:

```bash
uv tool install 'db-git[mysql]'
# Or:
pip install 'db-git[mysql]'
```

## Install the development version

```bash
uv tool install 'git+https://github.com/earthcomfy/db-git.git@main'
# With the optional MySQL driver:
uv tool install 'db-git[mysql] @ git+https://github.com/earthcomfy/db-git.git@main'
```

These commands follow the current `main` branch, which can change between installs.

## Database requirements

| Database | Requirements |
| --- | --- |
| PostgreSQL | A server and role with database creation privileges. Install `pg_dump` and `pg_restore` for the `pgdump` strategy. See [permissions and strategies](../databases/postgresql.md). |
| MySQL | Oracle MySQL 8.0 or 8.4, the optional Python driver, and Oracle `mysql` / `mysqldump` clients. See [required grants and supported objects](../databases/mysql.md). |
| SQLite | An existing untracked database file. Uses Python's built-in SQLite library; no server or external client required. See [file and URL requirements](../databases/sqlite.md). |

## Verify and continue

```bash
db-git --help
```

Continue with the [PostgreSQL quickstart](quickstart.md),
[MySQL setup](../databases/mysql.md), or [SQLite setup](../databases/sqlite.md).
After initialization, run `db-git doctor` to check the configuration.
