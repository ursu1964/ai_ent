from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.persistence.models import Base, Execution, Task, TaskLease
from ai_ent.persistence.repositories import ProjectRepository, TaskRepository
from ai_ent.scheduler.claiming import TaskClaimingService
from ai_ent.scheduler.readiness import TaskReadinessService


def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def seed_project(session: Session, project_id: str = "project-1") -> str:
    ProjectRepository().create(session, project_id=project_id, name=f"Project {project_id}")
    return project_id


def seed_task(
    session: Session,
    task_id: str,
    *,
    project_id: str = "project-1",
    status: str = "pending",
    schedulable: bool = True,
    execution_class: str = "implementation",
) -> Task:
    return TaskRepository().create(
        session,
        task_id=task_id,
        project_id=project_id,
        title=f"Task {task_id}",
        status=status,  # type: ignore[arg-type]
        schedulable=schedulable,
        execution_class=execution_class,  # type: ignore[arg-type]
    )


def add_dependency(session: Session, task_id: str, depends_on_task_id: str) -> None:
    TaskRepository().add_dependency(session, task_id=task_id, depends_on_task_id=depends_on_task_id)


def test_root_schedulable_task_is_ready() -> None:
    factory = session_factory()
    service = TaskReadinessService()
    with factory() as session:
        seed_project(session)
        seed_task(session, "TASK-A")

        decision = service.evaluate_task(session, "TASK-A")

        assert decision.ready is True
        assert decision.status == "READY"
        assert [task.id for task in service.list_ready_tasks(session, project_id="project-1")] == ["TASK-A"]


def test_dependency_complete_makes_dependent_ready() -> None:
    factory = session_factory()
    service = TaskReadinessService()
    with factory() as session:
        seed_project(session)
        seed_task(session, "TASK-A", status="passed")
        seed_task(session, "TASK-B")
        add_dependency(session, "TASK-B", "TASK-A")

        decision = service.evaluate_task(session, "TASK-B")

        assert decision.status == "READY"
        assert decision.dependencies == ("TASK-A",)


def test_pending_and_running_dependencies_wait() -> None:
    factory = session_factory()
    service = TaskReadinessService()
    with factory() as session:
        seed_project(session)
        seed_task(session, "TASK-A", status="pending")
        seed_task(session, "TASK-B", status="running")
        seed_task(session, "TASK-C")
        seed_task(session, "TASK-D")
        add_dependency(session, "TASK-C", "TASK-A")
        add_dependency(session, "TASK-D", "TASK-B")

        pending = service.evaluate_task(session, "TASK-C")
        running = service.evaluate_task(session, "TASK-D")

        assert pending.status == "WAITING_DEPENDENCIES"
        assert pending.waiting_dependencies == ("TASK-A",)
        assert running.status == "WAITING_DEPENDENCIES"
        assert running.waiting_dependencies == ("TASK-B",)


def test_failed_and_blocked_dependencies_block() -> None:
    factory = session_factory()
    service = TaskReadinessService()
    with factory() as session:
        seed_project(session)
        seed_task(session, "TASK-A", status="failed")
        seed_task(session, "TASK-B", status="blocked")
        seed_task(session, "TASK-C")
        seed_task(session, "TASK-D")
        add_dependency(session, "TASK-C", "TASK-A")
        add_dependency(session, "TASK-D", "TASK-B")

        failed = service.evaluate_task(session, "TASK-C")
        blocked = service.evaluate_task(session, "TASK-D")

        assert failed.status == "BLOCKED_DEPENDENCY"
        assert failed.blocking_dependencies == ("TASK-A",)
        assert blocked.status == "BLOCKED_DEPENDENCY"
        assert blocked.blocking_dependencies == ("TASK-B",)


