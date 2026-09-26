# Your first database branch

First follow the [installation guide](installation.md). Use an existing local
development database; PostgreSQL examples below assume it is named `myapp`.

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

Next, read about [modes](../concepts/modes.md), [running your app](../guides/applications.md), or your database: [PostgreSQL](../databases/postgresql.md), [MySQL](../databases/mysql.md), [SQLite](../databases/sqlite.md).
