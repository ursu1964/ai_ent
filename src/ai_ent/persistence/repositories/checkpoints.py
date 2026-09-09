from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ai_ent.persistence.models import Checkpoint, CheckpointType
from ai_ent.persistence.repositories.errors import (
    DuplicateCheckpointError,
    MissingCheckpointError,
    RepositoryError,
)


class CheckpointRepository:
    def create(
        self,
        session: Session,
        *,
        checkpoint_id: str,
        task_id: str,
        checkpoint_type: CheckpointType,
        state: str,
        execution_id: str | None = None,
        commit_hash: str | None = None,
        tree_hash: str | None = None,
    ) -> Checkpoint:
        checkpoint = Checkpoint(
            id=checkpoint_id,
            task_id=task_id,
            execution_id=execution_id,
            checkpoint_type=checkpoint_type,
            state=state,
            commit_hash=commit_hash,
            tree_hash=tree_hash,
        )
        session.add(checkpoint)
        try:
            session.flush()
        except IntegrityError as exc:
            raise DuplicateCheckpointError(
                f"checkpoint already exists or is invalid: {checkpoint_id}"
            ) from exc
        except SQLAlchemyError as exc:
            raise RepositoryError("checkpoint persistence failed") from exc
        return checkpoint

    def get(self, session: Session, checkpoint_id: str) -> Checkpoint | None:
        return session.get(Checkpoint, checkpoint_id)

    def require(self, session: Session, checkpoint_id: str) -> Checkpoint:
        checkpoint = self.get(session, checkpoint_id)
        if checkpoint is None:
            raise MissingCheckpointError(f"checkpoint not found: {checkpoint_id}")
        return checkpoint

    def list_by_task(self, session: Session, task_id: str) -> list[Checkpoint]:
        statement = (
            select(Checkpoint)
            .where(Checkpoint.task_id == task_id)
            .order_by(Checkpoint.created_at, Checkpoint.id)
        )
        return list(session.scalars(statement).all())

    def list_by_execution(self, session: Session, execution_id: str) -> list[Checkpoint]:
        statement = (
            select(Checkpoint)
            .where(Checkpoint.execution_id == execution_id)
            .order_by(Checkpoint.created_at, Checkpoint.id)
        )
        return list(session.scalars(statement).all())

    def latest_for_task(self, session: Session, task_id: str) -> Checkpoint | None:
        statement = (
            select(Checkpoint)
            .where(Checkpoint.task_id == task_id)
            .order_by(desc(Checkpoint.created_at), desc(Checkpoint.id))
            .limit(1)
        )
        return session.scalars(statement).first()

    def latest_for_execution(self, session: Session, execution_id: str) -> Checkpoint | None:
        statement = (
            select(Checkpoint)
            .where(Checkpoint.execution_id == execution_id)
            .order_by(desc(Checkpoint.created_at), desc(Checkpoint.id))
            .limit(1)
        )
        return session.scalars(statement).first()
