# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- version list -->

## v0.3.0 (2026-09-26)

### Bug Fixes

- Align database clients and validate release CI locally
  ([`56600a6`](https://github.com/earthcomfy/db-git/commit/56600a6b43873c44faa619275f2f6f7f2cd424c7))

- Prevent unsafe database switches and branch name collisions
  ([`8ae904e`](https://github.com/earthcomfy/db-git/commit/8ae904ed79d7862d5994dcac065be4bde6108fce))

### Build System

- **deps**: Bump actions/checkout from 6 to 7 ([#10](https://github.com/earthcomfy/db-git/pull/10),
  [`8a7221a`](https://github.com/earthcomfy/db-git/commit/8a7221ad4b248c15a93349c1d58f7a965e9b65f0))

- **deps**: Bump https://github.com/astral-sh/ruff-pre-commit
  ([#11](https://github.com/earthcomfy/db-git/pull/11),
  [`6ff3495`](https://github.com/earthcomfy/db-git/commit/6ff34959ba9655d173f47279ae155d89eb12b41d))

### Chores

- Update changelog
  ([`e135315`](https://github.com/earthcomfy/db-git/commit/e13531531c719345ebed747536a8594262b20ead))

### Documentation

- Add Material documentation site and Pages workflow
  ([`b3c0b04`](https://github.com/earthcomfy/db-git/commit/b3c0b04f17adc895cd329c705859eb7eddaaa957))

### Features

- Add application runner and explicit branch database sources
  ([`eab7f27`](https://github.com/earthcomfy/db-git/commit/eab7f27cf65e58c8fd7ebdad7779a2590c256003))

- Add checkpoint history and recoverable retention
  ([`3b495b8`](https://github.com/earthcomfy/db-git/commit/3b495b83cf51d207b20d1b4a6bd11d5ed8735395))

- Add doctor diagnostics and preserve PostgreSQL connection options
  ([`6abedd2`](https://github.com/earthcomfy/db-git/commit/6abedd24770a83f3635a5d2751fea3ba70a5d25b))

- Add MySQL databases, shared snapshots, and recoverable generations
  ([`124a63a`](https://github.com/earthcomfy/db-git/commit/124a63a8f96742eb8c757aff1b7104976e852cfd))

- Add recoverable database and snapshot operations
  ([`51ace0f`](https://github.com/earthcomfy/db-git/commit/51ace0fc1da9169d00c39aa39737a3860e4c5d31))

- Add SQLite per-branch databases and recoverable file generations
  ([`a42584c`](https://github.com/earthcomfy/db-git/commit/a42584c63ea7999c13acdcf907af3a14063861f4))

- Support Git worktrees with shared database ownership
  ([`701dc12`](https://github.com/earthcomfy/db-git/commit/701dc12c99c47e5d43a98bd4cd63bf805789a86b))


## v0.2.0 (2026-06-08)

### Added

- Introduced a new command `db-git url` that outputs the database connection URL
  for the current or specified branch.

### Changed

- Enhanced `config.py` to provide clearer documentation for the database URL configuration.

## v0.1.1 (2026-06-01)

### Changed

- Renamed project, CLI, package imports, configuration files, environment
  variables, hook metadata, and local state paths to `db-git`.

## v0.1.0 (2026-05-22)

### Added

- Initial `db-git` command-line interface.
- Git `post-checkout` hook installation, removal, enable, disable, and dispatch
  support.
- Shared database mode for saving and restoring branch-specific snapshots.
- Per-branch database mode for creating one database per git branch.
- PostgreSQL backend with `template` and `pgdump` snapshot strategies.
- Active connection handling with `terminate` and `fail` policies.
- Manual commands for `save`, `restore`, `create`, `reset`, `list`, `status`,
  and `prune`.
- Snapshot metadata and local state storage under `.git/db-git/`.
- Unit, integration, and end-to-end tests for CLI, storage, git hooks,
  PostgreSQL strategies, and branch database workflows.
- Nox sessions, Ruff linting/format checks, mypy type checking, pre-commit
  hooks, Dependabot, and GitHub Actions CI.
