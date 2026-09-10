from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.execution_planner import ExecutionPlannerService
from ai_ent.generator_orchestrator import (
    C20_GENERATOR_REQUIREMENTS,
    EVIDENCE_RECORDING_INTERFACE_ID,
    GENERATOR_ORCHESTRATOR_CONTRACT_VERSION,
    GENERATOR_ORCHESTRATOR_OUTPUT_SCHEMA_VERSION,
    WORKER_PACKAGE_INTERFACE_ID,
    GeneratorBoundary,
    GeneratorContract,
    GeneratorOrchestratorService,
)
from ai_ent.persistence.models import (
    Base,
    Execution,
    RuntimeHumanGate,
    Task,
    TaskDependency,
    TaskLease,
)
from ai_ent.project_manifest import EnvironmentProfile

MANIFEST_ROOT = Path("manifest/project/ai-ent")
C20_TASK_ID = "IMPL-C20-CMP-005"
C20_EXECUTION_ID = "exec-IMPL-C20-CMP-005-001"


def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def test_fr_005_generator_orchestrator_coordinates_without_completion_authority(
    tmp_path: Path,
) -> None:
    service = ExecutionPlannerService()
    factory = session_factory()
    plan = service.plan(MANIFEST_ROOT, environment=_environment(codex_configured=False))

    with factory() as session:
        import_result = service.import_plan(
            session,
            plan,
            require_clean_git=False,
            require_codex_command=False,
            repository_root=tmp_path,
        )
        first = GeneratorOrchestratorService(planner=service).coordinate_task(
            session,
            task_id="IMPL-C18-CMP-005",
            execution_id="exec-IMPL-C18-CMP-005-001",
            repository_path=tmp_path,
            timeout_seconds=123,
        )
        second = GeneratorOrchestratorService(planner=service).coordinate_task(
            session,
            task_id="IMPL-C18-CMP-005",
            execution_id="exec-IMPL-C18-CMP-005-001",
            repository_path=tmp_path,
            timeout_seconds=123,
        )

        assert import_result.status == "IMPORTED"
        assert first.ok
        assert first.contract_version == GENERATOR_ORCHESTRATOR_CONTRACT_VERSION
        assert first.schema_version == GENERATOR_ORCHESTRATOR_OUTPUT_SCHEMA_VERSION
        assert first.as_dict() == second.as_dict()
        assert first.output_hash == second.output_hash
        assert first.as_dict()["summary"]["completion_authority_granted"] is False
        assert first.as_dict()["summary"]["commit_authority_granted"] is False
        assert first.as_dict()["summary"]["push_authority_granted"] is False
        assert first.work_orders[0].task_id == "IMPL-C18-CMP-005"
        assert first.work_orders[0].denied_authorities == (
            "complete_task",
            "commit",
            "push",
            "schedule",
            "verify",
        )
        assert first.evidence_records[0].output_authority_state == "NON_AUTHORITATIVE"
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
        assert session.scalar(select(func.count()).select_from(TaskLease)) == 0


def test_int_002_generator_adapters_require_explicit_contract_boundaries(
    tmp_path: Path,
) -> None:
    service = ExecutionPlannerService()
    factory = session_factory()
    plan = service.plan(MANIFEST_ROOT, environment=_environment(codex_configured=False))
    invalid_contract = GeneratorContract(
        generator_id="raw-tool",
        name="Raw tool without worker package boundary",
        adapter_kind="tool",
        input_boundary=GeneratorBoundary(
            service="runtime-kernel",
            interface_id="IF-999",
            contract_version="raw",
            schema_version="raw",
        ),
        output_boundary=GeneratorBoundary(
            service="artifact-evidence-graph",
            interface_id=EVIDENCE_RECORDING_INTERFACE_ID,
            contract_version=GENERATOR_ORCHESTRATOR_CONTRACT_VERSION,
            schema_version=GENERATOR_ORCHESTRATOR_OUTPUT_SCHEMA_VERSION,
        ),
        capabilities=("C18",),
    )

    with factory() as session:
        service.import_plan(
            session,
            plan,
            require_clean_git=False,
            require_codex_command=False,
            repository_root=tmp_path,
        )
        result = GeneratorOrchestratorService(
            planner=service,
            generator_contracts=(invalid_contract,),
        ).coordinate_task(
            session,
            task_id="IMPL-C18-CMP-005",
            execution_id="exec-IMPL-C18-CMP-005-001",
            generator_id="raw-tool",
            repository_path=tmp_path,
        )

        assert result.status == "BLOCKED"
        assert result.work_orders == ()
        assert "generator input boundary is not execution-planner:raw-tool" in result.blockers
        assert f"generator input interface is not {WORKER_PACKAGE_INTERFACE_ID}:raw-tool" in result.blockers


