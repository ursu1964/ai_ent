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

