"""Repository boundary for persistence access."""

from ai_ent.persistence.repositories.errors import (
    DuplicateDependencyError,
    DuplicateProjectError,
    DuplicateTaskError,
    MissingProjectError,
    MissingTaskError,
    RepositoryError,
)
from ai_ent.persistence.repositories.projects import ProjectRepository
from ai_ent.persistence.repositories.tasks import TaskRepository

__all__ = [
    "DuplicateDependencyError",
    "DuplicateProjectError",
    "DuplicateTaskError",
    "MissingProjectError",
    "MissingTaskError",
    "ProjectRepository",
    "RepositoryError",
    "TaskRepository",
]
