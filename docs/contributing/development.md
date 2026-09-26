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

See [documentation development](documentation.md) to preview and publish the site.
