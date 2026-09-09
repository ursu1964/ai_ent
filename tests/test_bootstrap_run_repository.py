from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.persistence.models import Base
from ai_ent.persistence.repositories import (
    BootstrapRunRepository,
    DuplicateBootstrapCheckpointError,
    DuplicateBootstrapRunError,
    MissingBootstrapCheckpointError,
    MissingBootstrapRunError,
    ProjectRepository,
    RepositoryError,
    TaskRepository,
)


def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def seed_project_and_tasks(session: Session) -> None:
    projects = ProjectRepository()
    tasks = TaskRepository()
    projects.create(session, project_id="project-1", name="AI Enterprise")
    tasks.create(session, task_id="TASK-A", project_id="project-1", title="A")
    tasks.create(session, task_id="TASK-B", project_id="project-1", title="B")


def test_create_get_require_and_idempotent_run_creation() -> None:
    factory = session_factory()
    repository = BootstrapRunRepository()
    with factory() as session:
        seed_project_and_tasks(session)
        created = repository.create_run(
            session,
            run_id="run-1",
            project_id="project-1",
            manifest_ref="manifest/bootstrap",
            baseline_commit="abc123",
        )
        repeated = repository.create_run(
            session,
            run_id="run-1",
            project_id="project-1",
            manifest_ref="manifest/bootstrap",
            baseline_commit="abc123",
        )

        assert created is repeated
        assert repository.get_run(session, "run-1") is created
        assert repository.require_run(session, "run-1").manifest_ref == "manifest/bootstrap"

        try:
            repository.require_run(session, "missing")
        except MissingBootstrapRunError:
            pass
        else:
            raise AssertionError("missing bootstrap run was not rejected")


def test_duplicate_run_with_conflicting_identity_is_rejected() -> None:
    factory = session_factory()
    repository = BootstrapRunRepository()
    with factory() as session:
        seed_project_and_tasks(session)
        repository.create_run(session, run_id="run-1", project_id="project-1", manifest_ref="manifest/bootstrap")
        try:
            repository.create_run(session, run_id="run-1", project_id="project-1", manifest_ref="other")
        except DuplicateBootstrapRunError:
            pass
        else:
            raise AssertionError("conflicting bootstrap run was not rejected")

        try:
            repository.create_run(
                session,
                run_id="run-1",
                project_id="project-1",
                manifest_ref="manifest/bootstrap",
                baseline_commit="different",
            )
        except DuplicateBootstrapRunError:
            pass
        else:
            raise AssertionError("conflicting bootstrap baseline was not rejected")


def test_update_lifecycle_block_fail_complete_and_git_refs() -> None:
    factory = session_factory()
    repository = BootstrapRunRepository()
    with factory() as session:
        seed_project_and_tasks(session)
        repository.create_run(session, run_id="run-1", project_id="project-1", manifest_ref="manifest/bootstrap")

        run = repository.update_run_state(
            session,
            "run-1",
            status="running",
            current_stage="B04",
            current_task_id="TASK-A",
        )
        assert run.status == "running"
        assert run.current_task_id == "TASK-A"
        assert run.last_completed_task_id is None

        blocked = repository.mark_blocked(
            session,
            "run-1",
            current_task_id="TASK-A",
            reason="waiting for operator",
        )
        assert blocked.status == "blocked"
        assert blocked.blocked_reason == "waiting for operator"

        failed = repository.mark_failed(
            session,
            "run-1",
            current_task_id="TASK-A",
            classification="verification_failed",
            reason="tests failed",
        )
        assert failed.status == "failed"
        assert failed.failure_classification == "verification_failed"

        completed = repository.mark_completed(
            session,
            "run-1",
            last_completed_task_id="TASK-A",
            last_verified_commit="def456",
            finished_at=datetime.now(UTC),
        )
        assert completed.status == "completed"
        assert completed.last_completed_task_id == "TASK-A"
        assert completed.last_verified_commit == "def456"

        try:
            repository.update_run_state(session, "run-1", last_verified_commit="bad ref with space")
        except RepositoryError as exc:
            assert "password" not in str(exc).lower()
        else:
            raise AssertionError("invalid git reference was not rejected")


def test_append_checkpoints_are_historical_and_latest_is_by_sequence() -> None:
    factory = session_factory()
    repository = BootstrapRunRepository()
    with factory() as session:
        seed_project_and_tasks(session)
        repository.create_run(session, run_id="run-1", project_id="project-1", manifest_ref="manifest/bootstrap")
        first = repository.append_checkpoint(
            session,
            checkpoint_id="checkpoint-1",
            run_id="run-1",
            checkpoint_kind="task_completed",
            task_id="TASK-A",
            verified_commit="abc123",
            state='{"last_completed":"TASK-A"}',
        )
        second = repository.append_checkpoint(
            session,
            checkpoint_id="checkpoint-2",
            run_id="run-1",
            checkpoint_kind="state_snapshot",
            task_id="TASK-B",
            state='{"current":"TASK-B"}',
        )

        assert first.sequence == 1
        assert second.sequence == 2
        assert [checkpoint.id for checkpoint in repository.list_checkpoints(session, "run-1")] == [
            "checkpoint-1",
            "checkpoint-2",
        ]
        assert repository.latest_checkpoint(session, "run-1").id == "checkpoint-2"  # type: ignore[union-attr]
        assert repository.require_checkpoint(session, "checkpoint-1").verified_commit == "abc123"

        try:
            repository.require_checkpoint(session, "missing")
        except MissingBootstrapCheckpointError:
            pass
        else:
            raise AssertionError("missing bootstrap checkpoint was not rejected")


def test_duplicate_checkpoint_sequence_and_invalid_fk_are_rejected() -> None:
    factory = session_factory()
    repository = BootstrapRunRepository()
    with factory() as session:
        seed_project_and_tasks(session)
        repository.create_run(session, run_id="run-1", project_id="project-1", manifest_ref="manifest/bootstrap")
        repository.append_checkpoint(
            session,
            checkpoint_id="checkpoint-1",
            run_id="run-1",
            sequence=1,
            checkpoint_kind="state_snapshot",
            state="{}",
        )
        try:
            repository.append_checkpoint(
                session,
                checkpoint_id="checkpoint-2",
                run_id="run-1",
                sequence=1,
                checkpoint_kind="state_snapshot",
                state="{}",
            )
        except DuplicateBootstrapCheckpointError:
            session.rollback()
        else:
            raise AssertionError("duplicate checkpoint sequence was not rejected")

    with factory() as session:
        try:
            repository.create_run(session, run_id="run-bad", project_id="missing", manifest_ref="manifest/bootstrap")
        except DuplicateBootstrapRunError:
            session.rollback()
        else:
            raise AssertionError("invalid project foreign key was not rejected")


def test_transaction_rollback_and_no_hidden_commit() -> None:
    factory = session_factory()
    repository = BootstrapRunRepository()
    with factory() as session:
        seed_project_and_tasks(session)
        repository.create_run(session, run_id="run-1", project_id="project-1", manifest_ref="manifest/bootstrap")
        repository.append_checkpoint(
            session,
            checkpoint_id="checkpoint-1",
            run_id="run-1",
            checkpoint_kind="state_snapshot",
            state="{}",
        )
        session.rollback()

        assert repository.get_run(session, "run-1") is None
        assert repository.get_checkpoint(session, "checkpoint-1") is None
