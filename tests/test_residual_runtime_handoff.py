from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.persistence.models import (
    Base,
    BootstrapRun,
    Execution,
    Project,
    RuntimeHumanGate,
    RuntimePlanImport,
    RuntimeTaskPlanBinding,
    Task,
    TaskDependency,
    TaskLease,
)
from ai_ent.persistence.repositories.bootstrap import BootstrapRunRepository
from ai_ent.residual_runtime_handoff import (
    RESIDUAL_PLAN_ID,
    ResidualRuntimePlanImporter,
    load_residual_runtime_handoff_artifacts,
)
from ai_ent.runtime_handoff import DEFAULT_RUNTIME_PROJECT_ID, build_runtime_manifest_tasks
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


def seed_prior_plan_complete(session: Session, project_id: str = DEFAULT_RUNTIME_PROJECT_ID) -> None:
    session.add(Project(id=project_id, name=f"Runtime {project_id}"))
    session.flush()
    session.add(RuntimePlanImport(
        id="rhi-plan-1a75a2e3c5a7-v1",
        project_id=project_id,
        plan_project_id="PRJ-AI-ENT",
        plan_id="PLAN-1a75a2e3c5a7",
        plan_version="1",
        status="imported",
        imported_at=datetime.now(UTC),
        compiled_project_hash="a" * 64,
        capability_resolution_hash="b" * 64,
        trace_validation_hash="c" * 64,
        implementation_plan_hash="d" * 64,
        feasibility_hash="e" * 64,
        dry_run_hash="f" * 64,
        task_fingerprint_hash="1" * 64,
        dependency_graph_hash="2" * 64,
        importer_version="test",
        task_count=13,
        dependency_count=23,
        human_gate_count=6,
        effective_concurrency=1,
    ))
    for index in range(13):
        task_id = f"IMPL-TEST-{index:03d}"
        session.add(Task(
            id=task_id,
            project_id=project_id,
            title=task_id,
            objective=task_id,
            status="passed",
            execution_class="implementation",
            schedulable=True,
            fingerprint=_hash(task_id),
        ))
        session.add(RuntimeTaskPlanBinding(
            task_id=task_id,
            import_id="rhi-plan-1a75a2e3c5a7-v1",
            plan_id="PLAN-1a75a2e3c5a7",
            plan_version="1",
            fingerprint=_hash(task_id),
            risk_level="LOW",
            agent_role="AGT-001",
            model_profile="CODING_STANDARD",
            executor="codex",
            verification_profile="STANDARD_REGRESSION",
            feasibility_status="FEASIBLE",
            policy_decision="AUTO_ALLOWED",
            implements_json="{}",
            write_scope_json='{"allowed":["src/ai_ent/**"],"prohibited":[".env"]}',
            acceptance_json='["passed"]',
        ))
    session.add(BootstrapRun(
        id="run-test-authority",
        project_id=project_id,
        status="running",
        manifest_ref="test",
        current_stage="test",
        started_at=datetime.now(UTC),
    ))
    session.flush()
    BootstrapRunRepository().activate_postgresql_authority(session, project_id=project_id, run_id="run-test-authority")


def test_imports_exact_residual_tasks_edges_and_gates() -> None:
    factory = session_factory()
    artifacts = load_residual_runtime_handoff_artifacts()

    with factory() as session:
        seed_prior_plan_complete(session)
        result = ResidualRuntimePlanImporter().import_residual_plan(
            session,
            artifacts,
            require_clean_git=False,
            require_codex_command=False,
        )
        session.commit()

        assert result.status == "IMPORTED"
        assert result.residual_plan_id == RESIDUAL_PLAN_ID
        assert result.tasks_imported == 7
        assert result.dependency_edges_imported == 6
        assert result.human_gates_bound == 2
        assert result.executions_after_import == 0
        assert result.active_leases_after_import == 0
        assert result.prior_plan_preserved
        assert session.scalar(select(func.count()).select_from(RuntimePlanImport)) == 2
        assert len(session.scalars(select(Task).where(Task.id.like("RES-%"))).all()) == 7
        assert session.scalar(select(func.count()).select_from(TaskDependency).where(TaskDependency.task_id.like("RES-%"))) == 6
        assert session.scalar(select(func.count()).select_from(RuntimeHumanGate).where(RuntimeHumanGate.id.like("GATE-RES-%"))) == 2


def test_duplicate_residual_import_is_idempotent() -> None:
    factory = session_factory()
    artifacts = load_residual_runtime_handoff_artifacts()

    with factory() as session:
        seed_prior_plan_complete(session)
        first = ResidualRuntimePlanImporter().import_residual_plan(
            session,
            artifacts,
            require_clean_git=False,
            require_codex_command=False,
        )
        second = ResidualRuntimePlanImporter().import_residual_plan(
            session,
            artifacts,
            require_clean_git=False,
            require_codex_command=False,
        )

        assert first.status == "IMPORTED"
        assert second.status == "ALREADY_IMPORTED"
        assert len(session.scalars(select(Task).where(Task.id.like("RES-%"))).all()) == 7
        assert session.scalar(select(func.count()).select_from(RuntimeHumanGate).where(RuntimeHumanGate.id.like("GATE-RES-%"))) == 2


