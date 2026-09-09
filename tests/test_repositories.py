from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.persistence.models import Base
from ai_ent.persistence.repositories import (
    DuplicateDependencyError,
    DuplicateProjectError,
    DuplicateTaskError,
    MissingProjectError,
    MissingTaskError,
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


def test_create_get_project() -> None:
    projects = ProjectRepository()
    factory = session_factory()
    with factory() as session:
        created = projects.create(session, project_id="project-1", name="Project One")
        session.commit()
        found = projects.get(session, "project-1")

    assert created.id == "project-1"
    assert found is not None
    assert found.name == "Project One"


def test_duplicate_project_rejection() -> None:
    projects = ProjectRepository()
    factory = session_factory()
    with factory() as session:
        projects.create(session, project_id="project-1", name="Project One")
        try:
            projects.create(session, project_id="project-1", name="Project Duplicate")
        except DuplicateProjectError:
            session.rollback()
        else:
            raise AssertionError("duplicate project was not rejected")


def test_missing_project_behavior_and_status_update() -> None:
    projects = ProjectRepository()
    factory = session_factory()
    with factory() as session:
        assert not projects.exists(session, "missing")
        try:
            projects.require(session, "missing")
        except MissingProjectError:
            pass
        else:
            raise AssertionError("missing project was not rejected")

        projects.create(session, project_id="project-1", name="Project One")
        updated = projects.update_status(session, "project-1", "archived")
        assert updated.status == "archived"
        assert [project.id for project in projects.list(session, status="archived")] == ["project-1"]


def test_create_get_task_and_filters() -> None:
    projects = ProjectRepository()
    tasks = TaskRepository()
    factory = session_factory()
    with factory() as session:
        projects.create(session, project_id="project-1", name="Project One")
        created = tasks.create(
            session,
            task_id="TASK-1",
            project_id="project-1",
            title="Task One",
            objective="Do work",
            execution_class="simulation",
            schedulable=False,
            fingerprint="fingerprint-1",
        )
        session.commit()

        assert created.id == "TASK-1"
        assert tasks.exists(session, "TASK-1")
        assert tasks.get(session, "TASK-1") is not None
        assert [task.id for task in tasks.list_by_project(session, "project-1")] == ["TASK-1"]
        assert [task.id for task in tasks.list_by_status(session, "pending")] == ["TASK-1"]
        assert tasks.get(session, "TASK-1").execution_class == "simulation"  # type: ignore[union-attr]
        assert tasks.get(session, "TASK-1").schedulable is False  # type: ignore[union-attr]


def test_duplicate_and_missing_task_behavior() -> None:
    projects = ProjectRepository()
    tasks = TaskRepository()
    factory = session_factory()
    with factory() as session:
        projects.create(session, project_id="project-1", name="Project One")
        tasks.create(session, task_id="TASK-1", project_id="project-1", title="Task One")
        try:
            tasks.create(session, task_id="TASK-1", project_id="project-1", title="Task Again")
        except DuplicateTaskError:
            session.rollback()
        else:
            raise AssertionError("duplicate task was not rejected")

        assert not tasks.exists(session, "missing")
        try:
            tasks.require(session, "missing")
        except MissingTaskError:
            pass
        else:
            raise AssertionError("missing task was not rejected")


def test_task_status_update() -> None:
    projects = ProjectRepository()
    tasks = TaskRepository()
    factory = session_factory()
    with factory() as session:
        projects.create(session, project_id="project-1", name="Project One")
        tasks.create(session, task_id="TASK-1", project_id="project-1", title="Task One")
        updated = tasks.update_status(session, "TASK-1", "blocked")

    assert updated.status == "blocked"


def test_dependencies_and_dependents() -> None:
    projects = ProjectRepository()
    tasks = TaskRepository()
    factory = session_factory()
    with factory() as session:
        projects.create(session, project_id="project-1", name="Project One")
        tasks.create(session, task_id="TASK-1", project_id="project-1", title="Task One")
        tasks.create(session, task_id="TASK-2", project_id="project-1", title="Task Two")
        tasks.add_dependency(session, task_id="TASK-2", depends_on_task_id="TASK-1")
        session.commit()

        assert tasks.list_dependencies(session, "TASK-2") == ["TASK-1"]
        assert tasks.list_dependents(session, "TASK-1") == ["TASK-2"]
        assert tasks.remove_dependency(session, task_id="TASK-2", depends_on_task_id="TASK-1")
        assert not tasks.remove_dependency(session, task_id="TASK-2", depends_on_task_id="TASK-1")


def test_duplicate_dependency_rejection() -> None:
    projects = ProjectRepository()
    tasks = TaskRepository()
    factory = session_factory()
    with factory() as session:
        projects.create(session, project_id="project-1", name="Project One")
        tasks.create(session, task_id="TASK-1", project_id="project-1", title="Task One")
        tasks.create(session, task_id="TASK-2", project_id="project-1", title="Task Two")
        tasks.add_dependency(session, task_id="TASK-2", depends_on_task_id="TASK-1")
        try:
            tasks.add_dependency(session, task_id="TASK-2", depends_on_task_id="TASK-1")
        except DuplicateDependencyError:
            session.rollback()
        else:
            raise AssertionError("duplicate dependency was not rejected")


def test_self_dependency_rejection() -> None:
    projects = ProjectRepository()
    tasks = TaskRepository()
    factory = session_factory()
    with factory() as session:
        projects.create(session, project_id="project-1", name="Project One")
        tasks.create(session, task_id="TASK-1", project_id="project-1", title="Task One")
        try:
            tasks.add_dependency(session, task_id="TASK-1", depends_on_task_id="TASK-1")
        except DuplicateDependencyError:
            session.rollback()
        else:
            raise AssertionError("self dependency was not rejected")


def test_foreign_key_rejection() -> None:
    tasks = TaskRepository()
    factory = session_factory()
    with factory() as session:
        try:
            tasks.create(session, task_id="TASK-1", project_id="missing", title="Task One")
        except (DuplicateTaskError, IntegrityError):
            session.rollback()
        else:
            raise AssertionError("foreign-key violation was not rejected")


def test_multiple_repository_operations_in_one_transaction() -> None:
    projects = ProjectRepository()
    tasks = TaskRepository()
    factory = session_factory()
    with factory() as session:
        projects.create(session, project_id="project-1", name="Project One")
        tasks.create(session, task_id="TASK-1", project_id="project-1", title="Task One")
        tasks.create(session, task_id="TASK-2", project_id="project-1", title="Task Two")
        tasks.add_dependency(session, task_id="TASK-2", depends_on_task_id="TASK-1")
        session.commit()

        assert projects.exists(session, "project-1")
        assert tasks.list_dependencies(session, "TASK-2") == ["TASK-1"]


def test_transaction_rollback() -> None:
    projects = ProjectRepository()
    tasks = TaskRepository()
    factory = session_factory()
    with factory() as session:
        try:
            projects.create(session, project_id="project-1", name="Project One")
            tasks.create(session, task_id="TASK-1", project_id="project-1", title="Task One")
            raise RuntimeError("stop")
        except RuntimeError:
            session.rollback()

        assert not projects.exists(session, "project-1")
        assert not tasks.exists(session, "TASK-1")
