# MySQL

## Installation and setup

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

## Shared mode and application connections

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

## Supported data and stored objects

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

## Retention and cleanup

Per-branch `prune` removes ownership records. Shared `prune` removes snapshot
archives through recoverable file operations; `recover --discard` releases their
retained backup files. Database generations are **never automatically dropped**.
Stop applications and review ownership/recovery records before manually deleting
retained databases. Failed restores can leave unselected generations for inspection.

## Permissions

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

## Concurrent operations

A backup lock blocks table-schema changes during the transactional dump while
allowing data writes. Stored-object definitions are checked again after the dump
to detect concurrent changes. Lock acquisition fails after five seconds; db-git
never terminates application connections. Multiple worktrees require per-branch
mode, as with PostgreSQL.

## Connection options and credentials

URLs require an explicit host, user, and database. Supported options: `ssl_mode`
(`REQUIRED`, `VERIFY_CA`, `VERIFY_IDENTITY`, or `DISABLED`), `ssl_ca`, `ssl_cert`,
`ssl_key`, and `connect_timeout` (driver/`mysql` client, 1–60 seconds). TLS encryption
is required by default; use `VERIFY_IDENTITY` with a trusted CA for server identity
verification. Connection options survive branch URL rewriting. Passwords use a
temporary owner-only client option file, never command arguments or inherited
`MYSQL_PWD`. Keep the reserved `__dbgit_generation` table intact for recovery.
Generated database names fit MySQL's 64-character limit.

See also [recovery](../guides/recovery.md) and [configuration](../reference/configuration.md).
