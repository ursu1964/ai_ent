from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.persistence.models import (
    Base,
    Checkpoint,
    Execution,
    RuntimePlanImport,
    RuntimeTaskPlanBinding,
    Task,
    TaskLease,
)
from ai_ent.persistence.repositories import ProjectRepository, TaskRepository
from ai_ent.scheduler.claiming import TaskClaimingService
from ai_ent.scheduler.hardening_baseline import HardeningBaselineService
from ai_ent.scheduler.iteration import ExecutionPackageFactory
from ai_ent.scheduler.readiness import TaskReadinessService

PLAN_ID = "PUBLIC-CLOUD-HARDENING-PLAN-877cc1468dc3"
PLAN_VERSION = "1"
BASELINE_COMMIT = "e504ef38c9d47b8bcfad61ec4550dfafd49eec97"
BASELINE_TREE = "2897a5bd38a30cc34acd9bc655c5c17c6f7f6e59"


def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def seed_import(session: Session) -> RuntimePlanImport:
    ProjectRepository().create(session, project_id="PRJ-AI-ENT", name="AI Enterprise")
    plan_import = RuntimePlanImport(
        id="pchi-public-cloud-hardening-plan-877cc1468dc3-v1",
        project_id="PRJ-AI-ENT",
        plan_project_id="PRJ-AI-ENT",
        plan_id=PLAN_ID,
        plan_version=PLAN_VERSION,
        status="imported",
        compiled_project_hash="0" * 64,
        capability_resolution_hash="1" * 64,
        trace_validation_hash="2" * 64,
        implementation_plan_hash="877cc1468dc3a7b6efd93fae98d0d381553ac0bfecb7ea68613009fb745e9c2a",
        feasibility_hash="3" * 64,
        dry_run_hash="4" * 64,
        task_fingerprint_hash="5" * 64,
        dependency_graph_hash="6" * 64,
        importer_version="test",
        task_count=3,
        dependency_count=2,
        human_gate_count=0,
        effective_concurrency=1,
    )
    session.add(plan_import)
    session.flush()
    return plan_import


def seed_pch_task(
    session: Session,
    plan_import: RuntimePlanImport,
    task_id: str,
    *,
    status: str = "pending",
) -> Task:
    task = TaskRepository().create(
        session,
        task_id=task_id,
        project_id="PRJ-AI-ENT",
        title=f"Task {task_id}",
        objective=f"{task_id}: objective",
        status=status,  # type: ignore[arg-type]
        fingerprint=f"fingerprint-{task_id}",
    )
    session.add(
        RuntimeTaskPlanBinding(
            task_id=task.id,
            import_id=plan_import.id,
            plan_id=plan_import.plan_id,
            plan_version=plan_import.plan_version,
            fingerprint=task.fingerprint or "",
            risk_level="MEDIUM",
            agent_role="AGT-PCH",
            model_profile="MDL-PCH",
            executor="governed_scheduler_isolated_worktree",
            verification_profile="FULL_REGRESSION",
            feasibility_status="FEASIBLE",
            policy_decision="GUARDED_ALLOWED",
            implements_json=json.dumps({"requirements": ["PCH-RELEASE-001"]}),
            write_scope_json=json.dumps({"allowed": ["src/ai_ent/**"], "prohibited": [".env"]}),
            acceptance_json=json.dumps(
                {
                    "baseline_commit": BASELINE_COMMIT,
                    "baseline_tree": BASELINE_TREE,
                    "cumulative_baseline_policy": {
                        "owner_task": "PCH-TASK-001",
                        "on_violation": "STOP_BEFORE_CLAIM",
                        "rule": "downstream task base tree must contain all passed dependency trees before claim",
                    },
                    "acceptance_criteria": [
                        "Verified commit is cumulatively integrated into the authoritative hardening baseline before dependent tasks execute"
                    ],
                },
                sort_keys=True,
            ),
        )
    )
    session.flush()
    return task


def seed_pch_graph(session: Session) -> None:
    plan_import = seed_import(session)
    seed_pch_task(session, plan_import, "PCH-TASK-001", status="passed")
    seed_pch_task(session, plan_import, "PCH-TASK-002")
    seed_pch_task(session, plan_import, "PCH-TASK-013")
    tasks = TaskRepository()
    tasks.add_dependency(session, task_id="PCH-TASK-002", depends_on_task_id="PCH-TASK-001")
    tasks.add_dependency(session, task_id="PCH-TASK-013", depends_on_task_id="PCH-TASK-001")


