# Run applications and migrations

```bash
db-git run -- npm run dev
db-git run -- python manage.py migrate
db-git run -- sh -c 'psql "$DATABASE_URL"'
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
