# Git worktrees

Use **per-branch mode** to work on several branches at once:

```bash
# Initialize once in your primary checkout.
db-git init --database-url postgresql://localhost/myapp --mode per-branch

git worktree add ../myapp-feature -b feature/auth
cd ../myapp-feature
db-git run -- npm run dev
```

With the hook installed, `git worktree add` creates the new branch's database from
seed if it does not already exist. An existing recorded branch database is reused.
The primary checkout and linked worktree can run applications simultaneously with
separate databases. Databases belong to **branches**, so forcing the same branch
into two worktrees does not give it two databases. PostgreSQL's existing connection
policy still applies to cloning and reset operations; template cloning may need to
stop connections to its source. Use `on_active_connections = "fail"` to refuse
those operations instead of terminating sessions.

To start from another branch's data, create the database before adding its worktree:

```bash
db-git create feature/review --from feature/auth
git worktree add ../myapp-review -b feature/review
```

All worktrees use the primary checkout's `.db-git.toml`. `init` from a linked
worktree also updates that shared file. Relative `snapshot_dir` settings resolve
against the primary checkout, even from nested directories in another worktree.
A conflicting `.db-git.toml` in a linked checkout is rejected; reconcile it with
the primary file before continuing. Environment and CLI overrides retain their
normal precedence. Keep the primary checkout available while using linked ones.

| Resource | Scope |
| --- | --- |
| Configuration | Primary checkout's `.db-git.toml` |
| Branch ownership | Common Git directory: `db-git/state.json` |
| Branch operation journals and lock | Common Git directory: `db-git/operations/` |
| Default snapshot directory | Common Git directory: `db-git/snapshots/` |
| Disable marker and checkout/rebase state | Current worktree's Git directory |

`list`, `create --from`, and `recover` see the same ownership and recovery records
from every checkout. An interrupted operation blocks mutations and `run` across
worktrees until recovered. `disable` and `enable` affect only the current worktree.
Older versions' worktree-local ownership or recovery files are detected and
preserved; reconcile those records with the common state before proceeding.
Existing database names are never automatically changed.

Hook installation and removal respect `core.hooksPath`, preserve an existing hook
as an adjacent `.legacy` file, and restore it on removal. Legacy hooks still run
when db-git is disabled. With Git's default hooks directory, one installation
serves every worktree. An absolute `core.hooksPath` is also shared. A **relative**
`core.hooksPath` resolves within each checkout, so run `db-git hook install` in
each worktree; if its initial checkout had no hook, run `db-git create` there too.
Removing a shared hook affects every worktree that uses it.

Shared mode cannot safely represent several checked-out branches in one working
database. With multiple worktrees, db-git refuses shared-mode snapshot changes,
automatic switching, `run`, and `enable`; `doctor` reports the conflict. Existing
snapshots and recovery records remain available. Use per-branch mode or remove
extra worktrees before resuming shared mode.
