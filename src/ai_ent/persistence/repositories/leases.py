from __future__ import annotations

from datetime import datetime

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ai_ent.persistence.models import LeaseStatus, TaskLease
from ai_ent.persistence.repositories.errors import (
    DuplicateLeaseError,
    MissingLeaseError,
    RepositoryError,
)


class LeaseRepository:
    def create(
        self,
        session: Session,
        *,
        lease_id: str,
        task_id: str,
        execution_id: str,
        owner_id: str,
        acquired_at: datetime,
        expires_at: datetime,
        status: LeaseStatus = "active",
    ) -> TaskLease:
        lease = TaskLease(
            id=lease_id,
            task_id=task_id,
            execution_id=execution_id,
            owner_id=owner_id,
            status=status,
            acquired_at=acquired_at,
            renewed_at=acquired_at,
            expires_at=expires_at,
        )
        session.add(lease)
        try:
            session.flush()
        except IntegrityError as exc:
            raise DuplicateLeaseError(f"lease already exists or is invalid: {lease_id}") from exc
        except SQLAlchemyError as exc:
            raise RepositoryError("lease persistence failed") from exc
        return lease

    def get(self, session: Session, lease_id: str) -> TaskLease | None:
        return session.get(TaskLease, lease_id)

    def require(self, session: Session, lease_id: str) -> TaskLease:
        lease = self.get(session, lease_id)
        if lease is None:
            raise MissingLeaseError(f"lease not found: {lease_id}")
        return lease

    def active_for_task(self, session: Session, task_id: str) -> TaskLease | None:
        statement = (
            select(TaskLease)
            .where(TaskLease.task_id == task_id, TaskLease.status == "active")
            .order_by(desc(TaskLease.acquired_at), desc(TaskLease.id))
            .limit(1)
        )
        return session.scalars(statement).first()

    def list_by_task(self, session: Session, task_id: str) -> list[TaskLease]:
        statement = (
            select(TaskLease)
            .where(TaskLease.task_id == task_id)
            .order_by(TaskLease.acquired_at, TaskLease.id)
        )
        return list(session.scalars(statement).all())
