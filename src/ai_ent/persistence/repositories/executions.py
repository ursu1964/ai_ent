from __future__ import annotations

from datetime import datetime

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ai_ent.persistence.models import Execution, ExecutionStatus
from ai_ent.persistence.repositories.errors import (
    DuplicateExecutionError,
    MissingExecutionError,
    RepositoryError,
)


class ExecutionRepository:
    def create(
        self,
        session: Session,
        *,
        execution_id: str,
        task_id: str,
        executor_type: str,
        status: ExecutionStatus = "pending",
        attempt: int | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        terminal_state: str | None = None,
        error_classification: str | None = None,
        candidate_tree_hash: str | None = None,
        commit_hash: str | None = None,
    ) -> Execution:
        selected_attempt = attempt if attempt is not None else self.next_attempt(session, task_id)
        execution = Execution(
            id=execution_id,
            task_id=task_id,
            executor_type=executor_type,
            status=status,
            attempt=selected_attempt,
            started_at=started_at,
            finished_at=finished_at,
            terminal_state=terminal_state,
            error_classification=error_classification,
            candidate_tree_hash=candidate_tree_hash,
            commit_hash=commit_hash,
        )
        session.add(execution)
        try:
            session.flush()
        except IntegrityError as exc:
            raise DuplicateExecutionError(f"execution already exists or is invalid: {execution_id}") from exc
        except SQLAlchemyError as exc:
            raise RepositoryError("execution persistence failed") from exc
        return execution

    def get(self, session: Session, execution_id: str) -> Execution | None:
        return session.get(Execution, execution_id)

    def require(self, session: Session, execution_id: str) -> Execution:
        execution = self.get(session, execution_id)
        if execution is None:
            raise MissingExecutionError(f"execution not found: {execution_id}")
        return execution

    def list_by_task(self, session: Session, task_id: str) -> list[Execution]:
        statement = (
            select(Execution)
            .where(Execution.task_id == task_id)
            .order_by(Execution.attempt, Execution.created_at, Execution.id)
        )
        return list(session.scalars(statement).all())

    def list_by_status(
        self,
        session: Session,
        status: ExecutionStatus,
        *,
        task_id: str | None = None,
    ) -> list[Execution]:
        statement = select(Execution).where(Execution.status == status).order_by(Execution.created_at)
        if task_id is not None:
            statement = statement.where(Execution.task_id == task_id)
        return list(session.scalars(statement).all())

    def latest_for_task(self, session: Session, task_id: str) -> Execution | None:
        statement = (
            select(Execution)
            .where(Execution.task_id == task_id)
            .order_by(desc(Execution.attempt), desc(Execution.created_at), desc(Execution.id))
            .limit(1)
        )
        return session.scalars(statement).first()

    def latest_attempt(self, session: Session, task_id: str) -> int | None:
        statement = select(func.max(Execution.attempt)).where(Execution.task_id == task_id)
        return session.scalar(statement)

    def next_attempt(self, session: Session, task_id: str) -> int:
        return (self.latest_attempt(session, task_id) or 0) + 1

    def update_lifecycle(
        self,
        session: Session,
        execution_id: str,
        *,
        status: ExecutionStatus,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        terminal_state: str | None = None,
        error_classification: str | None = None,
        candidate_tree_hash: str | None = None,
        commit_hash: str | None = None,
    ) -> Execution:
        execution = self.require(session, execution_id)
        execution.status = status
        if started_at is not None:
            execution.started_at = started_at
        if finished_at is not None:
            execution.finished_at = finished_at
        if terminal_state is not None:
            execution.terminal_state = terminal_state
        if error_classification is not None:
            execution.error_classification = error_classification
        if candidate_tree_hash is not None:
            execution.candidate_tree_hash = candidate_tree_hash
        if commit_hash is not None:
            execution.commit_hash = commit_hash
        try:
            session.flush()
        except IntegrityError as exc:
            raise RepositoryError(f"invalid execution lifecycle state: {status}") from exc
        return execution
