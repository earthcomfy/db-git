class GitDbError(Exception):
    """
    Base exception for all git-db errors.
    """


class ConfigError(GitDbError):
    """
    Invalid or missing configuration.
    """


class DatabaseError(GitDbError):
    """
    Connection or query failures.
    """


class SnapshotError(GitDbError):
    """
    Save/restore failures.
    """


class ToolNotFoundError(GitDbError):
    """
    Database tool (pg_dump, pg_restore, etc.) not found in PATH.
    """


class ActiveConnectionsError(GitDbError):
    """
    Active connections exist and policy is 'fail'.
    """


class TerminationTimeout(GitDbError):
    """
    Connection terminate loop exceeded deadline.
    """


class HookError(GitDbError):
    """
    Hook install/remove failures.
    """
