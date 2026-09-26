# Troubleshooting

## Diagnose setup and connection problems

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


## Active connections block an operation

Stop your development server, database console, migration watcher, or GUI
client, then retry.

For PostgreSQL only, you can instead configure:

```toml
on_active_connections = "terminate"
```

Your PostgreSQL user may need superuser privileges or membership in
`pg_signal_backend` to terminate sessions owned by other users.


## Temporarily skip db-git

```bash
db-git disable
git checkout some-branch
db-git enable
```

Or for one command:

```bash
DB_GIT_SKIP=1 git checkout some-branch
```


## Show full tracebacks

```bash
DB_GIT_DEBUG=1 db-git status
```

For interrupted operations or a failed checkout, follow the
[recovery guide](guides/recovery.md) before re-enabling switching.
