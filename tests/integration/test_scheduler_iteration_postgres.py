from __future__ import annotations

import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from alembic import command
from sqlalchemy import select

from ai_ent.bootstrap.models import BootstrapTask
from ai_ent.persistence.config import DatabaseConfigError, load_database_settings
from ai_ent.persistence.database import Database
from ai_ent.persistence.migrations import build_alembic_config
from ai_ent.persistence.models import BootstrapStateAuthority, Execution, Project, Task, TaskLease
from ai_ent.persistence.repositories import ProjectRepository, TaskRepository
from ai_ent.scheduler.iteration import ExecutionPackageFactory, SchedulerIterationService
from tests.integration.helpers import clean_test_tables, make_test_project_id, make_test_suffix


def integration_database() -> Database:
    try:
        config = build_alembic_config(Path("alembic.ini"), Path(".env"))
        settings = load_database_settings(Path(".env"))
    except DatabaseConfigError as exc:
        raise unittest.SkipTest(f"scheduler iteration integration blocked: {exc}") from exc
    command.upgrade(config, "head")
    return Database(settings)


def manifest_task(task_id: str) -> BootstrapTask:
    return BootstrapTask(
        id=task_id,
        stage="T",
        title=f"Task {task_id}",
        executor="fake",
        objective=f"Execute {task_id}",
        allowed_paths=("tests/fixtures/**",),
        outputs=("tests/fixtures/result.txt",),
    )


def test_postgres_scheduler_run_once_claims_one_ready_task_and_returns_package() -> None:
    database = integration_database()
    clean_test_tables(database)
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id = make_test_project_id(suffix)
    task = manifest_task(f"TEST-TASK-{suffix}")
    projects = ProjectRepository()
    tasks = TaskRepository()
    service = SchedulerIterationService(
        package_factory=ExecutionPackageFactory(
            manifest_tasks={task.id: task},
            timeout_seconds=30,
        ),
        owner_id="worker-1",
        lease_duration=timedelta(minutes=5),
    )

    try:
        with database.session() as session:
            projects.create(session, project_id=project_id, name=f"Project {suffix}")
            tasks.create(session, task_id=task.id, project_id=project_id, title=task.title)

        with database.session() as session:
            result = service.run_once(session, project_id=project_id)
            assert result.status == "PACKAGE_READY"
            assert result.package is not None
            assert result.execution is not None
            assert result.lease is not None
            assert result.package.task_id == task.id
            assert result.package.execution_id == result.execution.id

        with database.session() as session:
            persisted_task = session.get(Task, task.id)
            executions = session.scalars(select(Execution).where(Execution.task_id == task.id)).all()
            leases = session.scalars(select(TaskLease).where(TaskLease.task_id == task.id)).all()
            assert persisted_task is not None
            assert persisted_task.status == "running"
            assert len(executions) == 1
            assert len(leases) == 1
            assert leases[0].status == "active"
            assert executions[0].status == "running"
    finally:
        clean_test_tables(database)
        database.dispose()


def test_integration_cleanup_preserves_non_test_authority_rows() -> None:
    database = integration_database()
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    protected_project_id = f"protected-project-{suffix}"
    protected_run_id = f"protected-run-{suffix}"
    protected_authority_id = f"protected-authority-{suffix}"

    try:
        with database.session() as session:
            session.add(Project(id=protected_project_id, name=f"Protected {suffix}"))
            session.add(
                Task(
                    id=f"PROTECTED-TASK-{suffix}",
                    project_id=protected_project_id,
                    title="Protected task",
                )
            )
            from ai_ent.persistence.models import BootstrapRun

            session.add(
                BootstrapRun(
                    id=protected_run_id,
                    project_id=protected_project_id,
                    status="running",
                    manifest_ref="manifest/bootstrap",
                )
            )
            session.add(
                BootstrapStateAuthority(
                    id=protected_authority_id,
                    project_id=protected_project_id,
                    run_id=protected_run_id,
                    backend="postgresql",
                    status="active",
                )
            )

        clean_test_tables(database)

        with database.session() as session:
            assert session.get(BootstrapStateAuthority, protected_authority_id) is not None
    finally:
        with database.session() as session:
            authority = session.get(BootstrapStateAuthority, protected_authority_id)
            if authority is not None:
                session.delete(authority)
            from ai_ent.persistence.models import BootstrapRun

            run = session.get(BootstrapRun, protected_run_id)
            if run is not None:
                session.delete(run)
            task = session.get(Task, f"PROTECTED-TASK-{suffix}")
            if task is not None:
                session.delete(task)
            project = session.get(Project, protected_project_id)
            if project is not None:
                session.delete(project)
        database.dispose()
