from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

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
from ai_ent.runtime_handoff import (
    DEFAULT_RUNTIME_PROJECT_ID,
    RuntimeHandoffArtifacts,
    RuntimePlanImporter,
    build_runtime_manifest_tasks,
    external_project_runtime_handoff_artifacts,
    load_runtime_handoff_artifacts,
)
from ai_ent.scheduler.readiness import TaskReadinessService
from tests.test_external_project import frozen_plan_project


def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def artifacts(tmp_path: Path) -> RuntimeHandoffArtifacts:
    return load_runtime_handoff_artifacts(output_dir=tmp_path / "compiled", accepted_artifact_path=None)


def import_plan(session: Session, plan_artifacts: RuntimeHandoffArtifacts) -> None:
    result = RuntimePlanImporter().import_frozen_plan(
        session,
        plan_artifacts,
        require_clean_git=False,
        require_codex_command=False,
    )
    assert result.ok


def test_imports_exact_frozen_plan_tasks_edges_and_gates(tmp_path: Path) -> None:
    factory = session_factory()
    plan_artifacts = artifacts(tmp_path)

    with factory() as session:
        result = RuntimePlanImporter().import_frozen_plan(
            session,
            plan_artifacts,
            require_clean_git=False,
            require_codex_command=False,
        )
        session.commit()

        assert result.status == "IMPORTED"
        assert result.project_id == DEFAULT_RUNTIME_PROJECT_ID
        assert result.tasks_imported == 13
        assert result.dependency_edges_imported == 23
        assert result.human_gates_bound == 6
        assert result.executions_after_import == 0
        assert result.active_leases_after_import == 0
        assert session.scalar(select(func.count()).select_from(RuntimePlanImport)) == 1
        assert len(session.scalars(select(Task).where(Task.id.like("IMPL-%"))).all()) == 13
        assert session.scalar(select(func.count()).select_from(TaskDependency)) == 23
        assert session.scalar(select(func.count()).select_from(RuntimeHumanGate)) == 6


def test_duplicate_import_is_idempotent(tmp_path: Path) -> None:
    factory = session_factory()
    plan_artifacts = artifacts(tmp_path)

    with factory() as session:
        first = RuntimePlanImporter().import_frozen_plan(
            session,
            plan_artifacts,
            require_clean_git=False,
            require_codex_command=False,
        )
        second = RuntimePlanImporter().import_frozen_plan(
            session,
            plan_artifacts,
            require_clean_git=False,
            require_codex_command=False,
        )
        session.commit()

        assert first.status == "IMPORTED"
        assert second.status == "ALREADY_IMPORTED"
        assert session.scalar(select(func.count()).select_from(RuntimePlanImport)) == 1
        assert len(session.scalars(select(RuntimeTaskPlanBinding)).all()) == 13


def test_material_task_mismatch_causes_conflict(tmp_path: Path) -> None:
    factory = session_factory()
    plan_artifacts = artifacts(tmp_path)

    with factory() as session:
        import_plan(session, plan_artifacts)
        task = session.get(Task, "IMPL-C01-CMP-001")
        assert task is not None
        task.fingerprint = "changed"
        session.flush()

        result = RuntimePlanImporter().import_frozen_plan(
            session,
            plan_artifacts,
            require_clean_git=False,
            require_codex_command=False,
        )

        assert result.status == "CONFLICT"
        assert result.blockers == ("task mismatch:IMPL-C01-CMP-001",)


def test_material_hash_mismatch_blocks_import(tmp_path: Path) -> None:
    factory = session_factory()
    plan_artifacts = artifacts(tmp_path)
    altered_dry_run = replace(
        plan_artifacts.dry_run,
        lock=replace(plan_artifacts.dry_run.lock, implementation_plan_hash="0" * 64),
    )
    altered = RuntimeHandoffArtifacts(
        plan=plan_artifacts.plan,
        feasibility=plan_artifacts.feasibility,
        dry_run=altered_dry_run,
    )

    with factory() as session:
        result = RuntimePlanImporter().import_frozen_plan(
            session,
            altered,
            require_clean_git=False,
            require_codex_command=False,
        )

        assert result.status == "BLOCKED"
        assert "implementation plan hash mismatch" in result.blockers
        assert len(session.scalars(select(Task).where(Task.id.like("IMPL-%"))).all()) == 0


