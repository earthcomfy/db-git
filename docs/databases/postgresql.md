# PostgreSQL

PostgreSQL supports shared and per-branch modes, with `template` and `pgdump`
strategies. Start with an existing development database and a role that can create
databases. Follow the [quickstart](../getting-started/quickstart.md) to initialize.

## template

The `template` strategy uses PostgreSQL database cloning:

```sql
CREATE DATABASE target TEMPLATE source;
```

It is usually fast, but requires sufficient PostgreSQL privileges and can be
blocked by active connections to the source or target database.

## pgdump

The `pgdump` strategy uses `pg_dump` and `pg_restore`.

It is slower than `template`, but can be a better fit when template cloning is
not available. It requires PostgreSQL client tools to be installed locally.
Both strategies require `CREATEDB` for cloning and staged restores. Shared-mode
replacement also requires ownership of the working database.

## Connection options

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
