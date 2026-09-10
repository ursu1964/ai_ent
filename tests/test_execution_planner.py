from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from ai_ent import project_manifest
from ai_ent.bootstrap.models import ExecutionPackage
from ai_ent.execution_planner import (
    EXECUTION_PLANNER_CONTRACT_VERSION,
    EXECUTION_PLANNER_OUTPUT_SCHEMA_VERSION,
    ExecutionPlannerService,
    create_execution_plan,
)
from ai_ent.persistence.models import (
    Base,
    Execution,
    RuntimeHumanGate,
    RuntimePlanImport,
    RuntimeTaskPlanBinding,
    Task,
    TaskDependency,
    TaskLease,
)
from ai_ent.project_manifest import EnvironmentProfile
from ai_ent.runtime_handoff import DEFAULT_RUNTIME_PROJECT_ID

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


def test_cmp_004_acc_002_nfr_001_planner_boundary_is_deterministic() -> None:
    first = create_execution_plan(MANIFEST_ROOT, environment=_environment(codex_configured=False))
    second = create_execution_plan(MANIFEST_ROOT, environment=_environment(codex_configured=False))

    assert first.contract_version == EXECUTION_PLANNER_CONTRACT_VERSION
    assert first.schema_version == EXECUTION_PLANNER_OUTPUT_SCHEMA_VERSION
    assert first.validation_passed
    assert first.ready_for_runtime_import
    assert first.output_hash == second.output_hash
    assert first.as_dict() == second.as_dict()
    assert (
        first.knowledge_graph.source_compiled_hash
        == first.implementation_plan.source_compiled_hash
    )
    assert first.as_dict()["input_boundary"]["service"] == "knowledge-graph"
    assert first.as_dict()["output_boundary"]["service"] == "runtime-handoff"
    assert "IMPL-C15-CMP-002" in first.task_ids_for_requirement("ACC-002")
    assert "IMPL-C17-CMP-004" in first.task_ids_for_requirement("NFR-001")


def test_acc_002_invalid_manifest_blocks_before_dag_generation(
    tmp_path: Path,
) -> None:
    target = tmp_path / "manifest"
    _copy_manifest(MANIFEST_ROOT, target)
    functional = target / "requirements" / "functional.yaml"
    functional.write_text(
        functional.read_text(encoding="utf-8").replace(
            "capabilities: [C03, C20]",
            "capabilities: [C03, C99]",
        ),
        encoding="utf-8",
    )

    def fail_if_dag_generation_starts(*_: object, **__: object) -> object:
        raise AssertionError("task DAG generation ran before manifest validation passed")

    original = project_manifest._generated_task_specs
    project_manifest._generated_task_specs = fail_if_dag_generation_starts  # type: ignore[method-assign]

    try:
        try:
            ExecutionPlannerService().plan(target, environment=_environment())
        except ValueError as exc:
            assert "project manifest validation failed" in str(exc)
        else:
            raise AssertionError("expected invalid manifest to block planning")
    finally:
        project_manifest._generated_task_specs = original  # type: ignore[method-assign]


def test_acc_001_data_002_fr_004_acc_003_imported_plan_builds_bounded_worker_package(
    tmp_path: Path,
) -> None:
    service = ExecutionPlannerService()
    planner_result = service.plan(MANIFEST_ROOT, environment=_environment(codex_configured=False))
    factory = session_factory()

    with factory() as session:
        import_result = service.import_plan(
            session,
            planner_result,
            require_clean_git=False,
            require_codex_command=False,
            repository_root=tmp_path,
        )
        package_plan = service.build_worker_package(
            session,
            task_id="IMPL-C17-CMP-004",
            execution_id="exec-IMPL-C17-CMP-004-001",
            timeout_seconds=123,
        )
        repeated_package_plan = service.build_worker_package(
            session,
            task_id="IMPL-C17-CMP-004",
            execution_id="exec-IMPL-C17-CMP-004-001",
            timeout_seconds=123,
        )

        assert import_result.status == "IMPORTED"
        assert import_result.tasks_imported == 13
        assert import_result.dependency_edges_imported == 23
        assert import_result.human_gates_bound == 6
        assert session.scalar(select(func.count()).select_from(RuntimePlanImport)) == 1
        assert session.scalar(select(func.count()).select_from(RuntimeTaskPlanBinding)) == 13
        assert session.scalar(select(func.count()).select_from(RuntimeHumanGate)) == 6
        assert session.scalar(select(func.count()).select_from(TaskDependency)) == 23
        assert len(session.scalars(select(Task).where(Task.id.like("IMPL-%"))).all()) == 13
        assert package_plan.package_hash == repeated_package_plan.package_hash
        assert isinstance(package_plan.package, ExecutionPackage)
        assert package_plan.plan_id == planner_result.dry_run.lock.plan_id
        assert package_plan.dry_run_hash == planner_result.dry_run.dry_run_hash
        assert package_plan.package.task_id == "IMPL-C17-CMP-004"
        assert package_plan.package.execution_id == "exec-IMPL-C17-CMP-004-001"
        assert package_plan.package.timeout_seconds == 123
        assert "src/ai_ent/**" in package_plan.package.allowed_paths
        assert ".env" in package_plan.package.prohibited_paths
        assert "Task: IMPL-C17-CMP-004" in package_plan.package.instructions
        assert (
            "Do not verify, commit, push, or schedule another task."
            in package_plan.package.instructions
        )
        assert planner_result.task_ids_for_requirement("ACC-001")
        assert planner_result.task_ids_for_requirement("ACC-003")
        assert planner_result.task_ids_for_requirement("DATA-002")
        assert planner_result.task_ids_for_requirement("FR-004")
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
        assert session.scalar(select(func.count()).select_from(TaskLease)) == 0


def test_acc_003_worker_package_requires_frozen_runtime_import() -> None:
    factory = session_factory()

    with factory() as session:
        try:
            ExecutionPlannerService().build_worker_package(
                session,
                task_id="IMPL-C17-CMP-004",
                execution_id="exec-1",
                project_id=DEFAULT_RUNTIME_PROJECT_ID,
            )
        except ValueError as exc:
            assert "valid frozen plan" in str(exc)
        else:
            raise AssertionError("expected missing frozen runtime import to block package planning")


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


def _copy_manifest(source: Path, target: Path) -> None:
    for path in source.rglob("*.yaml"):
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