def test_multiple_dependencies_require_all_complete() -> None:
    factory = session_factory()
    service = TaskReadinessService()
    with factory() as session:
        seed_project(session)
        seed_task(session, "TASK-A", status="passed")
        seed_task(session, "TASK-B", status="pending")
        seed_task(session, "TASK-C")
        add_dependency(session, "TASK-C", "TASK-A")
        add_dependency(session, "TASK-C", "TASK-B")

        assert service.evaluate_task(session, "TASK-C").status == "WAITING_DEPENDENCIES"
        session.get(Task, "TASK-B").status = "passed"  # type: ignore[union-attr]
        session.flush()

        assert service.evaluate_task(session, "TASK-C").status == "READY"


def test_non_schedulable_simulation_terminal_and_running_tasks_excluded() -> None:
    factory = session_factory()
    service = TaskReadinessService()
    with factory() as session:
        seed_project(session)
        seed_task(session, "TASK-NO", schedulable=False)
        seed_task(session, "TASK-SIM", execution_class="simulation")
        seed_task(session, "TASK-DONE", status="passed")
        seed_task(session, "TASK-RUN", status="running")

        assert service.evaluate_task(session, "TASK-NO").status == "NOT_SCHEDULABLE"
        assert service.evaluate_task(session, "TASK-SIM").status == "NOT_SCHEDULABLE"
        assert service.evaluate_task(session, "TASK-DONE").status == "TERMINAL"
        assert service.evaluate_task(session, "TASK-RUN").status == "RUNNING"
        assert service.list_ready_tasks(session, project_id="project-1") == []


def test_active_lease_excludes_task_but_expired_lease_does_not() -> None:
    factory = session_factory()
    service = TaskReadinessService()
    claiming = TaskClaimingService()
    with factory() as session:
        seed_project(session)
        seed_task(session, "TASK-A")
        active = claiming.claim_task(
            session,
            task_id="TASK-A",
            owner_id="worker-1",
            lease_duration=timedelta(minutes=5),
        )
        assert active.status == "CLAIMED"
        session.get(Task, "TASK-A").status = "pending"  # type: ignore[union-attr]
        assert service.evaluate_task(session, "TASK-A").status == "ALREADY_LEASED"

        active.lease.status = "expired"  # type: ignore[union-attr]
        session.flush()
        assert service.evaluate_task(session, "TASK-A").status == "READY"

        now = datetime(2000, 1, 1, tzinfo=UTC)
        execution = Execution(id="execution-expired", task_id="TASK-A", executor_type="fake", attempt=2)
        expired = TaskLease(
            id="lease-expired",
            task_id="TASK-A",
            execution_id=execution.id,
            owner_id="worker-2",
            status="active",
            acquired_at=now,
            renewed_at=now,
            expires_at=now + timedelta(minutes=1),
        )
        session.add_all([execution, expired])
        session.flush()
        assert service.evaluate_task(session, "TASK-A").status == "READY"


def test_missing_task_and_cycle_are_reported() -> None:
    factory = session_factory()
    service = TaskReadinessService()
    with factory() as session:
        seed_project(session)
        seed_task(session, "TASK-A")
        seed_task(session, "TASK-B")
        add_dependency(session, "TASK-A", "TASK-B")
        add_dependency(session, "TASK-B", "TASK-A")

        assert service.evaluate_task(session, "missing").status == "NOT_FOUND"
        assert service.evaluate_task(session, "TASK-A").status == "INVALID_GRAPH_STATE"
        assert service.evaluate_task(session, "TASK-B").status == "INVALID_GRAPH_STATE"


def test_ready_ordering_and_project_scope_are_deterministic() -> None:
    factory = session_factory()
    service = TaskReadinessService()
    with factory() as session:
        seed_project(session, "project-1")
        seed_project(session, "project-2")
        seed_task(session, "TASK-A", project_id="project-1")
        seed_task(session, "TASK-B", project_id="project-1")
        seed_task(session, "TASK-C", project_id="project-2")

        assert [task.id for task in service.list_ready_tasks(session, project_id="project-1")] == [
            "TASK-A",
            "TASK-B",
        ]
        assert [task.id for task in service.list_ready_tasks(session, project_id="project-1", limit=1)] == ["TASK-A"]
        assert [task.id for task in service.list_ready_tasks(session, project_id="project-2")] == ["TASK-C"]
