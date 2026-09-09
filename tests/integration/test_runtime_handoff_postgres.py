from __future__ import annotations

import hashlib
import unittest
import uuid
from pathlib import Path

from alembic import command

from ai_ent.persistence.config import DatabaseConfigError, load_database_settings
from ai_ent.persistence.database import Database
from ai_ent.persistence.migrations import build_alembic_config
from ai_ent.persistence.models import Execution, RuntimeHumanGate, TaskLease
from ai_ent.project_manifest import (
    EnvironmentProfile,
    ExecutionBatch,
    FeasibilityEvaluation,
    FeasibilityFacet,
    FeasibilityPolicyProfile,
    GeneratedImplementationTask,
    ImplementationDryRun,
    ImplementationPlan,
    ImplementationPlanLock,
    TaskFeasibility,
    TaskImportPreview,
    canonical_bytes,
)
from ai_ent.runtime_handoff import RuntimeHandoffArtifacts, RuntimePlanImporter
from tests.integration.helpers import clean_test_tables, make_test_project_id, make_test_suffix


def integration_database() -> Database:
    try:
        config = build_alembic_config(Path("alembic.ini"), Path(".env"))
        settings = load_database_settings(Path(".env"))
    except DatabaseConfigError as exc:
        raise unittest.SkipTest(f"runtime handoff integration blocked: {exc}") from exc
    command.upgrade(config, "head")
    return Database(settings)


def test_postgres_runtime_handoff_import_is_isolated_and_dry_run_safe() -> None:
    database = integration_database()
    clean_test_tables(database)
    suffix = make_test_suffix(uuid.uuid4().hex[:8])
    project_id = make_test_project_id(suffix)
    artifacts = _synthetic_artifacts(suffix)

    try:
        with database.session() as session:
            result = RuntimePlanImporter().import_frozen_plan(
                session,
                artifacts,
                runtime_project_id=project_id,
                require_clean_git=False,
                require_codex_command=False,
            )
            status = RuntimePlanImporter().status(
                session,
                project_id=project_id,
                include_guarded_dry_run=True,
            )

            assert result.status == "IMPORTED"
            assert result.tasks_imported == 2
            assert result.dependency_edges_imported == 1
            assert result.human_gates_bound == 1
            assert status.authority_active
            assert status.guarded_dry_run is not None
            assert status.guarded_dry_run["dry_run"] is True
            assert status.ready_tasks == (f"IMPL-TEST-A-{suffix}",)
            assert status.executions == 0
            assert status.active_leases == 0
            task_ids = (f"IMPL-TEST-A-{suffix}", f"IMPL-TEST-B-{suffix}")
            assert session.query(Execution).filter(Execution.task_id.in_(task_ids)).count() == 0
            assert session.query(TaskLease).filter(TaskLease.task_id.in_(task_ids)).count() == 0
            assert (
                session.query(RuntimeHumanGate)
                .filter_by(import_id=f"rhi-plan-test-{suffix}-v1", status="pending")
                .count()
                == 1
            )
    finally:
        clean_test_tables(database)
        database.dispose()


