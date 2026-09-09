from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from alembic import command
from sqlalchemy import delete

from ai_ent.persistence.config import DatabaseConfigError, load_database_settings
from ai_ent.persistence.database import Database
from ai_ent.persistence.migrations import build_alembic_config
from ai_ent.persistence.models import (
    BootstrapCheckpoint,
    BootstrapRun,
    BootstrapStateAuthority,
    Checkpoint,
    Execution,
    Project,
    Task,
    TaskDependency,
    TaskLease,
)
from ai_ent.persistence.repositories import (
    BootstrapRunRepository,
    ProjectRepository,
    TaskRepository,
)


def integration_database() -> Database:
    try:
        config = build_alembic_config(Path("alembic.ini"), Path(".env"))
        settings = load_database_settings(Path(".env"))
    except DatabaseConfigError as exc:
        raise unittest.SkipTest(f"bootstrap run integration blocked: {exc}") from exc
    command.upgrade(config, "head")
    return Database(settings)


def clean_tables(database: Database) -> None:
    with database.session() as session:
        session.execute(delete(BootstrapStateAuthority))
        session.execute(delete(BootstrapCheckpoint))
        session.execute(delete(BootstrapRun))
        session.execute(delete(TaskLease))
        session.execute(delete(Checkpoint))
        session.execute(delete(Execution))
        session.execute(delete(TaskDependency))
        session.execute(delete(Task))
        session.execute(delete(Project))


def seed_project_and_tasks(database: Database, suffix: str) -> tuple[str, str, str]:
    projects = ProjectRepository()
    tasks = TaskRepository()
    project_id = f"project-{suffix}"
    task_a = f"TASK-A-{suffix}"
    task_b = f"TASK-B-{suffix}"
    with database.session() as session:
        projects.create(session, project_id=project_id, name=f"Project {suffix}")
        tasks.create(session, task_id=task_a, project_id=project_id, title="A")
        tasks.create(session, task_id=task_b, project_id=project_id, title="B")
    return project_id, task_a, task_b


def test_postgres_bootstrap_run_lifecycle_and_checkpoints() -> None:
    database = integration_database()
    clean_tables(database)
    repository = BootstrapRunRepository()
    suffix = uuid.uuid4().hex[:8]
    project_id, task_a, task_b = seed_project_and_tasks(database, suffix)
    run_id = f"run-{suffix}"

    try:
        with database.session() as session:
            repository.create_run(
                session,
                run_id=run_id,
                project_id=project_id,
                manifest_ref="manifest/bootstrap",
                baseline_commit="f43f6e2",
            )
            repository.update_run_state(
                session,
                run_id,
                status="running",
                current_stage="B04",
                current_task_id=task_a,
            )
            repository.update_run_state(
                session,
                run_id,
                last_completed_task_id=task_a,
                last_verified_commit="abc123",
            )
            repository.append_checkpoint(
                session,
                checkpoint_id=f"checkpoint-a-{suffix}",
                run_id=run_id,
                checkpoint_kind="task_completed",
                task_id=task_a,
                verified_commit="abc123",
                state=f'{{"last_completed":"{task_a}"}}',
            )
            repository.update_run_state(session, run_id, status="running", current_task_id=task_b)

        with database.session() as session:
            reloaded = repository.require_run(session, run_id)
            latest = repository.latest_checkpoint(session, run_id)
            assert reloaded.status == "running"
            assert reloaded.current_task_id == task_b
            assert reloaded.last_completed_task_id == task_a
            assert reloaded.last_verified_commit == "abc123"
            assert latest is not None
            assert latest.task_id == task_a
            assert latest.verified_commit == "abc123"
    finally:
        clean_tables(database)
        database.dispose()


def test_postgres_bootstrap_run_rollback_and_idempotency() -> None:
    database = integration_database()
    clean_tables(database)
    repository = BootstrapRunRepository()
    suffix = uuid.uuid4().hex[:8]
    project_id, task_a, _ = seed_project_and_tasks(database, suffix)
    run_id = f"run-{suffix}"

    try:
        with database.session() as session:
            first = repository.create_run(
                session,
                run_id=run_id,
                project_id=project_id,
                manifest_ref="manifest/bootstrap",
            )
            second = repository.create_run(
                session,
                run_id=run_id,
                project_id=project_id,
                manifest_ref="manifest/bootstrap",
            )
            assert first is second
            checkpoint = repository.append_checkpoint(
                session,
                checkpoint_id=f"checkpoint-{suffix}",
                run_id=run_id,
                checkpoint_kind="task_completed",
                task_id=task_a,
                state="{}",
            )
            repeated = repository.append_checkpoint(
                session,
                checkpoint_id=f"checkpoint-{suffix}",
                run_id=run_id,
                checkpoint_kind="task_completed",
                task_id=task_a,
                state="{}",
            )
            assert checkpoint is repeated

        try:
            with database.session() as session:
                repository.create_run(
                    session,
                    run_id=f"rollback-{suffix}",
                    project_id=project_id,
                    manifest_ref="manifest/bootstrap",
                )
                raise RuntimeError("simulate interruption")
        except RuntimeError:
            pass

        with database.session() as session:
            assert repository.get_run(session, f"rollback-{suffix}") is None
            assert len(repository.list_checkpoints(session, run_id)) == 1
    finally:
        clean_tables(database)
        database.dispose()
