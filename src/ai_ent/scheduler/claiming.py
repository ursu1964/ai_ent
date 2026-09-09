from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ai_ent.persistence.models import Execution, Task, TaskLease
from ai_ent.persistence.repositories.executions import ExecutionRepository
from ai_ent.persistence.repositories.leases import LeaseRepository

ClaimStatus = Literal["CLAIMED", "NOT_FOUND", "NOT_CLAIMABLE", "ALREADY_LEASED"]
LeaseActionStatus = Literal["UPDATED", "NOT_FOUND", "WRONG_OWNER", "EXPIRED", "NOT_ACTIVE"]


@dataclass(frozen=True)
class ClaimResult:
    status: ClaimStatus
    task_id: str
    owner_id: str
    lease: TaskLease | None = None
    execution: Execution | None = None

    @property
    def claimed(self) -> bool:
        return self.status == "CLAIMED"


@dataclass(frozen=True)
class LeaseActionResult:
    status: LeaseActionStatus
    lease: TaskLease | None = None

    @property
    def updated(self) -> bool:
        return self.status == "UPDATED"


class TaskClaimingService:
    def __init__(
        self,
        executions: ExecutionRepository | None = None,
        leases: LeaseRepository | None = None,
    ) -> None:
        self.executions = executions or ExecutionRepository()
        self.leases = leases or LeaseRepository()

    def claim_task(
        self,
        session: Session,
        *,
        task_id: str,
        owner_id: str,
        lease_duration: timedelta,
        executor_type: str = "codex",
        lease_id: str | None = None,
        execution_id: str | None = None,
    ) -> ClaimResult:
        now = database_now(session)
        try:
            task = session.scalars(
                select(Task).where(Task.id == task_id).with_for_update(nowait=True)
            ).one_or_none()
        except OperationalError:
            return ClaimResult("ALREADY_LEASED", task_id=task_id, owner_id=owner_id)

        if task is None:
            return ClaimResult("NOT_FOUND", task_id=task_id, owner_id=owner_id)

        active_lease = self.leases.active_for_task(session, task_id)
        if active_lease is not None:
            if active_lease.expires_at > now:
                return ClaimResult("ALREADY_LEASED", task_id=task_id, owner_id=owner_id)
            active_lease.status = "expired"

        if task.status != "pending" or not task.schedulable or task.execution_class != "implementation":
            return ClaimResult("NOT_CLAIMABLE", task_id=task_id, owner_id=owner_id)

        execution = self.executions.create(
            session,
            execution_id=execution_id or f"execution-{uuid.uuid4().hex}",
            task_id=task_id,
            executor_type=executor_type,
            status="running",
            started_at=now,
        )
        lease = self.leases.create(
            session,
            lease_id=lease_id or f"lease-{uuid.uuid4().hex}",
            task_id=task_id,
            execution_id=execution.id,
            owner_id=owner_id,
            acquired_at=now,
            expires_at=now + lease_duration,
        )
        task.status = "running"
        session.flush()
        return ClaimResult("CLAIMED", task_id=task_id, owner_id=owner_id, lease=lease, execution=execution)

    def renew(
        self,
        session: Session,
        *,
        lease_id: str,
        owner_id: str,
        lease_duration: timedelta,
    ) -> LeaseActionResult:
        now = database_now(session)
        lease = self.leases.get(session, lease_id)
        if lease is None:
            return LeaseActionResult("NOT_FOUND")
        if lease.owner_id != owner_id:
            return LeaseActionResult("WRONG_OWNER", lease)
        if lease.status != "active":
            return LeaseActionResult("NOT_ACTIVE", lease)
        if lease.expires_at <= now:
            lease.status = "expired"
            session.flush()
            return LeaseActionResult("EXPIRED", lease)
        lease.renewed_at = now
        lease.expires_at = now + lease_duration
        session.flush()
        return LeaseActionResult("UPDATED", lease)

    def release(self, session: Session, *, lease_id: str, owner_id: str) -> LeaseActionResult:
        return self._finish(session, lease_id=lease_id, owner_id=owner_id, status="released")

    def complete(self, session: Session, *, lease_id: str, owner_id: str) -> LeaseActionResult:
        return self._finish(session, lease_id=lease_id, owner_id=owner_id, status="completed")

    def has_valid_lease(self, session: Session, *, task_id: str, owner_id: str) -> bool:
        now = database_now(session)
        lease = self.leases.active_for_task(session, task_id)
        return lease is not None and lease.owner_id == owner_id and lease.expires_at > now

    def _finish(
        self,
        session: Session,
        *,
        lease_id: str,
        owner_id: str,
        status: Literal["released", "completed"],
    ) -> LeaseActionResult:
        now = database_now(session)
        lease = self.leases.get(session, lease_id)
        if lease is None:
            return LeaseActionResult("NOT_FOUND")
        if lease.owner_id != owner_id:
            return LeaseActionResult("WRONG_OWNER", lease)
        if lease.status != "active":
            return LeaseActionResult("NOT_ACTIVE", lease)
        if lease.expires_at <= now:
            lease.status = "expired"
            session.flush()
            return LeaseActionResult("EXPIRED", lease)
        lease.status = status
        if status == "released":
            lease.released_at = now
        else:
            lease.completed_at = now
        session.flush()
        return LeaseActionResult("UPDATED", lease)


def database_now(session: Session) -> datetime:
    value = session.scalar(select(func.now()))
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.now(UTC)
