from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.persistence.models import Base
from ai_ent.persistence.repositories import (
    CheckpointRepository,
    DuplicateCheckpointError,
    DuplicateExecutionError,
    ExecutionRepository,
    MissingCheckpointError,
    MissingExecutionError,
    ProjectRepository,
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


def seed_task(session: Session, task_id: str = "TASK-1") -> None:
    ProjectRepository().create(session, project_id="project-1", name="Project One")
    TaskRepository().create(session, task_id=task_id, project_id="project-1", title="Task One")


def test_create_get_execution() -> None:
    factory = session_factory()
    executions = ExecutionRepository()
    with factory() as session:
        seed_task(session)
        execution = executions.create(
            session,
            execution_id="execution-1",
            task_id="TASK-1",
            executor_type="fake",
        )
        session.commit()

        found = executions.get(session, "execution-1")

    assert execution.id == "execution-1"
    assert found is not None
    assert found.executor_type == "fake"
    assert found.attempt == 1


def test_duplicate_and_missing_execution_behavior() -> None:
    factory = session_factory()
    executions = ExecutionRepository()
    with factory() as session:
        seed_task(session)
        executions.create(session, execution_id="execution-1", task_id="TASK-1", executor_type="fake")
        try:
            executions.create(
                session,
                execution_id="execution-1",
                task_id="TASK-1",
                executor_type="fake",
            )
        except DuplicateExecutionError:
            session.rollback()
        else:
            raise AssertionError("duplicate execution was not rejected")

        try:
            executions.require(session, "missing")
        except MissingExecutionError:
            pass
        else:
            raise AssertionError("missing execution was not rejected")


def test_multiple_attempts_latest_and_filters() -> None:
    factory = session_factory()
    executions = ExecutionRepository()
    with factory() as session:
        seed_task(session)
        first = executions.create(
            session,
            execution_id="execution-1",
            task_id="TASK-1",
            executor_type="fake",
            status="failed",
        )
        second = executions.create(
            session,
            execution_id="execution-2",
            task_id="TASK-1",
            executor_type="codex",
            status="succeeded",
        )
        session.commit()

        assert first.attempt == 1
        assert second.attempt == 2
        assert executions.latest_attempt(session, "TASK-1") == 2
        assert executions.latest_for_task(session, "TASK-1").id == "execution-2"  # type: ignore[union-attr]
        assert [item.id for item in executions.list_by_task(session, "TASK-1")] == [
            "execution-1",
            "execution-2",
        ]
        assert [item.id for item in executions.list_by_status(session, "succeeded")] == [
            "execution-2"
        ]


def test_execution_lifecycle_fields_persisted() -> None:
    factory = session_factory()
    executions = ExecutionRepository()
    started = datetime(2026, 9, 9, 1, 0, tzinfo=UTC)
    finished = datetime(2026, 9, 9, 1, 1, tzinfo=UTC)
    with factory() as session:
        seed_task(session)
        executions.create(session, execution_id="execution-1", task_id="TASK-1", executor_type="codex")
        updated = executions.update_lifecycle(
            session,
            "execution-1",
            status="succeeded",
            started_at=started,
            finished_at=finished,
            terminal_state="success",
            error_classification="none",
            candidate_tree_hash="a" * 40,
            commit_hash="b" * 40,
        )
        session.commit()

    assert updated.status == "succeeded"
    assert updated.started_at == started
    assert updated.finished_at == finished
    assert updated.terminal_state == "success"
    assert updated.candidate_tree_hash == "a" * 40


def test_create_get_checkpoint_and_execution_link() -> None:
    factory = session_factory()
    executions = ExecutionRepository()
    checkpoints = CheckpointRepository()
    with factory() as session:
        seed_task(session)
        executions.create(session, execution_id="execution-1", task_id="TASK-1", executor_type="fake")
        checkpoint = checkpoints.create(
            session,
            checkpoint_id="checkpoint-1",
            task_id="TASK-1",
            execution_id="execution-1",
            checkpoint_type="execution",
            state='{"phase":"done"}',
            commit_hash="b" * 40,
            tree_hash="c" * 40,
        )
        session.commit()

        found = checkpoints.get(session, "checkpoint-1")

    assert checkpoint.id == "checkpoint-1"
    assert found is not None
    assert found.execution_id == "execution-1"
    assert found.state == '{"phase":"done"}'


def test_multiple_checkpoints_latest_queries() -> None:
    factory = session_factory()
    executions = ExecutionRepository()
    checkpoints = CheckpointRepository()
    with factory() as session:
        seed_task(session)
        executions.create(session, execution_id="execution-1", task_id="TASK-1", executor_type="fake")
        checkpoints.create(
            session,
            checkpoint_id="checkpoint-1",
            task_id="TASK-1",
            execution_id="execution-1",
            checkpoint_type="execution",
            state="first",
        )
        checkpoints.create(
            session,
            checkpoint_id="checkpoint-2",
            task_id="TASK-1",
            execution_id="execution-1",
            checkpoint_type="execution",
            state="second",
        )
        session.commit()

        assert [item.id for item in checkpoints.list_by_task(session, "TASK-1")] == [
            "checkpoint-1",
            "checkpoint-2",
        ]
        assert [item.id for item in checkpoints.list_by_execution(session, "execution-1")] == [
            "checkpoint-1",
            "checkpoint-2",
        ]
        assert checkpoints.latest_for_task(session, "TASK-1").id == "checkpoint-2"  # type: ignore[union-attr]
        assert checkpoints.latest_for_execution(session, "execution-1").id == "checkpoint-2"  # type: ignore[union-attr]


def test_duplicate_and_missing_checkpoint_behavior() -> None:
    factory = session_factory()
    executions = ExecutionRepository()
    checkpoints = CheckpointRepository()
    with factory() as session:
        seed_task(session)
        executions.create(session, execution_id="execution-1", task_id="TASK-1", executor_type="fake")
        checkpoints.create(
            session,
            checkpoint_id="checkpoint-1",
            task_id="TASK-1",
            execution_id="execution-1",
            checkpoint_type="execution",
            state="first",
        )
        try:
            checkpoints.create(
                session,
                checkpoint_id="checkpoint-1",
                task_id="TASK-1",
                checkpoint_type="task",
                state="duplicate",
            )
        except DuplicateCheckpointError:
            session.rollback()
        else:
            raise AssertionError("duplicate checkpoint was not rejected")

        try:
            checkpoints.require(session, "missing")
        except MissingCheckpointError:
            pass
        else:
            raise AssertionError("missing checkpoint was not rejected")


def test_invalid_task_execution_and_checkpoint_fk_rejected() -> None:
    factory = session_factory()
    executions = ExecutionRepository()
    checkpoints = CheckpointRepository()
    with factory() as session:
        try:
            executions.create(session, execution_id="execution-1", task_id="missing", executor_type="fake")
        except DuplicateExecutionError:
            session.rollback()
        else:
            raise AssertionError("invalid execution FK was not rejected")

        try:
            checkpoints.create(
                session,
                checkpoint_id="checkpoint-1",
                task_id="missing",
                checkpoint_type="task",
                state="bad",
            )
        except DuplicateCheckpointError:
            session.rollback()
        else:
            raise AssertionError("invalid checkpoint FK was not rejected")


def test_transaction_rollback_across_execution_and_checkpoint() -> None:
    factory = session_factory()
    executions = ExecutionRepository()
    checkpoints = CheckpointRepository()
    with factory() as session:
        seed_task(session)
        try:
            executions.create(session, execution_id="execution-1", task_id="TASK-1", executor_type="fake")
            checkpoints.create(
                session,
                checkpoint_id="checkpoint-1",
                task_id="TASK-1",
                execution_id="execution-1",
                checkpoint_type="execution",
                state="created",
            )
            raise RuntimeError("stop")
        except RuntimeError:
            session.rollback()

        assert executions.get(session, "execution-1") is None
        assert checkpoints.get(session, "checkpoint-1") is None
