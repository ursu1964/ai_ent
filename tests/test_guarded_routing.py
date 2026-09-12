from __future__ import annotations

import argparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_ent.persistence.models import (
    Execution,
    Project,
    RuntimePlanImport,
    RuntimeTaskPlanBinding,
    Task,
    TaskLease,
)
from ai_ent.post_implementation import PRIOR_RUNTIME_PLAN_ID
from ai_ent.product_runtime_handoff import (
    PRODUCT_PLAN_ID,
    PRODUCT_PLAN_VERSION,
    ProductRuntimePlanImporter,
)
from ai_ent.residual_runtime_handoff import RESIDUAL_PLAN_ID, RESIDUAL_PLAN_VERSION
from ai_ent.runtime_handoff import DEFAULT_RUNTIME_PROJECT_ID
from ai_ent.scheduler.guarded import GuardedRunConfig, validate_guarded_routing
from scripts.bootstrap import _guarded_routing_defaults
from tests.test_product_runtime_handoff import (
    artifacts,
    seed_prior_and_residual_complete,
    session_factory,
)


def _import_product_plan(session: Session) -> None:
    result = ProductRuntimePlanImporter().import_product_plan(
        session,
        artifacts(),
        require_clean_git=False,
    )
    assert result.ok


def _seed_bootstrap_project(session: Session) -> None:
    session.add(Project(id="ai-ent", name="Bootstrap"))
    session.add(
        Task(
            id="TASK-0001",
            project_id="ai-ent",
            title="Bootstrap task",
            status="pending",
            schedulable=True,
            execution_class="implementation",
        )
    )
    session.flush()


def _counts(session: Session) -> tuple[int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(Execution)) or 0,
        session.scalar(select(func.count()).select_from(TaskLease)) or 0,
        session.scalar(select(func.count()).select_from(Task).where(Task.status == "pending")) or 0,
    )


def test_product_routing_accepts_product_project_plan_and_namespace() -> None:
    factory = session_factory()
    with factory() as session:
        seed_prior_and_residual_complete(session)
        _import_product_plan(session)

        result = validate_guarded_routing(
            session,
            GuardedRunConfig(
                project_id=DEFAULT_RUNTIME_PROJECT_ID,
                execution_mode="product",
                expected_project_id=DEFAULT_RUNTIME_PROJECT_ID,
                expected_plan_id=PRODUCT_PLAN_ID,
                expected_plan_version=PRODUCT_PLAN_VERSION,
                task_id_prefix="PRD-TASK-",
            ),
        )

        assert result.ok


def test_product_routing_rejects_bootstrap_project_before_side_effects() -> None:
    factory = session_factory()
    with factory() as session:
        seed_prior_and_residual_complete(session)
        _seed_bootstrap_project(session)
        _import_product_plan(session)
        before = _counts(session)
        statuses = {task.id: task.status for task in session.scalars(select(Task)).all()}

        result = validate_guarded_routing(
            session,
            GuardedRunConfig(
                project_id="ai-ent",
                execution_mode="product",
                expected_project_id=DEFAULT_RUNTIME_PROJECT_ID,
                expected_plan_id=PRODUCT_PLAN_ID,
                expected_plan_version=PRODUCT_PLAN_VERSION,
                task_id_prefix="PRD-TASK-",
            ),
        )

        assert not result.ok
        assert "PROJECT_PLAN_ROUTING_MISMATCH" in result.detail
        assert "requested_project_mismatch" in result.detail
        assert _counts(session) == before
        assert {task.id: task.status for task in session.scalars(select(Task)).all()} == statuses


def test_bootstrap_routing_accepts_bootstrap_project_namespace() -> None:
    factory = session_factory()
    with factory() as session:
        _seed_bootstrap_project(session)

        result = validate_guarded_routing(
            session,
            GuardedRunConfig(
                project_id="ai-ent",
                execution_mode="bootstrap",
                expected_project_id="ai-ent",
                task_id_prefix="TASK-",
            ),
        )

        assert result.ok


def test_runtime_routing_accepts_original_and_residual_plan_namespaces() -> None:
    factory = session_factory()
    with factory() as session:
        seed_prior_and_residual_complete(session)

        original = validate_guarded_routing(
            session,
            GuardedRunConfig(
                project_id=DEFAULT_RUNTIME_PROJECT_ID,
                execution_mode="original",
                expected_project_id=DEFAULT_RUNTIME_PROJECT_ID,
                expected_plan_id=PRIOR_RUNTIME_PLAN_ID,
                expected_plan_version="1",
                task_id_prefix="IMPL-",
            ),
        )
        residual = validate_guarded_routing(
            session,
            GuardedRunConfig(
                project_id=DEFAULT_RUNTIME_PROJECT_ID,
                execution_mode="residual",
                expected_project_id=DEFAULT_RUNTIME_PROJECT_ID,
                expected_plan_id=RESIDUAL_PLAN_ID,
                expected_plan_version=RESIDUAL_PLAN_VERSION,
                task_id_prefix="RES-",
            ),
        )

        assert original.ok
        assert residual.ok


