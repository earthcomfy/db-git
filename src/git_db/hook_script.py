HOOK_IDENTIFIER = "git-db-hook-v1"

_HOOK_TEMPLATE = """\
#!/bin/sh
# git-db post-checkout hook: do not edit (managed by git-db)
# Identifier: {identifier}

PREV_HEAD="$1"
NEW_HEAD="$2"
IS_BRANCH_CHECKOUT="$3"

# Skip file checkouts immediately (no Python startup cost)
[ "$IS_BRANCH_CHECKOUT" = "0" ] && exit 0

GIT_DIR="$(git rev-parse --git-dir)"

# Skip if git-db is disabled (flag file or env var)
[ "$GIT_DB_SKIP" = "1" ] && exit 0
[ -f "$GIT_DIR/git-db/disabled" ] && exit 0

# Run legacy hook if it exists
if [ -f "$GIT_DIR/hooks/post-checkout.legacy" ]; then
    "$GIT_DIR/hooks/post-checkout.legacy" "$@"
fi

# Call git-db; fail silently if not installed
if command -v git-db >/dev/null 2>&1; then
    git-db _hook-dispatch "$PREV_HEAD" "$NEW_HEAD" "$IS_BRANCH_CHECKOUT"
fi

# NEVER block git checkout
exit 0
"""


def render_hook_script() -> str:
    """
    Return the complete post-checkout hook script.
    """
    return _HOOK_TEMPLATE.format(identifier=HOOK_IDENTIFIER)
