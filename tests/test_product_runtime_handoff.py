from __future__ import annotations

import json
from typing import Any

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.persistence.models import (
    Base,
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
from ai_ent.product_runtime_handoff import (
    PRODUCT_PLAN_ID,
    ProductRuntimeHandoffArtifacts,
    ProductRuntimePlanImporter,
    load_product_runtime_handoff_artifacts,
)
from ai_ent.runtime_handoff import DEFAULT_RUNTIME_PROJECT_ID, build_runtime_manifest_tasks
from ai_ent.scheduler.readiness import TaskReadinessService
from tests.test_residual_runtime_handoff import seed_prior_plan_complete


def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def seed_prior_and_residual_complete(session: Session) -> None:
    seed_prior_plan_complete(session)
    session.add(
        RuntimePlanImport(
            id="rhi-residual-plan-a918c449cfe5-v1",
            project_id=DEFAULT_RUNTIME_PROJECT_ID,
            plan_project_id=DEFAULT_RUNTIME_PROJECT_ID,
            plan_id="RESIDUAL-PLAN-a918c449cfe5",
            plan_version="1",
            status="imported",
            compiled_project_hash="a" * 64,
            capability_resolution_hash="b" * 64,
            trace_validation_hash="c" * 64,
            implementation_plan_hash="d" * 64,
            feasibility_hash="e" * 64,
            dry_run_hash="f" * 64,
            task_fingerprint_hash="3" * 64,
            dependency_graph_hash="4" * 64,
            importer_version="test",
            task_count=7,
            dependency_count=6,
            human_gate_count=2,
            effective_concurrency=1,
        )
    )
    for index in range(7):
        task_id = f"RES-TEST-{index:03d}"
        session.add(
            Task(
                id=task_id,
                project_id=DEFAULT_RUNTIME_PROJECT_ID,
                title=task_id,
                objective=task_id,
                status="passed",
                execution_class="implementation",
                schedulable=True,
                fingerprint=f"{index}" * 64,
            )
        )
        session.add(
            RuntimeTaskPlanBinding(
                task_id=task_id,
                import_id="rhi-residual-plan-a918c449cfe5-v1",
                plan_id="RESIDUAL-PLAN-a918c449cfe5",
                plan_version="1",
                fingerprint=f"{index}" * 64,
                risk_level="LOW",
                agent_role="AGT-001",
                model_profile="MDL-001",
                executor="codex",
                verification_profile="STANDARD_REGRESSION",
                feasibility_status="FEASIBLE",
                policy_decision="AUTO_ALLOWED",
                implements_json="{}",
                write_scope_json="{}",
                acceptance_json="{}",
            )
        )
    BootstrapRunRepository().activate_postgresql_authority(
        session,
        project_id=DEFAULT_RUNTIME_PROJECT_ID,
        run_id="run-test-authority",
    )


def artifacts() -> ProductRuntimeHandoffArtifacts:
    return load_product_runtime_handoff_artifacts()


def test_imports_exact_product_tasks_edges_gates_and_receipt() -> None:
    factory = session_factory()
    plan_artifacts = artifacts()

    with factory() as session:
        seed_prior_and_residual_complete(session)
        result = ProductRuntimePlanImporter().import_product_plan(
            session,
            plan_artifacts,
            require_clean_git=False,
        )
        session.commit()

        assert result.status == "IMPORTED"
        assert result.product_plan_id == PRODUCT_PLAN_ID
        assert result.tasks_imported == 25
        assert result.dependency_edges_imported == 51
        assert result.human_gates_imported == 8
        assert result.gates_approved == 0
        assert result.product_executions == 0
        assert result.active_leases == 0
        assert result.prior_plan_preservation["original_plan_complete"]
        assert result.prior_plan_preservation["residual_plan_complete"]
        receipt = session.get(RuntimePlanImport, result.import_id)
        assert receipt is not None
        assert receipt.plan_id == PRODUCT_PLAN_ID
        assert receipt.task_count == 25
        assert session.scalar(select(func.count()).select_from(Task).where(Task.id.like("PRD-TASK-%"))) == 25
        assert session.scalar(select(func.count()).select_from(TaskDependency).where(TaskDependency.task_id.like("PRD-TASK-%"))) == 51
        assert session.scalar(select(func.count()).select_from(RuntimeHumanGate).where(RuntimeHumanGate.id.like("GATE-PRD-%"))) == 8


def test_duplicate_product_import_is_idempotent() -> None:
    factory = session_factory()
    plan_artifacts = artifacts()

    with factory() as session:
        seed_prior_and_residual_complete(session)
        first = ProductRuntimePlanImporter().import_product_plan(session, plan_artifacts, require_clean_git=False)
        second = ProductRuntimePlanImporter().import_product_plan(session, plan_artifacts, require_clean_git=False)

        assert first.status == "IMPORTED"
        assert second.status == "ALREADY_IMPORTED"
        assert second.idempotent_rerun_result == "ALREADY_IMPORTED / IN_SYNC"
        assert session.scalar(select(func.count()).select_from(RuntimePlanImport).where(RuntimePlanImport.plan_id == PRODUCT_PLAN_ID)) == 1
        assert session.scalar(select(func.count()).select_from(Task).where(Task.id.like("PRD-TASK-%"))) == 25


def test_product_import_mismatch_conflicts_without_duplicates() -> None:
    factory = session_factory()
    plan_artifacts = artifacts()

    with factory() as session:
        seed_prior_and_residual_complete(session)
        assert ProductRuntimePlanImporter().import_product_plan(session, plan_artifacts, require_clean_git=False).ok
        task = session.get(Task, "PRD-TASK-001")
        assert task is not None
        task.fingerprint = "changed"
        session.flush()

        conflict = ProductRuntimePlanImporter().import_product_plan(session, plan_artifacts, require_clean_git=False)

        assert conflict.status == "CONFLICT"
        assert "product task mismatch:PRD-TASK-001" in conflict.blockers
        assert session.scalar(select(func.count()).select_from(Task).where(Task.id.like("PRD-TASK-%"))) == 25


def test_product_import_blocks_without_prior_plan_preservation() -> None:
    factory = session_factory()
    plan_artifacts = artifacts()

    with factory() as session:
        session.add(Project(id=DEFAULT_RUNTIME_PROJECT_ID, name="Runtime"))
        session.flush()
        result = ProductRuntimePlanImporter().import_product_plan(session, plan_artifacts, require_clean_git=False)

        assert result.status == "BLOCKED"
        assert "prior PLAN-1a75a2e3c5a7/v1 is not complete" in result.blockers
        assert session.scalar(select(func.count()).select_from(Task).where(Task.id.like("PRD-TASK-%"))) == 0


def test_decision_and_gate_readiness_blocking() -> None:
    factory = session_factory()
    plan_artifacts = artifacts()

    with factory() as session:
        seed_prior_and_residual_complete(session)
        result = ProductRuntimePlanImporter().import_product_plan(session, plan_artifacts, require_clean_git=False)

        assert result.ready_tasks == ("PRD-TASK-001", "PRD-TASK-005")
        for task_id in ("PRD-TASK-019", "PRD-TASK-020", "PRD-TASK-023", "PRD-TASK-025"):
            for dependency in session.scalars(select(TaskDependency).where(TaskDependency.task_id == task_id)).all():
                dependency_task = session.get(Task, dependency.depends_on_task_id)
                assert dependency_task is not None
                dependency_task.status = "passed"
        for task_id in ("PRD-TASK-019", "PRD-TASK-020", "PRD-TASK-023", "PRD-TASK-025"):
            task = session.get(Task, task_id)
            assert task is not None
            task.status = "pending"
        session.flush()

        readiness = TaskReadinessService()
        assert readiness.evaluate_task(session, "PRD-TASK-019").status == "NOT_SCHEDULABLE"
        session.get(RuntimeHumanGate, "GATE-PRD-LAN-ACCESS").status = "approved"  # type: ignore[union-attr]
        session.get(RuntimeHumanGate, "GATE-PRD-CONFIG-SECRETS").status = "approved"  # type: ignore[union-attr]
        session.flush()
        assert readiness.evaluate_task(session, "PRD-TASK-019").status == "DECISION_BLOCKED"
        assert readiness.evaluate_task(session, "PRD-TASK-020").status == "DECISION_BLOCKED"
        session.get(Task, "PRD-TASK-019").status = "passed"  # type: ignore[union-attr]
        session.get(Task, "PRD-TASK-020").status = "passed"  # type: ignore[union-attr]
        session.flush()
        assert readiness.evaluate_task(session, "PRD-TASK-023").status == "DECISION_BLOCKED"
        assert readiness.evaluate_task(session, "PRD-TASK-025").status == "DECISION_BLOCKED"


def test_task_profiles_and_decision_bindings_preserved() -> None:
    factory = session_factory()
    plan_artifacts = artifacts()

    with factory() as session:
        seed_prior_and_residual_complete(session)
        ProductRuntimePlanImporter().import_product_plan(session, plan_artifacts, require_clean_git=False)
        binding = session.get(RuntimeTaskPlanBinding, "PRD-TASK-019")
        assert binding is not None
        acceptance = json.loads(binding.acceptance_json)
        implements = json.loads(binding.implements_json)

        assert binding.model_profile.startswith("MDL-001:Codex executor model:")
        assert binding.verification_profile
        assert acceptance["required_decisions"] == ["DECISION_REQUIRED:PRD-DEC-002"]
        assert implements["no_e2e_project_hardcoding"] is True


def test_product_manifest_task_package_scope_and_prompt() -> None:
    factory = session_factory()
    plan_artifacts = artifacts()

    with factory() as session:
        seed_prior_and_residual_complete(session)
        ProductRuntimePlanImporter().import_product_plan(session, plan_artifacts, require_clean_git=False)
        manifest_tasks = build_runtime_manifest_tasks(session, DEFAULT_RUNTIME_PROJECT_ID)

        task = manifest_tasks["PRD-TASK-019"]
        assert "src/ai_ent_product_api/**" in task.allowed_paths
        assert ".build/**" in task.prohibited_paths
        assert "Acceptance criteria:" in (task.objective or "")
        assert "Decision dependencies:" in (task.objective or "")
        assert "DECISION_REQUIRED:PRD-DEC-002" in (task.objective or "")


def test_guarded_dry_run_is_side_effect_free() -> None:
    factory = session_factory()
    plan_artifacts = artifacts()

    with factory() as session:
        seed_prior_and_residual_complete(session)
        result = ProductRuntimePlanImporter().import_product_plan(
            session,
            plan_artifacts,
            require_clean_git=False,
            include_guarded_dry_run=True,
        )

        assert result.guarded_dry_run_result is not None
        assert result.guarded_dry_run_result["side_effect_free"] is True
        assert result.guarded_dry_run_result["created_executions"] == 0
        assert result.guarded_dry_run_result["created_leases"] == 0
        assert session.scalar(select(func.count()).select_from(Execution).where(Execution.task_id.like("PRD-TASK-%"))) == 0
        assert session.scalar(select(func.count()).select_from(TaskLease).where(TaskLease.task_id.like("PRD-TASK-%"))) == 0


def test_hash_or_gate_mismatch_blocks_import() -> None:
    factory = session_factory()
    plan_artifacts = artifacts()
    altered = ProductRuntimeHandoffArtifacts(
        product_plan=plan_artifacts.product_plan,
        import_preview=plan_artifacts.import_preview,
        lock={**plan_artifacts.lock, "product_dry_run_hash": "0" * 64},
        ppg_acceptance=plan_artifacts.ppg_acceptance,
        pdf=plan_artifacts.pdf,
    )

    with factory() as session:
        seed_prior_and_residual_complete(session)
        result = ProductRuntimePlanImporter().import_product_plan(session, altered, require_clean_git=False)

        assert result.status == "BLOCKED"
        assert "product lock hash mismatch:product_dry_run_hash" in result.blockers


def test_partial_import_failure_rolls_back() -> None:
    factory = session_factory()
    plan_artifacts = artifacts()
    broken_preview = tuple(
        {**item, "fingerprint": plan_artifacts.import_preview[0]["fingerprint"]}
        if item["task_id"] == "PRD-TASK-002"
        else item
        for item in plan_artifacts.import_preview
    )
    broken_fingerprints = dict(plan_artifacts.lock["task_fingerprints"])
    broken_fingerprints["PRD-TASK-002"] = plan_artifacts.import_preview[0]["fingerprint"]
    altered = ProductRuntimeHandoffArtifacts(
        product_plan=plan_artifacts.product_plan,
        import_preview=broken_preview,
        lock={**plan_artifacts.lock, "task_fingerprints": broken_fingerprints},
        ppg_acceptance=plan_artifacts.ppg_acceptance,
        pdf=plan_artifacts.pdf,
    )

    with factory() as session:
        seed_prior_and_residual_complete(session)
        try:
            ProductRuntimePlanImporter().import_product_plan(session, altered, require_clean_git=False)
        except RuntimeError:
            pass

        assert session.scalar(select(func.count()).select_from(Task).where(Task.id.like("PRD-TASK-%"))) == 0
        assert (
            session.scalar(
                select(func.count()).select_from(RuntimePlanImport).where(RuntimePlanImport.plan_id == PRODUCT_PLAN_ID)
            )
            == 0
        )