def test_unknown_project_and_wrong_plan_are_rejected() -> None:
    factory = session_factory()
    with factory() as session:
        seed_prior_and_residual_complete(session)

        unknown = validate_guarded_routing(
            session,
            GuardedRunConfig(project_id="missing", execution_mode="product"),
        )
        wrong_plan = validate_guarded_routing(
            session,
            GuardedRunConfig(
                project_id=DEFAULT_RUNTIME_PROJECT_ID,
                execution_mode="product",
                expected_project_id=DEFAULT_RUNTIME_PROJECT_ID,
                expected_plan_id=PRODUCT_PLAN_ID,
                expected_plan_version=PRODUCT_PLAN_VERSION,
            ),
        )

        assert not unknown.ok
        assert "project_missing" in unknown.detail
        assert not wrong_plan.ok
        assert "runtime_plan_binding_missing" in wrong_plan.detail


def test_runtime_routing_rejects_plan_namespace_mismatch() -> None:
    factory = session_factory()
    with factory() as session:
        session.add(Project(id=DEFAULT_RUNTIME_PROJECT_ID, name="Runtime"))
        session.add(
            RuntimePlanImport(
                id="rhi-product-wrong-namespace-v1",
                project_id=DEFAULT_RUNTIME_PROJECT_ID,
                plan_project_id=DEFAULT_RUNTIME_PROJECT_ID,
                plan_id=PRODUCT_PLAN_ID,
                plan_version=PRODUCT_PLAN_VERSION,
                status="imported",
                compiled_project_hash="a" * 64,
                capability_resolution_hash="b" * 64,
                trace_validation_hash="c" * 64,
                implementation_plan_hash="d" * 64,
                feasibility_hash="e" * 64,
                dry_run_hash="f" * 64,
                task_fingerprint_hash="1" * 64,
                dependency_graph_hash="2" * 64,
                importer_version="test",
                task_count=1,
                dependency_count=0,
                human_gate_count=0,
                effective_concurrency=1,
            )
        )
        session.add(
            Task(
                id="TASK-WRONG",
                project_id=DEFAULT_RUNTIME_PROJECT_ID,
                title="Wrong namespace",
                status="pending",
                execution_class="implementation",
                schedulable=True,
                fingerprint="1" * 64,
            )
        )
        session.add(
            RuntimeTaskPlanBinding(
                task_id="TASK-WRONG",
                import_id="rhi-product-wrong-namespace-v1",
                plan_id=PRODUCT_PLAN_ID,
                plan_version=PRODUCT_PLAN_VERSION,
                fingerprint="1" * 64,
                risk_level="HIGH",
                agent_role="AGT-001",
                model_profile="MDL-001",
                executor="codex",
                verification_profile="FULL_REGRESSION_SECURITY",
                feasibility_status="FEASIBLE",
                policy_decision="HUMAN_APPROVED",
                implements_json="{}",
                write_scope_json="{}",
                acceptance_json="{}",
            )
        )
        session.flush()

        result = validate_guarded_routing(
            session,
            GuardedRunConfig(
                project_id=DEFAULT_RUNTIME_PROJECT_ID,
                execution_mode="product",
                expected_project_id=DEFAULT_RUNTIME_PROJECT_ID,
                expected_plan_id=PRODUCT_PLAN_ID,
                expected_plan_version=PRODUCT_PLAN_VERSION,
                task_id_prefix="PRD-TASK-",
            ),
        )

        assert not result.ok
        assert "task_namespace_mismatch" in result.detail
        assert "TASK-WRONG" in result.detail


def test_cli_product_routing_defaults_are_explicit() -> None:
    defaults = _guarded_routing_defaults(
        argparse.Namespace(
            project=DEFAULT_RUNTIME_PROJECT_ID,
            execution_mode="product",
            plan_id=None,
            plan_version=None,
            task_prefix=None,
        )
    )

    assert defaults == {
        "expected_project_id": DEFAULT_RUNTIME_PROJECT_ID,
        "expected_plan_id": PRODUCT_PLAN_ID,
        "expected_plan_version": PRODUCT_PLAN_VERSION,
        "task_id_prefix": "PRD-TASK-",
    }


def test_cli_bootstrap_routing_remains_explicitly_supported() -> None:
    defaults = _guarded_routing_defaults(
        argparse.Namespace(
            project="ai-ent",
            execution_mode="bootstrap",
            plan_id=None,
            plan_version=None,
            task_prefix=None,
        )
    )

    assert defaults == {
        "expected_project_id": "ai-ent",
        "expected_plan_id": None,
        "expected_plan_version": None,
        "task_id_prefix": "TASK-",
    }