def _synthetic_artifacts(suffix: str) -> RuntimeHandoffArtifacts:
    task_a = _task(f"IMPL-TEST-A-{suffix}", ())
    task_b = _task(f"IMPL-TEST-B-{suffix}", (task_a.id,), risk="HIGH")
    tasks = (task_a, task_b)
    edges = ((task_b.id, task_a.id),)
    plan_hash = _hash({"tasks": [task.as_dict() for task in tasks], "edges": edges})
    feasibility_hash = _hash({"plan": plan_hash, "feasible": True})
    dry_hash = _hash({"plan": plan_hash, "feasibility": feasibility_hash})
    dependency_graph_hash = _hash([list(edge) for edge in edges])
    gate = {
        "id": f"GATE-{task_b.id}",
        "task_id": task_b.id,
        "reason": "synthetic high risk test gate",
        "required_before": "execution",
    }
    lock = ImplementationPlanLock(
        plan_id=f"PLAN-TEST-{suffix}",
        plan_version="1",
        project_id=project_id_for_suffix(suffix),
        compiled_project_hash="a" * 64,
        capability_resolution_hash="b" * 64,
        trace_validation_hash="c" * 64,
        implementation_plan_hash=plan_hash,
        feasibility_hash=feasibility_hash,
        dry_run_hash=dry_hash,
        task_generator_version="test-generator",
        feasibility_evaluator_version="test-feasibility",
        dry_runner_version="test-dry-runner",
        task_fingerprints={task.id: task.fingerprint for task in tasks},
        dependency_graph_hash=dependency_graph_hash,
        human_gate_definitions=(gate,),
        effective_concurrency=1,
        execution_prerequisites=("set AIENT_CODEX_COMMAND before real execution",),
        state="FROZEN",
    )
    plan = ImplementationPlan(
        source_compiled_hash=lock.compiled_project_hash,
        capability_resolution_hash=lock.capability_resolution_hash,
        trace_validation_hash=lock.trace_validation_hash,
        generator_version=lock.task_generator_version,
        implementation_plan_hash=plan_hash,
        epics=(),
        features=(),
        tasks=tasks,
        satisfied_units=(),
        dependency_edges=edges,
        waves=({"id": "WAVE-001", "task_ids": [task_a.id]}, {"id": "WAVE-002", "task_ids": [task_b.id]}),
        parallelizable_sets=(),
        critical_path=(task_a.id, task_b.id),
        human_gates=(gate,),
        findings=(),
        warning_classifications=(),
    )
    feasibility = FeasibilityEvaluation(
        implementation_plan_hash=plan_hash,
        source_compiled_hash=lock.compiled_project_hash,
        capability_resolution_hash=lock.capability_resolution_hash,
        trace_validation_hash=lock.trace_validation_hash,
        evaluator_version=lock.feasibility_evaluator_version,
        feasibility_hash=feasibility_hash,
        plan_status="READY_WITH_HUMAN_GATES",
        environment_profile=EnvironmentProfile(
            profile_id="test",
            repository_path=".",
            git_available=True,
            postgresql_available=True,
            alembic_available=True,
            docker_available=True,
            docker_compose_available=True,
            codex_command_configured=False,
            codex_executable_available=True,
            python_path="python",
            python_available=True,
            ram_mb=None,
            gpu_available=False,
            external_network="unknown",
            configured_secret_names=(),
        ),
        policy_profile=FeasibilityPolicyProfile(
            profile_id="test",
            agent_roles=("AGT-001",),
            model_profiles=("CODING_STANDARD",),
            executors=("codex",),
            verification_profiles=("STANDARD_REGRESSION",),
            auto_allowed_risks=("LOW",),
            guarded_allowed_risks=("LOW", "HIGH"),
            prohibited_path_patterns=(".env",),
            max_parallel_width=1,
        ),
        task_results=(
            _task_feasibility(task_a.id, "FEASIBLE_WITH_CONDITIONS", "AUTO_ALLOWED"),
            _task_feasibility(task_b.id, "HUMAN_APPROVAL_REQUIRED", "HUMAN_APPROVAL_REQUIRED", gate["id"]),
        ),
        human_gates=(gate,),
        feasible_parallel_width=1,
        technical_blockers=(),
        findings=(),
    )
    dry_run = ImplementationDryRun(
        compiled_project_hash=lock.compiled_project_hash,
        capability_resolution_hash=lock.capability_resolution_hash,
        trace_validation_hash=lock.trace_validation_hash,
        implementation_plan_hash=plan_hash,
        feasibility_hash=feasibility_hash,
        dry_runner_version=lock.dry_runner_version,
        dry_run_hash=dry_hash,
        plan_acceptance_state="READY_WITH_EXECUTION_PREREQUISITES",
        frozen_plan_state="FROZEN",
        dependency_edge_count=1,
        wave_count=2,
        theoretical_parallel_width=1,
        effective_parallel_width=1,
        execution_batches=(ExecutionBatch("RUN-BATCH-001", (task_a.id,), (), None),),
        import_preview=(
            TaskImportPreview(task_a.id, (), "implementation", True, "LOW", task_a.fingerprint, "STANDARD_REGRESSION"),
            TaskImportPreview(task_b.id, (task_a.id,), "implementation", True, "HIGH", task_b.fingerprint, "STANDARD_REGRESSION"),
        ),
        recovery_points=("before_task", "after_claim"),
        repair_path_coverage=(task_a.id, task_b.id),
        write_scope_conflicts=(),
        execution_prerequisites=lock.execution_prerequisites,
        findings=(),
        lock=lock,
    )
    return RuntimeHandoffArtifacts(plan=plan, feasibility=feasibility, dry_run=dry_run)


def project_id_for_suffix(suffix: str) -> str:
    return make_test_project_id(suffix)


def _task(task_id: str, depends_on: tuple[str, ...], *, risk: str = "LOW") -> GeneratedImplementationTask:
    fingerprint = _hash({"task_id": task_id, "depends_on": depends_on, "risk": risk})
    return GeneratedImplementationTask(
        id=task_id,
        title=task_id,
        objective=f"Implement {task_id}",
        implements={"requirements": ("FR-TEST",), "capabilities": ("C01",), "components": ("CMP-TEST",), "interfaces": ()},
        depends_on=depends_on,
        write_scope={"allowed": ("tests/fixtures/**",), "prohibited": (".env", "aient/**")},
        execution={"agent_role": "AGT-001", "model_profile": "CODING_STANDARD", "executor": "codex"},
        verification={"profile": "STANDARD_REGRESSION", "acceptance_criteria": ("synthetic test passes",)},
        risk={"level": risk, "reason": "synthetic test risk"},
        provenance={
            "compiled_project_hash": "a" * 64,
            "capability_resolution_hash": "b" * 64,
            "trace_validation_hash": "c" * 64,
            "generator_version": "test-generator",
        },
        fingerprint=fingerprint,
        epic_id="EPC-TEST",
        feature_id="FEA-TEST",
        component_id="CMP-TEST",
    )


def _task_feasibility(
    task_id: str,
    status: str,
    policy_decision: str,
    gate_id: str | None = None,
) -> TaskFeasibility:
    available = FeasibilityFacet("AVAILABLE", ("ok",))
    return TaskFeasibility(
        task_id=task_id,
        status=status,  # type: ignore[arg-type]
        reasons=("ok",),
        blockers=(),
        conditions=("set AIENT_CODEX_COMMAND before real execution",),
        required_human_gates=tuple([gate_id] if gate_id else []),
        agent_feasibility=available,
        model_feasibility=available,
        tool_feasibility=FeasibilityFacet("CONDITIONAL", ("set AIENT_CODEX_COMMAND before real execution",)),
        infrastructure_feasibility=available,
        resource_feasibility=available,
        verification_feasibility=available,
        policy_decision=policy_decision,  # type: ignore[arg-type]
        dependency_feasibility=available,
    )


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
