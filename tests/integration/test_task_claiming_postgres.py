from __future__ import annotations

import threading
import time
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from alembic import command
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from ai_ent.persistence.config import DatabaseConfigError, load_database_settings
from ai_ent.persistence.database import Database
from ai_ent.persistence.migrations import build_alembic_config
from ai_ent.persistence.models import (
    BootstrapCheckpoint,
    BootstrapRun,
    Checkpoint,
    Execution,
    Project,
    Task,
    TaskDependency,
    TaskLease,
)
from ai_ent.persistence.repositories import ProjectRepository, TaskRepository
from ai_ent.scheduler.claiming import TaskClaimingService


def integration_database() -> Database:
    try:
        config = build_alembic_config(Path("alembic.ini"), Path(".env"))
        settings = load_database_settings(Path(".env"))
    except DatabaseConfigError as exc:
        raise unittest.SkipTest(f"task claiming integration blocked: {exc}") from exc
    command.upgrade(config, "head")
    return Database(settings)


def clean_tables(database: Database) -> None:
    with database.session() as session:
        session.execute(delete(BootstrapCheckpoint))
        session.execute(delete(BootstrapRun))
        session.execute(delete(TaskLease))
        session.execute(delete(Checkpoint))
        session.execute(delete(Execution))
        session.execute(delete(TaskDependency))
        session.execute(delete(Task))
        session.execute(delete(Project))


def create_task(database: Database, suffix: str, *, task_id: str | None = None) -> str:
    projects = ProjectRepository()
    tasks = TaskRepository()
    selected_task_id = task_id or f"TASK-{suffix}"
    with database.session() as session:
        project_id = f"project-{suffix}"
        projects.create(session, project_id=project_id, name=f"Project {suffix}")
        tasks.create(session, task_id=selected_task_id, project_id=project_id, title="Claimable task")
    return selected_task_id


def test_postgres_claim_creates_execution_and_lease_atomically() -> None:
    database = integration_database()
    clean_tables(database)
    service = TaskClaimingService()
    suffix = uuid.uuid4().hex[:8]
    task_id = create_task(database, suffix)

    try:
        with database.session() as session:
            result = service.claim_task(
                session,
                task_id=task_id,
                owner_id="worker-1",
                lease_duration=timedelta(minutes=5),
            )
            assert result.status == "CLAIMED"
            assert result.execution is not None
            assert result.lease is not None
            assert result.lease.execution_id == result.execution.id

        with database.session() as session:
            executions = session.scalars(select(Execution).where(Execution.task_id == task_id)).all()
            leases = session.scalars(select(TaskLease).where(TaskLease.task_id == task_id)).all()
            assert len(executions) == 1
            assert len(leases) == 1
            assert executions[0].attempt == 1
            assert leases[0].status == "active"
    finally:
        clean_tables(database)
        database.dispose()


def test_postgres_expired_lease_blocks_renewal_but_not_ownership_validation() -> None:
    database = integration_database()
    clean_tables(database)
    service = TaskClaimingService()
    suffix = uuid.uuid4().hex[:8]
    task_id = create_task(database, suffix)

    try:
        with database.session() as session:
            result = service.claim_task(
                session,
                task_id=task_id,
                owner_id="worker-1",
                lease_duration=timedelta(seconds=1),
            )
            assert result.lease is not None
            lease_id = result.lease.id
            result.lease.acquired_at = result.lease.acquired_at - timedelta(minutes=10)
            result.lease.renewed_at = result.lease.acquired_at
            result.lease.expires_at = result.lease.acquired_at + timedelta(seconds=1)

        with database.session() as session:
            assert not service.has_valid_lease(session, task_id=task_id, owner_id="worker-1")
            assert service.renew(
                session,
                lease_id=lease_id,
                owner_id="worker-1",
                lease_duration=timedelta(minutes=5),
            ).status == "EXPIRED"
    finally:
        clean_tables(database)
        database.dispose()


def test_postgres_concurrent_claim_has_exactly_one_winner() -> None:
    database = integration_database()
    clean_tables(database)
    suffix = uuid.uuid4().hex[:8]
    task_id = create_task(database, suffix)
    settings = database.settings
    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []
    results_lock = threading.Lock()

    def worker(owner_id: str) -> None:
        local_database = Database(settings)
        service = TaskClaimingService()
        try:
            with local_database.session() as session:
                barrier.wait(timeout=10)
                result = service.claim_task(
                    session,
                    task_id=task_id,
                    owner_id=owner_id,
                    lease_duration=timedelta(minutes=5),
                )
                with results_lock:
                    results.append(result.status)
                if result.status == "CLAIMED":
                    time.sleep(0.2)
        except (RuntimeError, SQLAlchemyError, threading.BrokenBarrierError) as exc:
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
            active_leases = session.scalars(
                select(TaskLease).where(TaskLease.task_id == task_id, TaskLease.status == "active")
            ).all()
            assert len(active_leases) == 1
    finally:
        clean_tables(database)
        database.dispose()
