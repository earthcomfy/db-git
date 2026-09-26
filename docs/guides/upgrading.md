# Upgrade existing projects

Existing branch databases keep the names recorded in `db-git/state.json` under
Git’s common directory (`.git/db-git/state.json` in an ordinary checkout).
Existing snapshots keep their names when their metadata identifies the exact
branch. No automatic database rename or snapshot migration is required. Use
`db-git url` instead of constructing database names in application scripts.

Keep the state and snapshot metadata when upgrading. If multiple branches are
already recorded against the same database, db-git refuses operations that rely
on that ambiguous ownership. Back up that database and reconcile its state
records before proceeding; changing names cannot recover data previously lost
to a collision.

See the [release notes](https://github.com/earthcomfy/db-git/blob/main/CHANGELOG.md)
for published changes and the [configuration reference](../reference/configuration.md)
for seed URL precedence.
