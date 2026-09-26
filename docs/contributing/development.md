# Development

Install dependencies:

```bash
uv sync --group dev
```

Run checks:

```bash
uv run ruff check .
uv run mypy src
uv run pytest tests/unit tests/sqlite
```

For MySQL development, install its optional driver and Oracle MySQL clients, then
run the Docker-backed workflow and recovery tests against each supported server:

```bash
uv sync --group dev --extra mysql
DB_GIT_TEST_MYSQL_IMAGE=mysql:8.0 uv run pytest tests/mysql
DB_GIT_TEST_MYSQL_IMAGE=mysql:8.4 uv run pytest tests/mysql
```

CI runs both MySQL workflows, stored-object cloning, and recovery tests on
Python 3.12 and 3.13 against MySQL 8.0 and 8.4.

Run the full nox suite:

```bash
nox
```

## Run the CI matrix locally

To catch differences between macOS tools and the Linux CI environment, use the
local runner from the repository root with Docker running:

```bash
python3 scripts/ci/local.py
```

It builds an Ubuntu 24.04 image, includes uncommitted source changes, and runs
the same nox sessions for Python 3.12/3.13, PostgreSQL 13–17, and MySQL 8.0/8.4.
PostgreSQL sessions select their matching client binaries; MySQL uses Ubuntu's
Oracle MySQL 8.0 clients. The checkout and its virtual environments stay untouched.
Logs and a JSON result summary are written to the temporary directory printed
by the runner. The command exits nonzero when a session fails.

The default is one job at a time with an x86-64 Linux test runner. GitHub gives
each matrix job its own machine; local jobs share Docker's CPU and memory with
your other containers. On Apple Silicon, emulation adds overhead, and concurrent
database suites can encounter transient connection failures. Select a specific
session, or increase concurrency when your machine has sufficient resources:

```bash
python3 scripts/ci/local.py --jobs 4
python3 scripts/ci/local.py --session "integration-3.12(pg_image='postgres:15')"
```

The runner uses the local Docker socket to create disposable test databases.
It validates test jobs, not GitHub authentication, caching, Pages deployment,
or PyPI publication. Database image architecture follows the Docker daemon;
the hosted runner's exact package patch versions may also differ over time.

See [documentation development](documentation.md) to preview and publish the site.
