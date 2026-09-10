from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.deterministic_validator import (
    DETERMINISTIC_VALIDATOR_VERSION,
    DeterministicValidationFinding,
    DeterministicValidationReport,
    DeterministicValidatorService,
)
from ai_ent.execution_planner import ExecutionPlannerService
from ai_ent.persistence.models import (
    Base,
    Execution,
    RuntimeHumanGate,
    Task,
    TaskDependency,
    TaskLease,
)
from ai_ent.project_manifest import EnvironmentProfile
from ai_ent.runtime_handoff import DEFAULT_RUNTIME_PROJECT_ID
from ai_ent.runtime_kernel import (
    C20_RUNTIME_KERNEL_REQUIREMENTS,
    C20_RUNTIME_STOP_CONDITIONS,
    RUNTIME_KERNEL_COMPONENT_ID,
    RUNTIME_KERNEL_CONTRACT_VERSION,
    RUNTIME_KERNEL_OUTPUT_SCHEMA_VERSION,
    RuntimeKernelService,
)

MANIFEST_ROOT = Path("manifest/project/ai-ent")
CMP_006_TASK_ID = "IMPL-C20-CMP-006"
CMP_006_EXECUTION_ID = "exec-IMPL-C20-CMP-006-001"


def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def test_cmp_006_runtime_kernel_blocks_pending_gate_without_side_effects(
    tmp_path: Path,
) -> None:
    service = ExecutionPlannerService()
    plan = service.plan(MANIFEST_ROOT, environment=_environment(codex_configured=False))
    factory = session_factory()

    with factory() as session:
        service.import_plan(
            session,
            plan,
            require_clean_git=False,
            require_codex_command=False,
            repository_root=tmp_path,
        )

        first = RuntimeKernelService().evaluate_task(
            session,
            task_id=CMP_006_TASK_ID,
            execution_id=CMP_006_EXECUTION_ID,
        )
        second = RuntimeKernelService().evaluate_task(
            session,
            task_id=CMP_006_TASK_ID,
            execution_id=CMP_006_EXECUTION_ID,
        )

        assert first.as_dict() == second.as_dict()
        assert first.contract_version == RUNTIME_KERNEL_CONTRACT_VERSION
        assert first.schema_version == RUNTIME_KERNEL_OUTPUT_SCHEMA_VERSION
        assert first.as_dict()["component_id"] == RUNTIME_KERNEL_COMPONENT_ID
        assert first.status == "BLOCKED"
        assert first.worker_package_permitted is False
        assert first.requirements_covered == C20_RUNTIME_KERNEL_REQUIREMENTS
        assert first.capabilities_covered == ("C20",)
        assert first.supported_stop_conditions == C20_RUNTIME_STOP_CONDITIONS
        assert "pending_human_gate" in first.active_stop_conditions
        assert f"human approval required:{CMP_006_TASK_ID}:pending" in first.blockers
        assert f"runtime task is not ready:{CMP_006_TASK_ID}:NOT_SCHEDULABLE" in first.blockers
        assert first.task_context.human_gate_ids == (f"GATE-{CMP_006_TASK_ID}",)
        assert first.task_context.human_gate_state == "pending"
        assert len(first.runtime_status_hash) == 64
        assert len(first.project_memory_hash) == 64
        assert len(first.deterministic_validation_hash) == 64
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
        assert session.scalar(select(func.count()).select_from(TaskLease)) == 0


def test_cmp_006_runtime_kernel_permits_package_only_after_gate_and_dependencies(
    tmp_path: Path,
) -> None:
    service = ExecutionPlannerService()
    plan = service.plan(MANIFEST_ROOT, environment=_environment(codex_configured=False))
    factory = session_factory()

    with factory() as session:
        service.import_plan(
            session,
            plan,
            require_clean_git=False,
            require_codex_command=False,
            repository_root=tmp_path,
        )
        _approve_gate(session, CMP_006_TASK_ID)
        _mark_direct_dependencies_passed(session, CMP_006_TASK_ID)

        result = RuntimeKernelService().evaluate_task(
            session,
            task_id=CMP_006_TASK_ID,
            execution_id=CMP_006_EXECUTION_ID,
        )

        assert result.ok
        assert result.worker_package_permitted
        assert result.blockers == ()
        assert result.active_stop_conditions == ()
        assert result.readiness_status == "READY"
        assert result.task_context.human_gate_state == "approved"
        assert CMP_006_TASK_ID in result.ready_task_ids
        assert {boundary.service for boundary in result.service_boundaries} == {
            "runtime-handoff",
            "runtime-kernel",
            "project-memory",
            "deterministic-validator",
            "scheduler-readiness",
            "artifact-evidence-graph",
        }
        assert result.as_dict()["summary"]["requirements_covered"] == list(C20_RUNTIME_KERNEL_REQUIREMENTS)


