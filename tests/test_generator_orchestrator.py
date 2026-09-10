from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.execution_planner import ExecutionPlannerService
from ai_ent.generator_orchestrator import (
    EVIDENCE_RECORDING_INTERFACE_ID,
    GENERATOR_ORCHESTRATOR_CONTRACT_VERSION,
    GENERATOR_ORCHESTRATOR_OUTPUT_SCHEMA_VERSION,
    WORKER_PACKAGE_INTERFACE_ID,
    GeneratorBoundary,
    GeneratorContract,
    GeneratorOrchestratorService,
)
from ai_ent.persistence.models import Base, Execution, TaskLease
from ai_ent.project_manifest import EnvironmentProfile

MANIFEST_ROOT = Path("manifest/project/ai-ent")


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
