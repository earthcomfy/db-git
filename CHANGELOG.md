# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-05-22

### Added

- Initial `git-db` command-line interface.
- Git `post-checkout` hook installation, removal, enable, disable, and dispatch
  support.
- Shared database mode for saving and restoring branch-specific snapshots.
- Per-branch database mode for creating one database per git branch.
- PostgreSQL backend with `template` and `pgdump` snapshot strategies.
- Active connection handling with `terminate` and `fail` policies.
- Manual commands for `save`, `restore`, `create`, `reset`, `list`, `status`,
  and `prune`.
- Snapshot metadata and local state storage under `.git/git-db/`.
- Unit, integration, and end-to-end tests for CLI, storage, git hooks,
  PostgreSQL strategies, and branch database workflows.
- Nox sessions, Ruff linting/format checks, mypy type checking, pre-commit
  hooks, Dependabot, and GitHub Actions CI.