def test_residual_mismatch_causes_conflict() -> None:
    factory = session_factory()
    artifacts = load_residual_runtime_handoff_artifacts()

    with factory() as session:
        seed_prior_plan_complete(session)
        result = ResidualRuntimePlanImporter().import_residual_plan(
            session,
            artifacts,
            require_clean_git=False,
            require_codex_command=False,
        )
        assert result.ok
        task = session.get(Task, "RES-C05-EVIDENCE")
        assert task is not None
        task.fingerprint = "changed"
        session.flush()

        conflict = ResidualRuntimePlanImporter().import_residual_plan(
            session,
            artifacts,
            require_clean_git=False,
            require_codex_command=False,
        )

        assert conflict.status == "CONFLICT"
        assert "residual task mismatch:RES-C05-EVIDENCE" in conflict.blockers


def test_hash_mismatch_blocks_import() -> None:
    factory = session_factory()
    artifacts = load_residual_runtime_handoff_artifacts()
    altered = replace(artifacts, dry_run=replace(artifacts.dry_run, residual_plan_hash="0" * 64))

    with factory() as session:
        seed_prior_plan_complete(session)
        result = ResidualRuntimePlanImporter().import_residual_plan(
            session,
            altered,
            require_clean_git=False,
            require_codex_command=False,
        )

        assert result.status == "BLOCKED"
        assert "RPG-001 hash mismatch:residual_plan_hash" in result.blockers
        assert len(session.scalars(select(Task).where(Task.id.like("RES-%"))).all()) == 0


def test_gated_c08_tasks_remain_blocked_and_evidence_type_is_preserved() -> None:
    factory = session_factory()
    artifacts = load_residual_runtime_handoff_artifacts()

    with factory() as session:
        seed_prior_plan_complete(session)
        result = ResidualRuntimePlanImporter().import_residual_plan(
            session,
            artifacts,
            require_clean_git=False,
            require_codex_command=False,
        )
        assert result.runtime_ready_tasks == ("RES-C05-EVIDENCE",)
        assert result.gated_not_ready_tasks == ("RES-C08-CONTRACT", "RES-C08-SERVICE")

        session.get(Task, "RES-C05-EVIDENCE").status = "passed"  # type: ignore[union-attr]
        session.get(Task, "RES-C06-EVIDENCE").status = "passed"  # type: ignore[union-attr]
        session.get(Task, "RES-C07-EVIDENCE").status = "passed"  # type: ignore[union-attr]
        session.flush()
        decision = TaskReadinessService().evaluate_task(session, "RES-C08-CONTRACT")
        binding = session.get(RuntimeTaskPlanBinding, "RES-C05-EVIDENCE")
        assert binding is not None

        assert decision.status == "NOT_SCHEDULABLE"
        assert decision.reasons == ("pending_human_gate",)
        assert json.loads(binding.implements_json)["task_type"] == "EVIDENCE_CLOSURE_TASK"


def test_runtime_manifest_includes_residual_tasks_for_future_execution() -> None:
    factory = session_factory()
    artifacts = load_residual_runtime_handoff_artifacts()

    with factory() as session:
        seed_prior_plan_complete(session)
        ResidualRuntimePlanImporter().import_residual_plan(
            session,
            artifacts,
            require_clean_git=False,
            require_codex_command=False,
        )
        manifest_tasks = build_runtime_manifest_tasks(session, DEFAULT_RUNTIME_PROJECT_ID)

    assert "RES-C05-EVIDENCE" in manifest_tasks
    assert manifest_tasks["RES-C05-EVIDENCE"].execution_class == "implementation"
    assert "Acceptance criteria:" in (manifest_tasks["RES-C05-EVIDENCE"].objective or "")


def test_guarded_dry_run_has_no_execution_or_lease_side_effects() -> None:
    factory = session_factory()
    artifacts = load_residual_runtime_handoff_artifacts()

    with factory() as session:
        seed_prior_plan_complete(session)
        result = ResidualRuntimePlanImporter().import_residual_plan(
            session,
            artifacts,
            require_clean_git=False,
            require_codex_command=False,
            include_guarded_dry_run=True,
        )

        assert result.guarded_dry_run is not None
        assert result.guarded_dry_run["dry_run"] is True
        assert result.guarded_dry_run["likely_next_task_id"] == "RES-C05-EVIDENCE"
        assert session.scalar(select(func.count()).select_from(Execution).where(Execution.task_id.like("RES-%"))) == 0
        assert session.scalar(select(func.count()).select_from(TaskLease).where(TaskLease.task_id.like("RES-%"))) == 0


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
