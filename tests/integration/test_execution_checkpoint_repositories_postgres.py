from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from alembic import command
from sqlalchemy import delete

from ai_ent.persistence.config import DatabaseConfigError, load_database_settings
from ai_ent.persistence.database import Database
from ai_ent.persistence.migrations import build_alembic_config
from ai_ent.persistence.models import Checkpoint, Execution, Project, Task, TaskDependency
from ai_ent.persistence.repositories import (
    CheckpointRepository,
    DuplicateCheckpointError,
    DuplicateExecutionError,
    ExecutionRepository,
    ProjectRepository,
    TaskRepository,
)


def integration_database() -> Database:
    try:
        config = build_alembic_config(Path("alembic.ini"), Path(".env"))
        settings = load_database_settings(Path(".env"))
    except DatabaseConfigError as exc:
        raise unittest.SkipTest(f"execution/checkpoint integration blocked: {exc}") from exc
    command.upgrade(config, "head")
    return Database(settings)


def clean_tables(database: Database) -> None:
    with database.session() as session:
        session.execute(delete(Checkpoint))
        session.execute(delete(Execution))
        session.execute(delete(TaskDependency))
        session.execute(delete(Task))
        session.execute(delete(Project))


def test_postgres_execution_checkpoint_repositories() -> None:
    database = integration_database()
    clean_tables(database)
    projects = ProjectRepository()
    tasks = TaskRepository()
    executions = ExecutionRepository()
    checkpoints = CheckpointRepository()
    suffix = uuid.uuid4().hex[:8]

    try:
        with database.session() as session:
            project_id = f"project-{suffix}"
            task_id = f"TASK-{suffix}"
            projects.create(session, project_id=project_id, name=f"Project {suffix}")
            tasks.create(session, task_id=task_id, project_id=project_id, title="Task")
            first = executions.create(
                session,
                execution_id=f"execution-1-{suffix}",
                task_id=task_id,
                executor_type="fake",
                status="failed",
            )
            second = executions.create(
                session,
                execution_id=f"execution-2-{suffix}",
                task_id=task_id,
                executor_type="codex",
                status="succeeded",
            )
            checkpoints.create(
                session,
                checkpoint_id=f"checkpoint-1-{suffix}",
                task_id=task_id,
                execution_id=second.id,
                checkpoint_type="execution",
                state='{"ok":true}',
            )

        with database.session() as session:
            assert first.attempt == 1
            assert second.attempt == 2
            assert executions.latest_for_task(session, task_id).id == second.id  # type: ignore[union-attr]
            assert checkpoints.latest_for_execution(session, second.id).state == '{"ok":true}'  # type: ignore[union-attr]
    finally:
        clean_tables(database)
        database.dispose()


def test_postgres_execution_checkpoint_constraint_translation() -> None:
    database = integration_database()
    clean_tables(database)
    executions = ExecutionRepository()
    checkpoints = CheckpointRepository()
    suffix = uuid.uuid4().hex[:8]

    try:
        with database.session() as session:
            try:
                executions.create(
                    session,
                    execution_id=f"execution-{suffix}",
                    task_id="missing",
                    executor_type="fake",
                )
            except DuplicateExecutionError:
                session.rollback()
            else:
                raise AssertionError("invalid execution FK was not rejected")

        with database.session() as session:
            try:
                checkpoints.create(
                    session,
                    checkpoint_id=f"checkpoint-{suffix}",
                    task_id="missing",
                    checkpoint_type="task",
                    state="bad",
                )
            except DuplicateCheckpointError:
                session.rollback()
            else:
                raise AssertionError("invalid checkpoint FK was not rejected")
    finally:
        clean_tables(database)
        database.dispose()