def test_sec_002_worker_scope_is_bounded_and_forbidden_authority_is_rejected(
    tmp_path: Path,
) -> None:
    service = ExecutionPlannerService()
    factory = session_factory()
    plan = service.plan(MANIFEST_ROOT, environment=_environment(codex_configured=False))

    with factory() as session:
        service.import_plan(
            session,
            plan,
            require_clean_git=False,
            require_codex_command=False,
            repository_root=tmp_path,
        )
        result = GeneratorOrchestratorService(planner=service).coordinate_task(
            session,
            task_id="IMPL-C18-CMP-005",
            execution_id="exec-IMPL-C18-CMP-005-001",
            repository_path=tmp_path,
        )

        assert result.ok
        package = result.worker_package_plans[0].package
        assert "src/ai_ent/**" in package.allowed_paths
        assert "tests/**" in package.allowed_paths
        assert ".env" in package.prohibited_paths
        assert "Do not verify, commit, push, or schedule another task." in package.instructions
        assert result.work_orders[0].allowed_paths == package.allowed_paths
        assert result.work_orders[0].prohibited_paths == package.prohibited_paths

    forbidden_contract = GeneratorContract(
        generator_id="unsafe-generator",
        name="Unsafe generator",
        adapter_kind="tool",
        input_boundary=GeneratorBoundary(
            service="execution-planner",
            interface_id=WORKER_PACKAGE_INTERFACE_ID,
            contract_version="c17.1",
            schema_version="execution-planner-output-v0.1",
        ),
        output_boundary=GeneratorBoundary(
            service="artifact-evidence-graph",
            interface_id=EVIDENCE_RECORDING_INTERFACE_ID,
            contract_version=GENERATOR_ORCHESTRATOR_CONTRACT_VERSION,
            schema_version=GENERATOR_ORCHESTRATOR_OUTPUT_SCHEMA_VERSION,
        ),
        capabilities=("C18",),
        allowed_authorities=("generate", "commit", "push"),
    )

    with factory() as session:
        service.import_plan(
            session,
            plan,
            require_clean_git=False,
            require_codex_command=False,
            repository_root=tmp_path,
        )
        denied = GeneratorOrchestratorService(
            planner=service,
            generator_contracts=(forbidden_contract,),
        ).coordinate_task(
            session,
            task_id="IMPL-C18-CMP-005",
            execution_id="exec-IMPL-C18-CMP-005-001",
            generator_id="unsafe-generator",
            repository_path=tmp_path,
        )

        assert denied.status == "BLOCKED"
        assert "forbidden authority granted:unsafe-generator:commit" in denied.blockers
        assert "forbidden authority granted:unsafe-generator:push" in denied.blockers
        assert denied.work_orders == ()


def test_c20_runtime_orchestration_blocks_pending_human_gate_with_evidence(
    tmp_path: Path,
) -> None:
    service = ExecutionPlannerService()
    factory = session_factory()
    plan = service.plan(MANIFEST_ROOT, environment=_environment(codex_configured=False))

    with factory() as session:
        service.import_plan(
            session,
            plan,
            require_clean_git=False,
            require_codex_command=False,
            repository_root=tmp_path,
        )
        first = GeneratorOrchestratorService(planner=service).coordinate_runtime_task(
            session,
            task_id=C20_TASK_ID,
            execution_id=C20_EXECUTION_ID,
            repository_path=tmp_path,
        )
        second = GeneratorOrchestratorService(planner=service).coordinate_runtime_task(
            session,
            task_id=C20_TASK_ID,
            execution_id=C20_EXECUTION_ID,
            repository_path=tmp_path,
        )

        assert first.as_dict() == second.as_dict()
        assert first.status == "BLOCKED"
        assert first.work_orders == ()
        assert f"human approval required:{C20_TASK_ID}:pending" in first.blockers
        assert tuple(first.as_dict()["summary"]["requirements_covered"]) == C20_GENERATOR_REQUIREMENTS
        assert first.as_dict()["summary"]["capabilities_covered"] == ["C20"]
        assert first.as_dict()["summary"]["output_authority_state"] == "NON_AUTHORITATIVE"
        evidence = first.runtime_evidence[0]
        assert evidence.task_id == C20_TASK_ID
        assert evidence.human_gate_ids == (f"GATE-{C20_TASK_ID}",)
        assert evidence.human_gate_state == "pending"
        assert {"pending_human_gate", "reconciliation_required", "task_limit"} <= set(
            evidence.stop_conditions
        )
        assert evidence.worker_package_hash is None
        assert evidence.work_order_input_hash is None
        assert len(evidence.project_memory_hash) == 64
        assert len(evidence.deterministic_validation_hash) == 64
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
        assert session.scalar(select(func.count()).select_from(TaskLease)) == 0


