"""Repository boundary for persistence access."""

from ai_ent.persistence.repositories.bootstrap import BootstrapRunRepository
from ai_ent.persistence.repositories.checkpoints import CheckpointRepository
from ai_ent.persistence.repositories.errors import (
    DuplicateBootstrapCheckpointError,
    DuplicateBootstrapRunError,
    DuplicateCheckpointError,
    DuplicateDependencyError,
    DuplicateExecutionError,
    DuplicateLeaseError,
    DuplicateProjectError,
    DuplicateTaskError,
    MissingBootstrapCheckpointError,
    MissingBootstrapRunError,
    MissingCheckpointError,
    MissingExecutionError,
    MissingLeaseError,
    MissingProjectError,
    MissingTaskError,
    RepositoryError,
)
from ai_ent.persistence.repositories.executions import ExecutionRepository
from ai_ent.persistence.repositories.leases import LeaseRepository
from ai_ent.persistence.repositories.projects import ProjectRepository
from ai_ent.persistence.repositories.tasks import TaskRepository

__all__ = [
    "BootstrapRunRepository",
    "CheckpointRepository",
    "DuplicateBootstrapCheckpointError",
    "DuplicateBootstrapRunError",
    "DuplicateCheckpointError",
    "DuplicateDependencyError",
    "DuplicateExecutionError",
    "DuplicateLeaseError",
    "DuplicateProjectError",
    "DuplicateTaskError",
    "ExecutionRepository",
    "LeaseRepository",
    "MissingBootstrapCheckpointError",
    "MissingBootstrapRunError",
    "MissingCheckpointError",
    "MissingExecutionError",
    "MissingLeaseError",
    "MissingProjectError",
    "MissingTaskError",
    "ProjectRepository",
    "RepositoryError",
    "TaskRepository",
]
