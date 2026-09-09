from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ai_ent.persistence.models import (
    BootstrapCheckpoint,
    BootstrapCheckpointKind,
    BootstrapRun,
    BootstrapRunStatus,
)
from ai_ent.persistence.repositories.errors import (
    DuplicateBootstrapCheckpointError,
    DuplicateBootstrapRunError,
    MissingBootstrapCheckpointError,
    MissingBootstrapRunError,
    RepositoryError,
)

_GIT_REF_RE = re.compile(r"^[A-Za-z0-9._/@:+-]{1,128}$")


class BootstrapRunRepository:
    def create_run(
        self,
        session: Session,
        *,
        run_id: str,
        project_id: str,
        manifest_ref: str,
        status: BootstrapRunStatus = "pending",
        baseline_commit: str | None = None,
        current_stage: str | None = None,
        current_task_id: str | None = None,
        started_at: datetime | None = None,
    ) -> BootstrapRun:
        self._validate_git_ref("baseline_commit", baseline_commit)
        existing = session.get(BootstrapRun, run_id)
        if existing is not None:
            if (
                existing.project_id == project_id
                and existing.manifest_ref == manifest_ref
                and existing.baseline_commit == baseline_commit
            ):
                return existing
            raise DuplicateBootstrapRunError(f"bootstrap run already exists: {run_id}")
        run = BootstrapRun(
            id=run_id,
            project_id=project_id,
            status=status,
            manifest_ref=manifest_ref,
            baseline_commit=baseline_commit,
            current_stage=current_stage,
            current_task_id=current_task_id,
            started_at=started_at,
        )
        session.add(run)
        try:
            session.flush()
        except IntegrityError as exc:
            raise DuplicateBootstrapRunError(f"bootstrap run already exists or is invalid: {run_id}") from exc
        except SQLAlchemyError as exc:
            raise RepositoryError("bootstrap run persistence failed") from exc
        return run

    def get_run(self, session: Session, run_id: str) -> BootstrapRun | None:
        return session.get(BootstrapRun, run_id)

    def require_run(self, session: Session, run_id: str) -> BootstrapRun:
        run = self.get_run(session, run_id)
        if run is None:
            raise MissingBootstrapRunError(f"bootstrap run not found: {run_id}")
        return run

    def update_run_state(
        self,
        session: Session,
        run_id: str,
        *,
        status: BootstrapRunStatus | None = None,
        current_stage: str | None = None,
        current_task_id: str | None = None,
        last_completed_task_id: str | None = None,
        last_verified_commit: str | None = None,
        finished_at: datetime | None = None,
        failure_classification: str | None = None,
        blocked_reason: str | None = None,
    ) -> BootstrapRun:
        self._validate_git_ref("last_verified_commit", last_verified_commit)
        run = self.require_run(session, run_id)
        if status is not None:
            run.status = status
        if current_stage is not None:
            run.current_stage = current_stage
        if current_task_id is not None:
            run.current_task_id = current_task_id
        if last_completed_task_id is not None:
            run.last_completed_task_id = last_completed_task_id
        if last_verified_commit is not None:
            run.last_verified_commit = last_verified_commit
        if finished_at is not None:
            run.finished_at = finished_at
        if failure_classification is not None:
            run.failure_classification = failure_classification
        if blocked_reason is not None:
            run.blocked_reason = blocked_reason
        try:
            session.flush()
        except IntegrityError as exc:
            raise RepositoryError(f"invalid bootstrap run state: {run_id}") from exc
        except SQLAlchemyError as exc:
            raise RepositoryError("bootstrap run update failed") from exc
        return run

    def mark_blocked(
        self,
        session: Session,
        run_id: str,
        *,
        current_task_id: str | None,
        reason: str,
        classification: str = "blocked",
    ) -> BootstrapRun:
        return self.update_run_state(
            session,
            run_id,
            status="blocked",
            current_task_id=current_task_id,
            failure_classification=classification,
            blocked_reason=reason,
        )

    def mark_failed(
        self,
        session: Session,
        run_id: str,
        *,
        current_task_id: str | None,
        classification: str,
        reason: str,
        finished_at: datetime | None = None,
    ) -> BootstrapRun:
        return self.update_run_state(
            session,
            run_id,
            status="failed",
            current_task_id=current_task_id,
            failure_classification=classification,
            blocked_reason=reason,
            finished_at=finished_at,
        )

    def mark_completed(
        self,
        session: Session,
        run_id: str,
        *,
        last_completed_task_id: str,
        last_verified_commit: str,
        finished_at: datetime | None = None,
    ) -> BootstrapRun:
        return self.update_run_state(
            session,
            run_id,
            status="completed",
            last_completed_task_id=last_completed_task_id,
            last_verified_commit=last_verified_commit,
            finished_at=finished_at,
        )

    def append_checkpoint(
        self,
        session: Session,
        *,
        checkpoint_id: str,
        run_id: str,
        checkpoint_kind: BootstrapCheckpointKind,
        state: str,
        task_id: str | None = None,
        execution_id: str | None = None,
        verified_commit: str | None = None,
        tree_hash: str | None = None,
        sequence: int | None = None,
    ) -> BootstrapCheckpoint:
        self._validate_git_ref("verified_commit", verified_commit)
        self._validate_git_ref("tree_hash", tree_hash)
        existing = session.get(BootstrapCheckpoint, checkpoint_id)
        if existing is not None:
            if (
                existing.run_id == run_id
                and existing.checkpoint_kind == checkpoint_kind
                and existing.task_id == task_id
                and existing.execution_id == execution_id
                and existing.verified_commit == verified_commit
                and existing.tree_hash == tree_hash
                and existing.state == state
            ):
                return existing
            raise DuplicateBootstrapCheckpointError(f"bootstrap checkpoint already exists: {checkpoint_id}")
        selected_sequence = sequence if sequence is not None else self.next_checkpoint_sequence(session, run_id)
        checkpoint = BootstrapCheckpoint(
            id=checkpoint_id,
            run_id=run_id,
            sequence=selected_sequence,
            checkpoint_kind=checkpoint_kind,
            task_id=task_id,
            execution_id=execution_id,
            verified_commit=verified_commit,
            tree_hash=tree_hash,
            state=state,
        )
        session.add(checkpoint)
        try:
            session.flush()
        except IntegrityError as exc:
            raise DuplicateBootstrapCheckpointError(
                f"bootstrap checkpoint already exists or is invalid: {checkpoint_id}"
            ) from exc
        except SQLAlchemyError as exc:
            raise RepositoryError("bootstrap checkpoint persistence failed") from exc
        return checkpoint

    def get_checkpoint(self, session: Session, checkpoint_id: str) -> BootstrapCheckpoint | None:
        return session.get(BootstrapCheckpoint, checkpoint_id)

    def require_checkpoint(self, session: Session, checkpoint_id: str) -> BootstrapCheckpoint:
        checkpoint = self.get_checkpoint(session, checkpoint_id)
        if checkpoint is None:
            raise MissingBootstrapCheckpointError(f"bootstrap checkpoint not found: {checkpoint_id}")
        return checkpoint

    def list_checkpoints(self, session: Session, run_id: str) -> list[BootstrapCheckpoint]:
        statement = (
            select(BootstrapCheckpoint)
            .where(BootstrapCheckpoint.run_id == run_id)
            .order_by(BootstrapCheckpoint.sequence, BootstrapCheckpoint.created_at, BootstrapCheckpoint.id)
        )
        return list(session.scalars(statement).all())

    def latest_checkpoint(self, session: Session, run_id: str) -> BootstrapCheckpoint | None:
        statement = (
            select(BootstrapCheckpoint)
            .where(BootstrapCheckpoint.run_id == run_id)
            .order_by(desc(BootstrapCheckpoint.sequence), desc(BootstrapCheckpoint.created_at), desc(BootstrapCheckpoint.id))
            .limit(1)
        )
        return session.scalars(statement).first()

    def next_checkpoint_sequence(self, session: Session, run_id: str) -> int:
        statement = select(func.max(BootstrapCheckpoint.sequence)).where(BootstrapCheckpoint.run_id == run_id)
        return (session.scalar(statement) or 0) + 1

    def _validate_git_ref(self, field: str, value: str | None) -> None:
        if value is not None and not _GIT_REF_RE.fullmatch(value):
            raise RepositoryError(f"invalid git reference: {field}")