def test_c20_approved_runtime_orchestration_builds_bounded_worker_package(
    tmp_path: Path,
) -> None:
    service = ExecutionPlannerService()
    factory = session_factory()
    plan = service.plan(MANIFEST_ROOT, environment=_environment(codex_configured=False))

    with factory() as session:
        service.import_plan(
            session,
            plan,
            require_clean_git=False,
            require_codex_command=False,
            repository_root=tmp_path,
        )
        gate = session.get(RuntimeHumanGate, f"GATE-{C20_TASK_ID}")
        assert gate is not None
        gate.status = "approved"
        _mark_direct_dependencies_passed(session, C20_TASK_ID)
        session.flush()

        result = GeneratorOrchestratorService(planner=service).coordinate_runtime_task(
            session,
            task_id=C20_TASK_ID,
            execution_id=C20_EXECUTION_ID,
            repository_path=tmp_path,
            timeout_seconds=321,
        )

        assert result.ok
        assert result.output_hash == GeneratorOrchestratorService(
            planner=service
        ).coordinate_runtime_task(
            session,
            task_id=C20_TASK_ID,
            execution_id=C20_EXECUTION_ID,
            repository_path=tmp_path,
            timeout_seconds=321,
        ).output_hash
        package = result.worker_package_plans[0].package
        work_order = result.work_orders[0]
        runtime_evidence = result.runtime_evidence[0]
        boundaries = {boundary.service: boundary.interface_id for boundary in runtime_evidence.service_boundaries}

        assert package.task_id == C20_TASK_ID
        assert package.allowed_paths == ("src/ai_ent/**", "tests/**", "scripts/**")
        assert ".env" in package.prohibited_paths
        assert work_order.denied_authorities == (
            "complete_task",
            "commit",
            "push",
            "schedule",
            "verify",
        )
        assert work_order.timeout_seconds == 321
        assert runtime_evidence.worker_package_hash == result.worker_package_plans[0].package_hash
        assert runtime_evidence.work_order_input_hash == work_order.input_hash
        assert runtime_evidence.human_gate_state == "approved"
        assert runtime_evidence.requirements_covered == C20_GENERATOR_REQUIREMENTS
        assert boundaries["execution-planner"] == WORKER_PACKAGE_INTERFACE_ID
        assert boundaries["runtime-kernel"] == WORKER_PACKAGE_INTERFACE_ID
        assert boundaries["project-memory"] == EVIDENCE_RECORDING_INTERFACE_ID
        assert boundaries["artifact-evidence-graph"] == EVIDENCE_RECORDING_INTERFACE_ID
        assert boundaries["deterministic-validator"] == EVIDENCE_RECORDING_INTERFACE_ID
        assert result.evidence_records[0].output_authority_state == "NON_AUTHORITATIVE"
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
        assert session.scalar(select(func.count()).select_from(TaskLease)) == 0


def test_c20_runtime_orchestration_requires_c20_generator_contract(
    tmp_path: Path,
) -> None:
    service = ExecutionPlannerService()
    factory = session_factory()
    plan = service.plan(MANIFEST_ROOT, environment=_environment(codex_configured=False))
    c18_only_contract = GeneratorContract(
        generator_id="c18-only-generator",
        name="C18-only generator",
        adapter_kind="tool",
        input_boundary=GeneratorBoundary(
            service="execution-planner",
            interface_id=WORKER_PACKAGE_INTERFACE_ID,
            contract_version="c17.1",
            schema_version="execution-planner-output-v0.1",
        ),
        output_boundary=GeneratorBoundary(
            service="artifact-evidence-graph",
            interface_id=EVIDENCE_RECORDING_INTERFACE_ID,
            contract_version=GENERATOR_ORCHESTRATOR_CONTRACT_VERSION,
            schema_version=GENERATOR_ORCHESTRATOR_OUTPUT_SCHEMA_VERSION,
        ),
        capabilities=("C18",),
    )

    with factory() as session:
        service.import_plan(
            session,
            plan,
            require_clean_git=False,
            require_codex_command=False,
            repository_root=tmp_path,
        )
        gate = session.get(RuntimeHumanGate, f"GATE-{C20_TASK_ID}")
        assert gate is not None
        gate.status = "approved"
        session.flush()

        result = GeneratorOrchestratorService(
            planner=service,
            generator_contracts=(c18_only_contract,),
        ).coordinate_runtime_task(
            session,
            task_id=C20_TASK_ID,
            execution_id=C20_EXECUTION_ID,
            generator_id="c18-only-generator",
            repository_path=tmp_path,
        )

        assert result.status == "BLOCKED"
        assert "generator contract does not declare C20 capability:c18-only-generator" in result.blockers
        assert result.work_orders == ()


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


def _mark_direct_dependencies_passed(session: Session, task_id: str) -> None:
    dependency_ids = session.scalars(
        select(TaskDependency.depends_on_task_id).where(TaskDependency.task_id == task_id)
    ).all()
    for dependency_id in dependency_ids:
        task = session.get(Task, dependency_id)
        assert task is not None
        task.status = "passed"
