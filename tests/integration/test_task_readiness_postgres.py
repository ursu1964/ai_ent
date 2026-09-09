from __future__ import annotations

import threading
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from alembic import command
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ai_ent.persistence.config import DatabaseConfigError, load_database_settings
from ai_ent.persistence.database import Database
from ai_ent.persistence.migrations import build_alembic_config
from ai_ent.persistence.models import (
    TaskLease,
)
from ai_ent.persistence.repositories import ProjectRepository, TaskRepository
from ai_ent.scheduler.claiming import TaskClaimingService
from ai_ent.scheduler.readiness import TaskReadinessService
from tests.integration.helpers import clean_test_tables, make_test_suffix


def integration_database() -> Database:
    try:
        config = build_alembic_config(Path("alembic.ini"), Path(".env"))
        settings = load_database_settings(Path(".env"))
    except DatabaseConfigError as exc:
        raise unittest.SkipTest(f"task readiness integration blocked: {exc}") from exc
    command.upgrade(config, "head")
    return Database(settings)


def clean_tables(database: Database) -> None:
    clean_test_tables(database)


def seed_project_tasks(database: Database, suffix: str) -> tuple[str, str, str, str]:
    projects = ProjectRepository()
    tasks = TaskRepository()
    project_id = f"project-{suffix}"
    task_a = f"TASK-A-{suffix}"
    task_b = f"TASK-B-{suffix}"
    task_c = f"TASK-C-{suffix}"
    with database.session() as session:
        projects.create(session, project_id=project_id, name=f"Project {suffix}")
        tasks.create(session, task_id=task_a, project_id=project_id, title="A")
        tasks.create(session, task_id=task_b, project_id=project_id, title="B")
        tasks.create(session, task_id=task_c, project_id=project_id, title="C")
        tasks.add_dependency(session, task_id=task_b, depends_on_task_id=task_a)
        tasks.add_dependency(session, task_id=task_c, depends_on_task_id=task_b)
    return project_id, task_a, task_b, task_c


def test_postgres_chain_readiness_evolves_as_dependencies_complete() -> None:
    database = integration_database()
    clean_tables(database)
    service = TaskReadinessService()
    tasks = TaskRepository()
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id, task_a, task_b, task_c = seed_project_tasks(database, suffix)

    try:
        with database.session() as session:
            assert [task.id for task in service.list_ready_tasks(session, project_id=project_id)] == [task_a]
            assert service.evaluate_task(session, task_b).status == "WAITING_DEPENDENCIES"
            assert service.evaluate_task(session, task_c).status == "WAITING_DEPENDENCIES"
            tasks.update_status(session, task_a, "passed")

        with database.session() as session:
            assert [task.id for task in service.list_ready_tasks(session, project_id=project_id)] == [task_b]
            tasks.update_status(session, task_b, "passed")

        with database.session() as session:
            assert [task.id for task in service.list_ready_tasks(session, project_id=project_id)] == [task_c]
    finally:
        clean_tables(database)
        database.dispose()


def test_postgres_blocked_dependency_explains_not_ready() -> None:
    database = integration_database()
    clean_tables(database)
    service = TaskReadinessService()
    tasks = TaskRepository()
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id, task_a, task_b, _ = seed_project_tasks(database, suffix)

    try:
        with database.session() as session:
            tasks.update_status(session, task_a, "failed")
            decision = service.explain_not_ready(session, task_b)
            assert decision.status == "BLOCKED_DEPENDENCY"
            assert decision.blocking_dependencies == (task_a,)
            assert service.list_ready_tasks(session, project_id=project_id) == []
    finally:
        clean_tables(database)
        database.dispose()


def test_postgres_ready_visibility_then_concurrent_claim_has_one_winner() -> None:
    database = integration_database()
    clean_tables(database)
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id, task_a, _, _ = seed_project_tasks(database, suffix)
    settings = database.settings
    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []
    results_lock = threading.Lock()

    def worker(owner_id: str) -> None:
        local_database = Database(settings)
        readiness = TaskReadinessService()
        claiming = TaskClaimingService()
        try:
            with local_database.session() as session:
                ready_ids = [task.id for task in readiness.list_ready_tasks(session, project_id=project_id)]
                assert task_a in ready_ids
                barrier.wait(timeout=10)
                result = claiming.claim_task(
                    session,
                    task_id=task_a,
                    owner_id=owner_id,
                    lease_duration=timedelta(minutes=5),
                )
                with results_lock:
                    results.append(result.status)
        except (AssertionError, RuntimeError, SQLAlchemyError, threading.BrokenBarrierError) as exc:
            with results_lock:
                errors.append(exc)
        finally:
            local_database.dispose()

    try:
        threads = [
            threading.Thread(target=worker, args=("worker-a",)),
            threading.Thread(target=worker, args=("worker-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert errors == []
        assert len(results) == 2
        assert results.count("CLAIMED") == 1

        with database.session() as session:
            assert service_count_active_leases(session, task_a) == 1
            assert TaskReadinessService().evaluate_task(session, task_a).status == "ALREADY_LEASED"
    finally:
        clean_tables(database)
        database.dispose()


def service_count_active_leases(session: Session, task_id: str) -> int:
    return len(
        session.scalars(select(TaskLease).where(TaskLease.task_id == task_id, TaskLease.status == "active")).all()
    )
