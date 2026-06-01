HOOK_IDENTIFIER = "db-git-hook-v1"

_HOOK_TEMPLATE = """\
#!/bin/sh
# db-git post-checkout hook: do not edit (managed by db-git)
# Identifier: {identifier}

PREV_HEAD="$1"
NEW_HEAD="$2"
IS_BRANCH_CHECKOUT="$3"

# Skip file checkouts immediately (no Python startup cost)
[ "$IS_BRANCH_CHECKOUT" = "0" ] && exit 0

GIT_DIR="$(git rev-parse --git-dir)"

# Skip if db-git is disabled (flag file or env var)
[ "$DB_GIT_SKIP" = "1" ] && exit 0
[ -f "$GIT_DIR/db-git/disabled" ] && exit 0

# Run legacy hook if it exists
if [ -f "$GIT_DIR/hooks/post-checkout.legacy" ]; then
    "$GIT_DIR/hooks/post-checkout.legacy" "$@"
fi

# Call db-git. Prefer the executable resolved when the hook was installed so
# project-local venv installs keep working in Git's sparse hook environment.
DB_GIT_BIN="{db_git_executable}"
if [ -n "$DB_GIT_BIN" ] && [ -x "$DB_GIT_BIN" ]; then
    "$DB_GIT_BIN" _hook-dispatch "$PREV_HEAD" "$NEW_HEAD" "$IS_BRANCH_CHECKOUT"
elif command -v db-git >/dev/null 2>&1; then
    db-git _hook-dispatch "$PREV_HEAD" "$NEW_HEAD" "$IS_BRANCH_CHECKOUT"
else
    echo "db-git warning: executable not found; run 'db-git hook install'" >&2
fi

# NEVER block git checkout
exit 0
"""


def render_hook_script(db_git_executable: str = "") -> str:
    """
    Return the complete post-checkout hook script.
    """
    return _HOOK_TEMPLATE.format(
        identifier=HOOK_IDENTIFIER,
        db_git_executable=db_git_executable,
    )
