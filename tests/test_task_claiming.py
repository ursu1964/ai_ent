from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.persistence.models import Base, Execution, Project, Task, TaskLease
from ai_ent.persistence.repositories import DuplicateLeaseError, LeaseRepository
from ai_ent.scheduler.claiming import TaskClaimingService


def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def seed_task(
    session: Session,
    *,
    task_id: str = "TASK-1",
    status: str = "pending",
    schedulable: bool = True,
    execution_class: str = "implementation",
) -> Task:
    project = Project(id=f"project-{task_id}", name=f"Project {task_id}")
    task = Task(
        id=task_id,
        project_id=project.id,
        title=f"Task {task_id}",
        status=status,  # type: ignore[arg-type]
        schedulable=schedulable,
        execution_class=execution_class,  # type: ignore[arg-type]
    )
    session.add_all([project, task])
    session.flush()
    return task


def test_eligible_task_claim_creates_execution_and_lease() -> None:
    factory = session_factory()
    service = TaskClaimingService()
    with factory() as session:
        seed_task(session)
        result = service.claim_task(
            session,
            task_id="TASK-1",
            owner_id="worker-1",
            lease_duration=timedelta(minutes=5),
        )

        assert result.status == "CLAIMED"
        assert result.claimed is True
        assert result.execution is not None
        assert result.execution.attempt == 1
        assert result.execution.status == "running"
        assert result.lease is not None
        assert result.lease.execution_id == result.execution.id
        assert service.has_valid_lease(session, task_id="TASK-1", owner_id="worker-1")
        assert session.get(Task, "TASK-1").status == "running"  # type: ignore[union-attr]


def test_missing_and_non_claimable_tasks_return_deterministic_statuses() -> None:
    factory = session_factory()
    service = TaskClaimingService()
    with factory() as session:
        blocked = seed_task(session, task_id="TASK-BLOCKED", status="blocked")
        simulation = seed_task(session, task_id="TASK-SIM", execution_class="simulation")
        unscheduled = seed_task(session, task_id="TASK-UNSCHEDULED", schedulable=False)

        assert service.claim_task(
            session,
            task_id="missing",
            owner_id="worker",
            lease_duration=timedelta(minutes=5),
        ).status == "NOT_FOUND"
        assert service.claim_task(
            session,
            task_id=blocked.id,
            owner_id="worker",
            lease_duration=timedelta(minutes=5),
        ).status == "NOT_CLAIMABLE"
        assert service.claim_task(
            session,
            task_id=simulation.id,
            owner_id="worker",
            lease_duration=timedelta(minutes=5),
        ).status == "NOT_CLAIMABLE"
        assert service.claim_task(
            session,
            task_id=unscheduled.id,
            owner_id="worker",
            lease_duration=timedelta(minutes=5),
        ).status == "NOT_CLAIMABLE"


def test_active_lease_blocks_second_claimant() -> None:
    factory = session_factory()
    service = TaskClaimingService()
    with factory() as session:
        seed_task(session)
        first = service.claim_task(
            session,
            task_id="TASK-1",
            owner_id="worker-1",
            lease_duration=timedelta(minutes=5),
        )
        second = service.claim_task(
            session,
            task_id="TASK-1",
            owner_id="worker-2",
            lease_duration=timedelta(minutes=5),
        )

        assert first.status == "CLAIMED"
        assert second.status == "ALREADY_LEASED"


def test_lease_renewal_release_completion_and_ownership_validation() -> None:
    factory = session_factory()
    service = TaskClaimingService()
    with factory() as session:
        seed_task(session)
        claim = service.claim_task(
            session,
            task_id="TASK-1",
            owner_id="worker-1",
            lease_duration=timedelta(minutes=5),
        )
        assert claim.lease is not None
        lease_id = claim.lease.id

        assert service.renew(
            session,
            lease_id=lease_id,
            owner_id="wrong-worker",
            lease_duration=timedelta(minutes=10),
        ).status == "WRONG_OWNER"
        renewed = service.renew(
            session,
            lease_id=lease_id,
            owner_id="worker-1",
            lease_duration=timedelta(minutes=10),
        )
        assert renewed.status == "UPDATED"
        assert service.has_valid_lease(session, task_id="TASK-1", owner_id="worker-1")

        released = service.release(session, lease_id=lease_id, owner_id="worker-1")
        assert released.status == "UPDATED"
        assert not service.has_valid_lease(session, task_id="TASK-1", owner_id="worker-1")
        assert service.renew(
            session,
            lease_id=lease_id,
            owner_id="worker-1",
            lease_duration=timedelta(minutes=10),
        ).status == "NOT_ACTIVE"


def test_expired_lease_is_not_valid_ownership() -> None:
    factory = session_factory()
    service = TaskClaimingService()
    with factory() as session:
        seed_task(session)
        claim = service.claim_task(
            session,
            task_id="TASK-1",
            owner_id="worker-1",
            lease_duration=timedelta(minutes=5),
        )
        assert claim.lease is not None
        claim.lease.acquired_at = datetime(2000, 1, 1, tzinfo=UTC)
        claim.lease.renewed_at = datetime(2000, 1, 1, tzinfo=UTC)
        claim.lease.expires_at = datetime(2000, 1, 2, tzinfo=UTC)
        session.flush()

        assert not service.has_valid_lease(session, task_id="TASK-1", owner_id="worker-1")
        assert service.renew(
            session,
            lease_id=claim.lease.id,
            owner_id="worker-1",
            lease_duration=timedelta(minutes=10),
        ).status == "EXPIRED"


def test_claim_rollback_leaves_no_partial_ownership() -> None:
    factory = session_factory()
    service = TaskClaimingService()
    with factory() as session:
        seed_task(session)
        result = service.claim_task(
            session,
            task_id="TASK-1",
            owner_id="worker-1",
            lease_duration=timedelta(minutes=5),
        )
        assert result.status == "CLAIMED"
        session.rollback()

        assert session.scalars(select(Execution)).all() == []
        assert session.scalars(select(TaskLease)).all() == []
        assert session.get(Task, "TASK-1") is None


def test_execution_attempt_unique_per_task() -> None:
    factory = session_factory()
    with factory() as session:
        task = seed_task(session)
        first = Execution(id="execution-1", task_id=task.id, executor_type="fake", attempt=1)
        duplicate = Execution(id="execution-2", task_id=task.id, executor_type="fake", attempt=1)
        session.add_all([first, duplicate])
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
        else:
            raise AssertionError("duplicate execution attempt was not rejected")


def test_lease_repository_translates_invalid_foreign_key() -> None:
    factory = session_factory()
    repository = LeaseRepository()
    with factory() as session:
        now = datetime.now(UTC)
        try:
            repository.create(
                session,
                lease_id="lease-1",
                task_id="missing",
                execution_id="missing",
                owner_id="worker-1",
                acquired_at=now,
                expires_at=now + timedelta(minutes=5),
            )
        except DuplicateLeaseError as exc:
            assert "password" not in str(exc).lower()
        else:
            raise AssertionError("invalid lease foreign keys were not rejected")
