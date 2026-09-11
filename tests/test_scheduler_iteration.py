from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
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
from ai_ent.runtime_handoff import DEFAULT_RUNTIME_PROJECT_ID
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
    fingerprint: str | None = None,
) -> Task:
    return TaskRepository().create(
        session,
        task_id=task_id,
        project_id=project_id,
        title=title or f"Task {task_id}",
        status=status,  # type: ignore[arg-type]
        schedulable=schedulable,
        execution_class=execution_class,  # type: ignore[arg-type]
        fingerprint=fingerprint,
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


def seed_runtime_plan_import(
    session: Session,
    project_id: str = "project-1",
    *,
    import_id: str | None = None,
    plan_id: str | None = None,
) -> RuntimePlanImport:
    plan_import = RuntimePlanImport(
        id=import_id or f"import-{project_id}",
        project_id=project_id,
        plan_project_id=project_id,
        plan_id=plan_id or f"PLAN-{project_id}",
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
    fingerprint: str | None = None,
    risk_level: str = "MEDIUM",
    agent_role: str = "test-agent",
    model_profile: str = "STANDARD",
    executor: str = "codex",
    verification_profile: str = "STANDARD_REGRESSION",
    write_scope: dict[str, object] | None = None,
    acceptance: dict[str, object] | None = None,
) -> RuntimeTaskPlanBinding:
    binding = RuntimeTaskPlanBinding(
        task_id=task.id,
        import_id=plan_import.id,
        plan_id=plan_import.plan_id,
        plan_version=plan_import.plan_version,
        fingerprint=fingerprint or task.fingerprint or f"fingerprint-{task.id}",
        risk_level=risk_level,
        agent_role=agent_role,
        model_profile=model_profile,
        executor=executor,
        verification_profile=verification_profile,
        feasibility_status="FEASIBLE",
        policy_decision="AUTO_ALLOWED",
        implements_json="{}",
        write_scope_json=json.dumps(write_scope or {}),
        acceptance_json=json.dumps(acceptance or {}),
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


def test_scheduler_iteration_loads_runtime_bound_product_task_without_bootstrap_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = session_factory()
    commands = (
        "/repo/aient/bin/python -m pytest -q",
        "/repo/aient/bin/python -m ruff check .",
        "/repo/aient/bin/python -m pyright",
        "/repo/aient/bin/python -m pytest tests/custom.py -q",
        "/repo/aient/bin/python -m pytest tests/custom.py -q",
    )

    def fail_if_bootstrap_loader_is_used() -> dict[str, BootstrapTask]:
        raise AssertionError("runtime-bound package construction must not call bootstrap load_tasks")

    monkeypatch.setattr("ai_ent.scheduler.iteration.load_tasks", fail_if_bootstrap_loader_is_used)
    service = SchedulerIterationService(
        package_factory=ExecutionPackageFactory(
            repository_path=Path("/repo"),
            timeout_policy=ExecutionTimeoutPolicy.for_codex_default(900),
        ),
        owner_id="worker-1",
        lease_duration=timedelta(hours=1),
    )
    with factory() as session:
        seed_project(session, DEFAULT_RUNTIME_PROJECT_ID)
        task = seed_task(
            session,
            "PRD-TASK-003",
            project_id=DEFAULT_RUNTIME_PROJECT_ID,
            title="Reusable external-project runtime importer",
            fingerprint="p" * 64,
        )
        plan_import = seed_runtime_plan_import(
            session,
            DEFAULT_RUNTIME_PROJECT_ID,
            import_id="import-product",
            plan_id="PRODUCT-PLAN-test",
        )
        seed_runtime_binding(
            session,
            task,
            plan_import,
            risk_level="HIGH",
            model_profile="MDL-001:Codex executor model:high-risk-guarded",
            verification_profile="FULL_REGRESSION_SECURITY",
            write_scope={"allowed": ["src/ai_ent/**"], "prohibited": [".env", ".build/**"]},
            acceptance={
                "acceptance_criteria": ["runtime package can be built"],
                "verification_commands": list(commands),
            },
        )

        result = service.run_once(session, project_id=DEFAULT_RUNTIME_PROJECT_ID)

        assert result.status == "PACKAGE_READY"
        assert result.package is not None
        assert result.package.task_id == "PRD-TASK-003"
        assert result.package.timeout_seconds == 1800
        assert result.package.allowed_paths == ("src/ai_ent/**",)
        assert result.package.prohibited_paths == (".env", ".build/**")
        assert result.package.instructions.count(commands[-1]) == 2
        command_offsets = [result.package.instructions.index(command) for command in commands[:-1]]
        command_offsets.append(result.package.instructions.rindex(commands[-1]))
        assert command_offsets == sorted(command_offsets)


def test_runtime_bound_package_factory_rejects_without_session() -> None:
    task = Task(
        id="PRD-TASK-003",
        project_id=DEFAULT_RUNTIME_PROJECT_ID,
        title="Runtime bound task",
        fingerprint="p" * 64,
    )
    task.runtime_plan_binding = RuntimeTaskPlanBinding(
        task_id=task.id,
        import_id="import-product",
        plan_id="PRODUCT-PLAN-test",
        plan_version="1",
        fingerprint=task.fingerprint or "",
        risk_level="HIGH",
        agent_role="agent",
        model_profile="MDL-001:Codex executor model:high-risk-guarded",
        executor="codex",
        verification_profile="FULL_REGRESSION_SECURITY",
        feasibility_status="FEASIBLE",
        policy_decision="HUMAN_APPROVAL_REQUIRED",
        implements_json="{}",
        write_scope_json="{}",
        acceptance_json="{}",
    )

    try:
        ExecutionPackageFactory().build(task, execution_id="execution-1")
    except ValueError as exc:
        assert "runtime manifest task loader requires a database session" in str(exc)
    else:
        raise AssertionError("expected runtime-bound package construction to require a session")


def test_runtime_bound_package_factory_rejects_unknown_runtime_task_without_bootstrap_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = session_factory()
    stale_bootstrap = manifest_task("PRD-TASK-UNKNOWN", title="Unknown runtime task")
    monkeypatch.setattr("ai_ent.scheduler.iteration.load_tasks", lambda: {stale_bootstrap.id: stale_bootstrap})

    with factory() as session:
        seed_project(session, DEFAULT_RUNTIME_PROJECT_ID)
        task = seed_task(
            session,
            stale_bootstrap.id,
            project_id=DEFAULT_RUNTIME_PROJECT_ID,
            title=stale_bootstrap.title,
            fingerprint="a" * 64,
        )
        plan_import = seed_runtime_plan_import(
            session,
            DEFAULT_RUNTIME_PROJECT_ID,
            import_id="import-unknown-runtime",
            plan_id="PLAN-UNKNOWN",
        )
        seed_runtime_binding(session, task, plan_import, fingerprint="b" * 64)

        try:
            ExecutionPackageFactory(manifest_tasks=None).build(
                task,
                execution_id="execution-unknown",
                session=session,
                project_id=DEFAULT_RUNTIME_PROJECT_ID,
            )
        except ValueError as exc:
            assert "runtime task is not imported from a valid frozen plan" in str(exc)
        else:
            raise AssertionError("expected invalid runtime binding to be rejected")


def test_runtime_package_factory_supports_imported_impl_res_and_prd_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = session_factory()
    commands = ("pytest", "ruff", "pyright", "custom")
    monkeypatch.setattr(
        "ai_ent.scheduler.iteration.load_tasks",
        lambda: {
            "PRD-TASK-003": manifest_task("PRD-TASK-003", title="Stale bootstrap collision"),
        },
    )

    with factory() as session:
        seed_project(session, DEFAULT_RUNTIME_PROJECT_ID)
        task_ids = (
            ("IMPL-C01-CMP-001", "PLAN-original", "LOW"),
            ("RES-C05-EVIDENCE", "RESIDUAL-PLAN-test", "MEDIUM"),
            ("PRD-TASK-003", "PRODUCT-PLAN-test", "HIGH"),
        )
        for index, (task_id, plan_id, risk) in enumerate(task_ids):
            fingerprint = f"{index}" * 64
            task = seed_task(
                session,
                task_id,
                project_id=DEFAULT_RUNTIME_PROJECT_ID,
                title=f"Runtime {task_id}",
                fingerprint=fingerprint,
            )
            plan_import = seed_runtime_plan_import(
                session,
                DEFAULT_RUNTIME_PROJECT_ID,
                import_id=f"import-{index}",
                plan_id=plan_id,
            )
            seed_runtime_binding(
                session,
                task,
                plan_import,
                risk_level=risk,
                write_scope={"allowed": [f"src/{task_id}/**"], "prohibited": [".env"]},
                acceptance={"verification_commands": list(commands)},
            )

        package_factory = ExecutionPackageFactory(
            repository_path=Path("/repo"),
            timeout_policy=ExecutionTimeoutPolicy.for_standard_timeout(1800),
        )
        for task_id, _, _ in task_ids:
            persisted = session.get(Task, task_id)
            assert persisted is not None

            package = package_factory.build(
                persisted,
                execution_id=f"execution-{task_id}",
                session=session,
                project_id=DEFAULT_RUNTIME_PROJECT_ID,
            )

            assert package.task_id == task_id
            assert package.timeout_seconds == 1800
            assert package.allowed_paths == (f"src/{task_id}/**",)
            command_offsets = [package.instructions.index(command) for command in commands]
            assert command_offsets == sorted(command_offsets)


def test_bootstrap_only_package_factory_still_uses_explicit_bootstrap_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = session_factory()
    bootstrap_task = manifest_task("TASK-A")
    monkeypatch.setattr("ai_ent.scheduler.iteration.load_tasks", lambda: {bootstrap_task.id: bootstrap_task})

    with factory() as session:
        seed_project(session, "bootstrap")
        task = seed_task(session, "TASK-A", project_id="bootstrap", title=bootstrap_task.title)

        package = ExecutionPackageFactory(repository_path=Path("/repo"), timeout_seconds=30).build(
            task,
            execution_id="execution-bootstrap",
        )

    assert package.task_id == "TASK-A"
    assert package.timeout_seconds == 30
    assert package.allowed_paths == bootstrap_task.allowed_paths


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