def test_passed_dependency_without_baseline_representation_blocks_readiness_and_claim() -> None:
    factory = session_factory()
    readiness = TaskReadinessService(task_id_prefix="PCH-TASK-")
    claiming = TaskClaimingService()

    with factory() as session:
        seed_pch_graph(session)

        task_002 = readiness.evaluate_task(session, "PCH-TASK-002")
        task_013 = readiness.evaluate_task(session, "PCH-TASK-013")
        ready = readiness.list_ready_tasks(session, project_id="PRJ-AI-ENT")
        claim = claiming.claim_task(
            session,
            task_id="PCH-TASK-002",
            owner_id="worker",
            lease_duration=timedelta(minutes=5),
        )

        assert task_002.status == "BASELINE_INTEGRATION_REQUIRED"
        assert task_002.blocking_dependencies == ("PCH-TASK-001",)
        assert task_002.reasons == ("baseline_integration_required:PCH-TASK-001",)
        assert task_013.status == "BASELINE_INTEGRATION_REQUIRED"
        assert ready == []
        assert claim.status == "NOT_CLAIMABLE"
        assert claim.reason == "baseline_integration_required:PCH-TASK-001"
        assert session.scalars(select(Execution)).all() == []
        assert session.scalars(select(TaskLease)).all() == []


def test_baseline_advancement_unlocks_downstream_readiness_with_compare_and_swap() -> None:
    factory = session_factory()
    baseline = HardeningBaselineService()
    readiness = TaskReadinessService(task_id_prefix="PCH-TASK-")
    claiming = TaskClaimingService()

    with factory() as session:
        seed_pch_graph(session)

        advanced = baseline.advance(
            session,
            plan_id=PLAN_ID,
            plan_version=PLAN_VERSION,
            task_id="PCH-TASK-001",
            task_commit="1" * 40,
            task_tree="2" * 40,
            resulting_baseline_commit="3" * 40,
            resulting_baseline_tree="4" * 40,
            expected_generation=0,
        )
        stale = baseline.advance(
            session,
            plan_id=PLAN_ID,
            plan_version=PLAN_VERSION,
            task_id="PCH-TASK-013",
            task_commit="5" * 40,
            task_tree="6" * 40,
            resulting_baseline_commit="7" * 40,
            resulting_baseline_tree="8" * 40,
            expected_generation=0,
        )

        assert advanced.status == "ADVANCED"
        assert advanced.state is not None
        assert advanced.state.generation == 1
        assert advanced.state.integrated_task_ids == ("PCH-TASK-001",)
        assert stale.status == "STALE_ADVANCEMENT"
        assert stale.state is not None
        assert stale.state.generation == 1
        assert readiness.evaluate_task(session, "PCH-TASK-002").status == "READY"
        assert readiness.evaluate_task(session, "PCH-TASK-013").status == "READY"
        claim = claiming.claim_task(
            session,
            task_id="PCH-TASK-002",
            owner_id="worker",
            lease_duration=timedelta(minutes=5),
        )
        assert claim.status == "CLAIMED"
        assert session.scalar(select(Checkpoint).where(Checkpoint.checkpoint_type == "runtime")) is not None


def test_runtime_package_is_bound_to_authoritative_baseline_generation() -> None:
    factory = session_factory()
    package_factory = ExecutionPackageFactory(repository_path=Path("/repo"), timeout_seconds=30)

    with factory() as session:
        plan_import = seed_import(session)
        task = seed_pch_task(session, plan_import, "PCH-TASK-001")

        package = package_factory.build(
            task,
            execution_id="execution-pch-001",
            session=session,
            project_id="PRJ-AI-ENT",
        )

        assert package.baseline_commit == BASELINE_COMMIT
        assert package.baseline_tree == BASELINE_TREE
        assert package.baseline_generation == 0
        assert f"- commit: {BASELINE_COMMIT}" in package.instructions
        assert f"- tree: {BASELINE_TREE}" in package.instructions
        assert "- generation: 0" in package.instructions
