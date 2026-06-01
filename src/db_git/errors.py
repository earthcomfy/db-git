class DbGitError(Exception):
    """
    Base exception for all db-git errors.
    """


class ConfigError(DbGitError):
    """
    Invalid or missing configuration.
    """


class DatabaseError(DbGitError):
    """
    Connection or query failures.
    """


class SnapshotError(DbGitError):
    """
    Save/restore failures.
    """


class ToolNotFoundError(DbGitError):
    """
    Database tool (pg_dump, pg_restore, etc.) not found in PATH.
    """


class ActiveConnectionsError(DbGitError):
    """
    Active connections exist and policy is 'fail'.
    """


class TerminationTimeout(DbGitError):
    """
    Connection terminate loop exceeded deadline.
    """


class HookError(DbGitError):
    """
    Hook install/remove failures.
    """
