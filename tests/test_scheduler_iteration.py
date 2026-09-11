from __future__ import annotations

import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.bootstrap.models import BootstrapTask
from ai_ent.persistence.models import (
    Base,
    Execution,
    RuntimePlanImport,
    RuntimeTaskPlanBinding,
    Task,
    TaskLease,
)
from ai_ent.persistence.repositories import ProjectRepository, TaskRepository
from ai_ent.scheduler.claiming import ClaimResult
from ai_ent.scheduler.iteration import (
    ExecutionPackageFactory,
    ExecutionTimeoutPolicy,
    SchedulerIterationService,
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


def manifest_task(
    task_id: str,
    *,
    title: str | None = None,
    allowed_paths: tuple[str, ...] = ("tests/fixtures/**",),
) -> BootstrapTask:
    return BootstrapTask(
        id=task_id,
        stage="T",
        title=title or f"Task {task_id}",
        executor="fake",
        objective=f"Execute {task_id}",
        allowed_paths=allowed_paths,
        outputs=("tests/fixtures/result.txt",),
    )


def seed_project(session: Session, project_id: str = "project-1") -> None:
    ProjectRepository().create(session, project_id=project_id, name=f"Project {project_id}")


def seed_task(
    session: Session,
    task_id: str,
    *,
    project_id: str = "project-1",
    title: str | None = None,
    status: str = "pending",
    schedulable: bool = True,
    execution_class: str = "implementation",
) -> Task:
    return TaskRepository().create(
        session,
        task_id=task_id,
        project_id=project_id,
        title=title or f"Task {task_id}",
        status=status,  # type: ignore[arg-type]
        schedulable=schedulable,
        execution_class=execution_class,  # type: ignore[arg-type]
    )


def service_for(*tasks: BootstrapTask) -> SchedulerIterationService:
    manifests = {task.id: task for task in tasks}
    return SchedulerIterationService(
        package_factory=ExecutionPackageFactory(
            repository_path=Path("/repo"),
            timeout_seconds=30,
            manifest_tasks=manifests,
        ),
        owner_id="worker-1",
        lease_duration=timedelta(minutes=5),
    )


def seed_runtime_plan_import(session: Session, project_id: str = "project-1") -> RuntimePlanImport:
    plan_import = RuntimePlanImport(
        id=f"import-{project_id}",
        project_id=project_id,
        plan_project_id=project_id,
        plan_id=f"PLAN-{project_id}",
        plan_version="1",
        status="imported",
        compiled_project_hash="0" * 64,
        capability_resolution_hash="1" * 64,
        trace_validation_hash="2" * 64,
        implementation_plan_hash="3" * 64,
        feasibility_hash="4" * 64,
        dry_run_hash="5" * 64,
        task_fingerprint_hash="6" * 64,
        dependency_graph_hash="7" * 64,
        importer_version="test",
        task_count=1,
        dependency_count=0,
        human_gate_count=0,
        effective_concurrency=1,
    )
    session.add(plan_import)
    session.flush()
    return plan_import


def seed_runtime_binding(
    session: Session,
    task: Task,
    plan_import: RuntimePlanImport,
    *,
    risk_level: str = "MEDIUM",
    model_profile: str = "STANDARD",
    verification_profile: str = "STANDARD_REGRESSION",
) -> RuntimeTaskPlanBinding:
    binding = RuntimeTaskPlanBinding(
        task_id=task.id,
        import_id=plan_import.id,
        plan_id=plan_import.plan_id,
        plan_version=plan_import.plan_version,
        fingerprint=f"fingerprint-{task.id}",
        risk_level=risk_level,
        agent_role="test-agent",
        model_profile=model_profile,
        executor="codex",
        verification_profile=verification_profile,
        feasibility_status="FEASIBLE",
        policy_decision="AUTO_ALLOWED",
        implements_json="{}",
        write_scope_json="{}",
        acceptance_json="{}",
    )
    session.add(binding)
    session.flush()
    return binding


def test_no_ready_tasks_returns_no_ready() -> None:
    factory = session_factory()
    service = service_for()
    with factory() as session:
        seed_project(session)
        seed_task(session, "TASK-BLOCKED", status="blocked")

        result = service.run_once(session, project_id="project-1")

        assert result.status == "NO_READY_TASK"
        assert session.scalars(select(Execution)).all() == []
        assert session.scalars(select(TaskLease)).all() == []


def test_one_ready_task_is_claimed_and_package_returned() -> None:
    factory = session_factory()
    task = manifest_task("TASK-A")
    service = service_for(task)
    with factory() as session:
        seed_project(session)
        seed_task(session, task.id, title=task.title)

        result = service.run_once(session, project_id="project-1")

        assert result.status == "PACKAGE_READY"
        assert result.execution is not None
        assert result.lease is not None
        assert result.package is not None
        assert result.package.task_id == task.id
        assert result.package.execution_id == result.execution.id
        assert result.package.allowed_paths == task.allowed_paths
        assert "tests/fixtures/result.txt" in result.package.instructions
        assert session.get(Task, task.id).status == "running"  # type: ignore[union-attr]


def test_timeout_policy_keeps_standard_tasks_at_900_seconds() -> None:
    policy = ExecutionTimeoutPolicy.for_codex_default(900)
    task = Task(id="TASK-A", project_id="project-1", title="Task A")

    assert policy.standard_timeout_seconds == 900
    assert policy.high_complexity_timeout_seconds == 1800
    assert policy.maximum_timeout_seconds == 1800
    assert policy.timeout_for_task(task) == 900


def test_timeout_policy_extends_only_qualifying_runtime_tasks() -> None:
    policy = ExecutionTimeoutPolicy.for_codex_default(900)
    standard = Task(id="TASK-STANDARD", project_id="project-1", title="Standard")
    high = Task(id="TASK-HIGH", project_id="project-1", title="High")
    security = Task(id="TASK-SECURITY", project_id="project-1", title="Security")
    high.runtime_plan_binding = RuntimeTaskPlanBinding(
        task_id=high.id,
        import_id="import-1",
        plan_id="PLAN",
        plan_version="1",
        fingerprint="fingerprint-high",
        risk_level="HIGH",
        agent_role="agent",
        model_profile="MDL-001:Codex executor model:high-risk-guarded",
        executor="codex",
        verification_profile="STANDARD_REGRESSION",
        feasibility_status="FEASIBLE",
        policy_decision="HUMAN_APPROVAL_REQUIRED",
        implements_json="{}",
        write_scope_json="{}",
        acceptance_json="{}",
    )
    security.runtime_plan_binding = RuntimeTaskPlanBinding(
        task_id=security.id,
        import_id="import-1",
        plan_id="PLAN",
        plan_version="1",
        fingerprint="fingerprint-security",
        risk_level="MEDIUM",
        agent_role="agent",
        model_profile="STANDARD",
        executor="codex",
        verification_profile="FULL_REGRESSION_SECURITY",
        feasibility_status="FEASIBLE",
        policy_decision="AUTO_ALLOWED",
        implements_json="{}",
        write_scope_json="{}",
        acceptance_json="{}",
    )

    assert policy.timeout_for_task(standard) == 900
    assert policy.timeout_for_task(high) == 1800
    assert policy.timeout_for_task(security) == 1800


def test_execution_package_factory_uses_profile_timeout_from_runtime_binding() -> None:
    factory = session_factory()
    task = manifest_task("PRD-TASK-003", title="Reusable external-project runtime importer")
    service = SchedulerIterationService(
        package_factory=ExecutionPackageFactory(
            repository_path=Path("/repo"),
            timeout_policy=ExecutionTimeoutPolicy.for_codex_default(900),
            manifest_tasks={task.id: task},
        ),
        owner_id="worker-1",
        lease_duration=timedelta(hours=1),
    )
    with factory() as session:
        seed_project(session)
        persisted = seed_task(session, task.id, title=task.title)
        plan_import = seed_runtime_plan_import(session)
        seed_runtime_binding(
            session,
            persisted,
            plan_import,
            risk_level="HIGH",
            model_profile="MDL-001:Codex executor model:high-risk-guarded",
            verification_profile="FULL_REGRESSION_SECURITY",
        )

        result = service.run_once(session, project_id="project-1")

        assert result.status == "PACKAGE_READY"
        assert result.package is not None
        assert result.package.timeout_seconds == 1800


def test_deterministic_selection_and_run_once_processes_one_task() -> None:
    factory = session_factory()
    first = manifest_task("TASK-A")
    second = manifest_task("TASK-B")
    service = service_for(first, second)
    with factory() as session:
        seed_project(session)
        seed_task(session, first.id, title=first.title)
        seed_task(session, second.id, title=second.title)

        result = service.run_once(session, project_id="project-1")

        assert result.status == "PACKAGE_READY"
        assert result.task is not None
        assert result.task.id == first.id
        assert len(session.scalars(select(Execution)).all()) == 1
        assert len(session.scalars(select(TaskLease).where(TaskLease.status == "active")).all()) == 1


def test_non_schedulable_dependency_blocked_and_leased_tasks_are_ignored() -> None:
    factory = session_factory()
    ready = manifest_task("TASK-READY")
    service = service_for(ready)
    with factory() as session:
        seed_project(session)
        seed_task(session, "TASK-SIM", execution_class="simulation")
        seed_task(session, "TASK-NO", schedulable=False)
        seed_task(session, "TASK-FAILED", status="failed")
        seed_task(session, ready.id, title=ready.title)

        result = service.run_once(session, project_id="project-1")

        assert result.status == "PACKAGE_READY"
        assert result.task is not None
        assert result.task.id == ready.id


def test_expired_lease_allows_iteration() -> None:
    factory = session_factory()
    task = manifest_task("TASK-A")
    service = service_for(task)
    with factory() as session:
        seed_project(session)
        seed_task(session, task.id, title=task.title)
        first = service.run_once(session, project_id="project-1")
        assert first.lease is not None
        first.lease.status = "expired"
        session.get(Task, task.id).status = "pending"  # type: ignore[union-attr]
        session.flush()

        second = service.run_once(session, project_id="project-1")

        assert second.status == "PACKAGE_READY"
        assert second.execution is not None
        assert second.execution.attempt == 2


def test_persisted_timeout_consumes_attempt_number() -> None:
    factory = session_factory()
    task = manifest_task("TASK-TIMEOUT")
    service = service_for(task)
    with factory() as session:
        seed_project(session)
        seed_task(session, task.id, title=task.title)
        session.add(
            Execution(
                id="execution-timeout-1",
                task_id=task.id,
                executor_type="codex",
                status="timeout",
                attempt=1,
                terminal_state="timeout",
                error_classification="executor_timeout",
            )
        )
        session.flush()

        result = service.run_once(session, project_id="project-1")

        assert result.status == "PACKAGE_READY"
        assert result.execution is not None
        assert result.execution.attempt == 2


def test_claim_contention_is_structured() -> None:
    factory = session_factory()
    task = manifest_task("TASK-A")

    class ContendedClaiming:
        def claim_task(self, session: Session, **kwargs: object) -> ClaimResult:
            return ClaimResult("ALREADY_LEASED", task_id=task.id, owner_id="worker-2")

    service = SchedulerIterationService(
        claiming=ContendedClaiming(),  # type: ignore[arg-type]
        package_factory=ExecutionPackageFactory(manifest_tasks={task.id: task}),
    )
    with factory() as session:
        seed_project(session)
        seed_task(session, task.id, title=task.title)

        result = service.run_once(session, project_id="project-1")

        assert result.status == "CLAIM_CONTENTION"
        assert result.package is None


def test_manifest_db_mismatch_releases_lease_and_fails_execution() -> None:
    factory = session_factory()
    task = manifest_task("TASK-A", title="Manifest title")
    service = service_for(task)
    with factory() as session:
        seed_project(session)
        seed_task(session, task.id, title="Database title")

        result = service.run_once(session, project_id="project-1")

        assert result.status == "ERROR"
        assert result.reason == "manifest/db task mismatch: TASK-A"
        assert result.execution is not None
        assert result.execution.status == "failed"
        assert result.lease is not None
        assert result.lease.status == "released"
        assert session.get(Task, task.id).status == "blocked"  # type: ignore[union-attr]


def test_scheduler_iteration_does_not_invoke_executor_verifier_or_git(monkeypatch) -> None:
    factory = session_factory()
    task = manifest_task("TASK-A")

    def forbidden_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("scheduler iteration must not run subprocesses")

    monkeypatch.setattr(subprocess, "run", forbidden_run)
    service = service_for(task)
    with factory() as session:
        seed_project(session)
        seed_task(session, task.id, title=task.title)

        result = service.run_once(session, project_id="project-1")

        assert result.status == "PACKAGE_READY"