def test_gate_bindings_are_pending_and_block_readiness(tmp_path: Path) -> None:
    factory = session_factory()
    plan_artifacts = artifacts(tmp_path)

    with factory() as session:
        import_plan(session, plan_artifacts)
        gate = session.get(RuntimeHumanGate, "GATE-IMPL-C14-CMP-001")
        assert gate is not None
        assert gate.status == "pending"
        session.get(Task, "IMPL-C01-CMP-001").status = "passed"  # type: ignore[union-attr]
        session.get(Task, "IMPL-C02-CONTRACT").status = "passed"  # type: ignore[union-attr]
        session.flush()

        decision = TaskReadinessService().evaluate_task(session, "IMPL-C14-CMP-001")

        assert decision.status == "NOT_SCHEDULABLE"
        assert decision.reasons == ("pending_human_gate",)


def test_runtime_manifest_tasks_preserve_scope_fingerprint_and_verification(tmp_path: Path) -> None:
    factory = session_factory()
    plan_artifacts = artifacts(tmp_path)

    with factory() as session:
        import_plan(session, plan_artifacts)
        manifest_tasks = build_runtime_manifest_tasks(session, DEFAULT_RUNTIME_PROJECT_ID)

    task = manifest_tasks["IMPL-C01-CMP-001"]
    assert task.id == "IMPL-C01-CMP-001"
    assert task.executor == "codex"
    assert task.execution_class == "implementation"
    assert "src/ai_ent/**" in task.allowed_paths
    assert ".env" in task.prohibited_paths
    assert any("pytest" in command for command in task.verification.commands)
    assert "Acceptance criteria:" in (task.objective or "")


def test_runtime_status_has_no_side_effects(tmp_path: Path) -> None:
    factory = session_factory()
    plan_artifacts = artifacts(tmp_path)

    with factory() as session:
        import_plan(session, plan_artifacts)
        status = RuntimePlanImporter().status(session, project_id=DEFAULT_RUNTIME_PROJECT_ID)

        assert status.imported_tasks == 13
        assert status.executions == 0
        assert status.active_leases == 0
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
        assert session.scalar(select(func.count()).select_from(TaskLease)) == 0


def test_external_frozen_plan_imports_generic_task_ids_and_pending_gate(
    tmp_path: Path,
) -> None:
    factory = session_factory()
    frozen = frozen_plan_project(tmp_path)

    with factory() as session:
        result = RuntimePlanImporter().import_external_frozen_plan(
            session,
            frozen,
            require_clean_git=False,
            require_codex_command=False,
            repository_root=tmp_path,
        )
        session.commit()

        status = RuntimePlanImporter().status(session, project_id=frozen.project.project_id)
        manifest_tasks = build_runtime_manifest_tasks(session, frozen.project.project_id)
        gate = session.get(RuntimeHumanGate, "GATE-EXT-TASK-002")

        assert result.status == "IMPORTED"
        assert result.project_id == frozen.project.project_id
        assert result.tasks_imported == 2
        assert result.dependency_edges_imported == 1
        assert result.human_gates_bound == 1
        assert result.runtime_ready_tasks == ("EXT-TASK-001",)
        assert result.gated_not_ready_tasks == ("EXT-TASK-002",)
        assert status.imported_tasks == 2
        assert status.pending_human_gates == 1
        assert set(manifest_tasks) == {"EXT-TASK-001", "EXT-TASK-002"}
        assert manifest_tasks["EXT-TASK-002"].depends_on == ("EXT-TASK-001",)
        assert any(
            "pytest" in command
            for command in manifest_tasks["EXT-TASK-001"].verification.commands
        )
        assert gate is not None
        assert gate.status == "pending"


def test_external_runtime_handoff_artifacts_are_deterministic(
    tmp_path: Path,
) -> None:
    first = external_project_runtime_handoff_artifacts(frozen_plan_project(tmp_path))
    second = external_project_runtime_handoff_artifacts(frozen_plan_project(tmp_path))

    assert first.plan.as_dict() == second.plan.as_dict()
    assert first.feasibility.as_dict() == second.feasibility.as_dict()
    assert first.dry_run.as_dict() == second.dry_run.as_dict()
    assert first.dry_run.lock.plan_id == "PLAN-EXT-RUNTIME"
    assert first.dry_run.lock.project_id == "EXT-RUNTIME-APP"


def test_external_frozen_plan_import_blocks_without_leaking_secret_remote(
    tmp_path: Path,
) -> None:
    factory = session_factory()
    frozen = frozen_plan_project(
        tmp_path,
        remote_url="https://git.example/runtime-app.git?token=SUPERSECRET",
    )

    with factory() as session:
        result = RuntimePlanImporter().import_external_frozen_plan(
            session,
            frozen,
            require_clean_git=False,
            require_codex_command=False,
            repository_root=tmp_path,
        )

    rendered = "\n".join(result.blockers)
    assert result.status == "BLOCKED"
    assert "https://git.example/runtime-app.git?<redacted>" in rendered
    assert "SUPERSECRET" not in rendered
