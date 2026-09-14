from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_ent.persistence.models import Execution, Task, TaskLease


class ExecutionOwnershipPersistenceError(RuntimeError):
    """Raised when a claimed execution cannot be verified after commit."""


@dataclass(frozen=True)
class DurableExecutionOwnership:
    task_id: str
    execution_id: str
    lease_id: str
    owner_id: str


def persist_execution_ownership(
    session: Session,
    *,
    task_id: str,
    execution_id: str,
    owner_id: str,
    lease_id: str | None = None,
) -> DurableExecutionOwnership:
    """Commit and re-read the execution/lease ownership boundary."""

    session.commit()
    execution = session.get(Execution, execution_id)
    task = session.get(Task, task_id)
    if lease_id is not None:
        lease = session.get(TaskLease, lease_id)
    else:
        lease = session.scalars(
            select(TaskLease).where(
                TaskLease.task_id == task_id,
                TaskLease.execution_id == execution_id,
                TaskLease.status == "active",
            )
        ).one_or_none()

    if execution is None:
        raise ExecutionOwnershipPersistenceError(f"execution ownership was not persisted: {execution_id}")
    if task is None:
        raise ExecutionOwnershipPersistenceError(f"task ownership was not persisted: {task_id}")
    if lease is None:
        raise ExecutionOwnershipPersistenceError(f"lease ownership was not persisted: {execution_id}")
    if execution.task_id != task_id:
        raise ExecutionOwnershipPersistenceError(f"execution task mismatch: {execution_id}")
    if lease.task_id != task_id or lease.execution_id != execution_id:
        raise ExecutionOwnershipPersistenceError(f"lease execution mismatch: {execution_id}")
    if lease.owner_id != owner_id or lease.status != "active":
        raise ExecutionOwnershipPersistenceError(f"lease ownership invalid: {lease.id}")
    if task.status != "running" or execution.status not in {"pending", "running"}:
        raise ExecutionOwnershipPersistenceError(f"execution ownership not runnable: {execution_id}")

    ownership = DurableExecutionOwnership(
        task_id=task_id,
        execution_id=execution_id,
        lease_id=lease.id,
        owner_id=owner_id,
    )
    session.commit()
    return ownership
