from __future__ import annotations


class RepositoryError(RuntimeError):
    """Base repository error with sanitized, deterministic messages."""


class DuplicateProjectError(RepositoryError):
    """Raised when a project identity or unique field already exists."""


class MissingProjectError(RepositoryError):
    """Raised when a project is required but missing."""


class DuplicateTaskError(RepositoryError):
    """Raised when a task identity or unique field already exists."""


class MissingTaskError(RepositoryError):
    """Raised when a task is required but missing."""


class DuplicateDependencyError(RepositoryError):
    """Raised when a task dependency edge already exists."""


class DuplicateExecutionError(RepositoryError):
    """Raised when an execution identity already exists."""


class MissingExecutionError(RepositoryError):
    """Raised when an execution is required but missing."""


class DuplicateCheckpointError(RepositoryError):
    """Raised when a checkpoint identity already exists."""


class MissingCheckpointError(RepositoryError):
    """Raised when a checkpoint is required but missing."""
