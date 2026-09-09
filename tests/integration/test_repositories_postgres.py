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
    DuplicateDependencyError,
    DuplicateProjectError,
    DuplicateTaskError,
    ProjectRepository,
    TaskRepository,
)


def integration_database() -> Database:
    try:
        config = build_alembic_config(Path("alembic.ini"), Path(".env"))
        settings = load_database_settings(Path(".env"))
    except DatabaseConfigError as exc:
        raise unittest.SkipTest(f"repository integration blocked: {exc}") from exc
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


def test_postgres_project_task_dependency_repositories() -> None:
    database = integration_database()
    clean_tables(database)
    projects = ProjectRepository()
    tasks = TaskRepository()
    suffix = uuid.uuid4().hex[:8]

    try:
        with database.session() as session:
            project_id = f"project-{suffix}"
            projects.create(session, project_id=project_id, name=f"Project {suffix}")
            tasks.create(session, task_id=f"TASK-A-{suffix}", project_id=project_id, title="A")
            tasks.create(session, task_id=f"TASK-B-{suffix}", project_id=project_id, title="B")
            tasks.add_dependency(
                session,
                task_id=f"TASK-B-{suffix}",
                depends_on_task_id=f"TASK-A-{suffix}",
            )

        with database.session() as session:
            assert projects.exists(session, project_id)
            assert [task.id for task in tasks.list_by_project(session, project_id)] == [
                f"TASK-A-{suffix}",
                f"TASK-B-{suffix}",
            ]
            assert tasks.list_dependencies(session, f"TASK-B-{suffix}") == [f"TASK-A-{suffix}"]
    finally:
        clean_tables(database)
        database.dispose()


def test_postgres_constraint_translation() -> None:
    database = integration_database()
    clean_tables(database)
    projects = ProjectRepository()
    tasks = TaskRepository()
    suffix = uuid.uuid4().hex[:8]

    try:
        with database.session() as session:
            project_id = f"project-{suffix}"
            projects.create(session, project_id=project_id, name=f"Project {suffix}")
            try:
                projects.create(session, project_id=project_id, name=f"Other {suffix}")
            except DuplicateProjectError:
                session.rollback()
            else:
                raise AssertionError("duplicate project was not rejected")

        with database.session() as session:
            project_id = f"project-{suffix}-2"
            projects.create(session, project_id=project_id, name=f"Project {suffix}-2")
            tasks.create(session, task_id=f"TASK-A-{suffix}", project_id=project_id, title="A")
            try:
                tasks.create(session, task_id=f"TASK-A-{suffix}", project_id=project_id, title="B")
            except DuplicateTaskError:
                session.rollback()
            else:
                raise AssertionError("duplicate task was not rejected")

        with database.session() as session:
            project_id = f"project-{suffix}-3"
            projects.create(session, project_id=project_id, name=f"Project {suffix}-3")
            tasks.create(session, task_id=f"TASK-A-{suffix}-3", project_id=project_id, title="A")
            try:
                tasks.add_dependency(
                    session,
                    task_id=f"TASK-A-{suffix}-3",
                    depends_on_task_id=f"TASK-A-{suffix}-3",
                )
            except DuplicateDependencyError:
                session.rollback()
            else:
                raise AssertionError("self dependency was not rejected")
    finally:
        clean_tables(database)
        database.dispose()