def test_cmp_006_runtime_kernel_enforces_operational_limits(
    tmp_path: Path,
) -> None:
    service = ExecutionPlannerService()
    plan = service.plan(MANIFEST_ROOT, environment=_environment(codex_configured=False))
    factory = session_factory()

    with factory() as session:
        service.import_plan(
            session,
            plan,
            require_clean_git=False,
            require_codex_command=False,
            repository_root=tmp_path,
        )
        _approve_gate(session, CMP_006_TASK_ID)
        _mark_direct_dependencies_passed(session, CMP_006_TASK_ID)

        result = RuntimeKernelService().evaluate_task(
            session,
            task_id=CMP_006_TASK_ID,
            execution_id=CMP_006_EXECUTION_ID,
            tasks_attempted_this_run=1,
            max_tasks_per_run=1,
            failures_this_run=1,
            max_failures_per_run=1,
            elapsed_wall_clock_seconds=900.0,
            max_wall_clock_seconds=900.0,
            crash_recovery_required=True,
        )

        assert result.status == "BLOCKED"
        assert result.worker_package_permitted is False
        assert {
            "failure_limit",
            "time_limit",
            "task_limit",
            "crash_recovery_before_resume",
        } <= set(result.active_stop_conditions)
        assert "runtime task limit reached" in result.blockers
        assert "runtime failure limit reached" in result.blockers
        assert "runtime wall-clock limit reached" in result.blockers
        assert "crash recovery required before resume" in result.blockers


def test_cmp_006_runtime_kernel_blocks_failed_deterministic_validation(
    tmp_path: Path,
) -> None:
    service = ExecutionPlannerService()
    plan = service.plan(MANIFEST_ROOT, environment=_environment(codex_configured=False))
    factory = session_factory()

    with factory() as session:
        service.import_plan(
            session,
            plan,
            require_clean_git=False,
            require_codex_command=False,
            repository_root=tmp_path,
        )
        _approve_gate(session, CMP_006_TASK_ID)
        _mark_direct_dependencies_passed(session, CMP_006_TASK_ID)

        result = RuntimeKernelService(validator=FailingValidator()).evaluate_task(
            session,
            task_id=CMP_006_TASK_ID,
            execution_id=CMP_006_EXECUTION_ID,
        )

        assert result.status == "BLOCKED"
        assert result.worker_package_permitted is False
        assert "deterministic validation failed" in result.blockers
        assert "reconciliation_required" in result.active_stop_conditions


def test_cmp_006_runtime_kernel_blocks_missing_runtime_import() -> None:
    factory = session_factory()

    with factory() as session:
        result = RuntimeKernelService().evaluate_task(
            session,
            task_id=CMP_006_TASK_ID,
            execution_id=CMP_006_EXECUTION_ID,
            project_id=DEFAULT_RUNTIME_PROJECT_ID,
        )

        assert result.status == "BLOCKED"
        assert result.worker_package_permitted is False
        assert result.requirements_covered == C20_RUNTIME_KERNEL_REQUIREMENTS
        assert "runtime_error" in result.active_stop_conditions
        assert f"runtime plan is not imported:{DEFAULT_RUNTIME_PROJECT_ID}" in result.blockers


class FailingValidator(DeterministicValidatorService):
    def validate(self, _: Path) -> DeterministicValidationReport:
        return DeterministicValidationReport(
            source_compiled_hash="0" * 64,
            model_hash="1" * 64,
            validator_version=DETERMINISTIC_VALIDATOR_VERSION,
            report_hash="2" * 64,
            checks=(),
            findings=(
                DeterministicValidationFinding(
                    finding_id="DV-FAIL",
                    severity="ERROR",
                    requirement_id="NFR-004",
                    subject_id="runtime-kernel",
                    message="forced failure",
                ),
            ),
            generated_artifacts=(),
            artifact_authority=(),
        )


def _approve_gate(session: Session, task_id: str) -> None:
    gate = session.get(RuntimeHumanGate, f"GATE-{task_id}")
    assert gate is not None
    gate.status = "approved"
    session.flush()


def _mark_direct_dependencies_passed(session: Session, task_id: str) -> None:
    dependency_ids = session.scalars(
        select(TaskDependency.depends_on_task_id).where(TaskDependency.task_id == task_id)
    ).all()
    for dependency_id in dependency_ids:
        task = session.get(Task, dependency_id)
        assert task is not None
        task.status = "passed"
    session.flush()


def _environment(*, codex_configured: bool = True) -> EnvironmentProfile:
    return EnvironmentProfile(
        profile_id="test",
        repository_path="/repo",
        git_available=True,
        postgresql_available=True,
        alembic_available=True,
        docker_available=True,
        docker_compose_available=True,
        codex_command_configured=codex_configured,
        codex_executable_available=True,
        python_path="/repo/aient/bin/python",
        python_available=True,
        ram_mb=8192,
        gpu_available=False,
        external_network="available",
        configured_secret_names=(),
    )
