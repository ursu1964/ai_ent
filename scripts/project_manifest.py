from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_ent.e2e_application_proof import write_first_application_creation_proof
from ai_ent.external_project import load_external_project_frozen_plan
from ai_ent.persistence.config import load_database_settings
from ai_ent.persistence.database import Database
from ai_ent.persistence.models import RuntimePlanImport, Task
from ai_ent.post_implementation import (
    write_post_implementation_review,
    write_post_residual_implementation_review,
)
from ai_ent.product_decisions import record_prd_dec_001
from ai_ent.product_dry_run import (
    EXPECTED_PRD_DEC_001_HASH,
    write_product_dry_run,
)
from ai_ent.product_feasibility import write_product_feasibility
from ai_ent.product_plan_acceptance import (
    EXPECTED_PRODUCTIZATION_PLAN_HASH,
    write_product_plan_acceptance,
)
from ai_ent.product_plan_final_acceptance import (
    EXPECTED_PRODUCT_DRY_RUN_HASH,
    write_frozen_product_plan_acceptance,
)
from ai_ent.product_runtime_handoff import (
    EXPECTED_PPG_ACCEPTANCE_HASH,
    ProductRuntimePlanImporter,
    load_product_runtime_handoff_artifacts,
    write_product_runtime_handoff_evidence,
)
from ai_ent.productization_plan import write_productization_plan
from ai_ent.project_manifest import (
    compile_project_manifest,
    dry_run_implementation_plan,
    evaluate_feasibility,
    freeze_implementation_plan,
    generate_implementation_plan,
    resolve_capabilities,
    validate_project_manifest,
    validate_traces,
    write_capability_resolution,
    write_compiled_project,
    write_dry_run_plan,
    write_feasibility_evaluation,
    write_implementation_plan,
    write_trace_validation,
)
from ai_ent.residual_dag import write_residual_implementation_plan
from ai_ent.residual_dry_run import (
    dry_run_residual_plan,
    freeze_residual_plan,
    write_residual_dry_run_plan,
)
from ai_ent.residual_feasibility import (
    evaluate_residual_feasibility,
    write_residual_feasibility,
)
from ai_ent.residual_plan_acceptance import (
    accept_residual_plan,
    write_residual_plan_acceptance,
)
from ai_ent.residual_runtime_handoff import (
    RESIDUAL_PLAN_ID,
    ResidualRuntimePlanImporter,
    load_residual_runtime_handoff_artifacts,
)
from ai_ent.runtime_handoff import (
    DEFAULT_RUNTIME_PROJECT_ID,
    ExternalProjectRuntimeImporter,
    RuntimePlanImporter,
    load_runtime_handoff_artifacts,
)
from ai_ent.system_acceptance import write_system_acceptance


def validate(args: argparse.Namespace) -> int:
    result = validate_project_manifest(Path(args.manifest_root))
    report = result.as_dict()
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result.ok else 1


def compile_manifest(args: argparse.Namespace) -> int:
    result = write_compiled_project(Path(args.manifest_root), Path(args.output_dir))
    print(
        json.dumps(
            {
                "compiled_hash": result.lock.compiled_hash,
                "compiler_version": result.lock.compiler_version,
                "manifest_schema_version": result.lock.manifest_schema_version,
                "output_dir": args.output_dir,
                "source_file_count": len(result.lock.source_file_hashes),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def show_hash(args: argparse.Namespace) -> int:
    result = compile_project_manifest(Path(args.manifest_root))
    print(result.lock.compiled_hash)
    return 0


def resolve_capabilities_command(args: argparse.Namespace) -> int:
    resolution = write_capability_resolution(Path(args.manifest_root), Path(args.output_dir))
    print(
        json.dumps(
            {
                "capability_resolution_hash": resolution.capability_resolution_hash,
                "resolver_version": resolution.resolver_version,
                "source_compiled_hash": resolution.source_compiled_hash,
                "capabilities_resolved": len(resolution.capabilities),
                "direct_dependency_edges": len(resolution.direct_edges),
                "transitive_dependency_edges": len(resolution.transitive_edges),
                "output_dir": args.output_dir,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def capability_gaps(args: argparse.Namespace) -> int:
    resolution = resolve_capabilities(Path(args.manifest_root))
    print(
        json.dumps(
            {
                "capability_resolution_hash": resolution.capability_resolution_hash,
                "required_capabilities": list(resolution.required_capabilities),
                "satisfied_capabilities": list(resolution.satisfied_capabilities),
                "partially_satisfied_capabilities": list(
                    resolution.partially_satisfied_capabilities
                ),
                "unsatisfied_capabilities": list(resolution.unsatisfied_capabilities),
                "deferred_capabilities": list(resolution.deferred_capabilities),
                "blocked_capabilities": list(resolution.blocked_capabilities),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def validate_traces_command(args: argparse.Namespace) -> int:
    result = write_trace_validation(Path(args.manifest_root), Path(args.output_dir))
    print(
        json.dumps(
            {
                "trace_validation_hash": result.trace_validation_hash,
                "trace_validator_version": result.trace_validator_version,
                "source_compiled_hash": result.source_compiled_hash,
                "capability_resolution_hash": result.capability_resolution_hash,
                "error_count": result.error_count,
                "warning_count": result.warning_count,
                "output_dir": args.output_dir,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result.ok else 1


def trace_gaps(args: argparse.Namespace) -> int:
    result = validate_traces(Path(args.manifest_root))
    summary = result.as_dict()["summary"]
    print(
        json.dumps(
            {
                "trace_validation_hash": result.trace_validation_hash,
                "summary": summary,
                "implementation_gaps": list(result.implementation_gaps),
                "error_findings": [
                    finding.as_dict() for finding in result.findings if finding.severity == "ERROR"
                ],
                "warning_findings": [
                    finding.as_dict()
                    for finding in result.findings
                    if finding.severity == "WARNING"
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result.ok else 1


def generate_dag(args: argparse.Namespace) -> int:
    plan = write_implementation_plan(Path(args.manifest_root), Path(args.output_dir))
    summary = plan.as_dict()["summary"]
    print(
        json.dumps(
            {
                "implementation_plan_hash": plan.implementation_plan_hash,
                "generator_version": plan.generator_version,
                "source_compiled_hash": plan.source_compiled_hash,
                "capability_resolution_hash": plan.capability_resolution_hash,
                "trace_validation_hash": plan.trace_validation_hash,
                "executable_task_count": summary["executable_task_count"],
                "dependency_edge_count": summary["dependency_edge_count"],
                "wave_count": summary["wave_count"],
                "critical_path_task_count": summary["critical_path_task_count"],
                "human_gate_count": summary["human_gate_count"],
                "output_dir": args.output_dir,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if plan.ok else 1


def plan_summary(args: argparse.Namespace) -> int:
    plan = generate_implementation_plan(Path(args.manifest_root))
    report = plan.as_dict()
    print(
        json.dumps(
            {
                "implementation_plan_hash": plan.implementation_plan_hash,
                "summary": report["summary"],
                "implementation_gaps_covered": sorted(
                    {
                        capability
                        for task in plan.tasks
                        for capability in task.implements["capabilities"]
                    }
                ),
                "human_gates": list(plan.human_gates),
                "findings": [finding.as_dict() for finding in plan.findings],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if plan.ok else 1


def critical_path(args: argparse.Namespace) -> int:
    plan = generate_implementation_plan(Path(args.manifest_root))
    print(
        json.dumps(
            {
                "implementation_plan_hash": plan.implementation_plan_hash,
                "critical_path": list(plan.critical_path),
                "critical_path_length": len(plan.critical_path),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if plan.ok else 1


def evaluate_feasibility_command(args: argparse.Namespace) -> int:
    evaluation = write_feasibility_evaluation(Path(args.manifest_root), Path(args.output_dir))
    summary = evaluation.as_dict()["summary"]
    print(
        json.dumps(
            {
                "feasibility_hash": evaluation.feasibility_hash,
                "evaluator_version": evaluation.evaluator_version,
                "implementation_plan_hash": evaluation.implementation_plan_hash,
                "plan_status": evaluation.plan_status,
                "total_tasks": summary["total_tasks"],
                "feasible": summary["feasible"],
                "feasible_with_conditions": summary["feasible_with_conditions"],
                "human_approval_required": summary["human_approval_required"],
                "blocked": summary["blocked"],
                "deferred": summary["deferred"],
                "human_gates": summary["human_gates"],
                "feasible_parallel_width": summary["feasible_parallel_width"],
                "technical_blockers": summary["technical_blockers"],
                "output_dir": args.output_dir,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if evaluation.ok else 1


def feasibility_summary(args: argparse.Namespace) -> int:
    evaluation = evaluate_feasibility(Path(args.manifest_root))
    print(
        json.dumps(
            {
                "feasibility_hash": evaluation.feasibility_hash,
                "plan_status": evaluation.plan_status,
                "summary": evaluation.as_dict()["summary"],
                "technical_blockers": list(evaluation.technical_blockers),
                "human_gates": list(evaluation.human_gates),
                "task_statuses": [
                    {
                        "task_id": result.task_id,
                        "status": result.status,
                        "policy_decision": result.policy_decision,
                        "conditions": list(result.conditions),
                        "blockers": list(result.blockers),
                    }
                    for result in evaluation.task_results
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if evaluation.ok else 1


def dry_run_plan(args: argparse.Namespace) -> int:
    dry_run = write_dry_run_plan(Path(args.manifest_root), Path(args.output_dir))
    print(
        json.dumps(
            {
                "dry_run_hash": dry_run.dry_run_hash,
                "dry_runner_version": dry_run.dry_runner_version,
                "implementation_plan_hash": dry_run.implementation_plan_hash,
                "feasibility_hash": dry_run.feasibility_hash,
                "plan_acceptance_state": dry_run.plan_acceptance_state,
                "frozen_plan_state": dry_run.frozen_plan_state,
                "tasks_simulated": len(dry_run.import_preview),
                "dependency_edges": dry_run.dependency_edge_count,
                "waves": dry_run.wave_count,
                "theoretical_parallel_width": dry_run.theoretical_parallel_width,
                "effective_parallel_width": dry_run.effective_parallel_width,
                "human_gates": len(dry_run.lock.human_gate_definitions),
                "execution_batches": len(dry_run.execution_batches),
                "execution_prerequisites": list(dry_run.execution_prerequisites),
                "dry_run_errors": len(
                    [finding for finding in dry_run.findings if finding.severity == "ERROR"]
                ),
                "dry_run_warnings": len(
                    [finding for finding in dry_run.findings if finding.severity == "WARNING"]
                ),
                "output_dir": args.output_dir,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if dry_run.ok else 1


def freeze_plan(args: argparse.Namespace) -> int:
    dry_run = freeze_implementation_plan(Path(args.manifest_root), Path(args.output_dir))
    print(
        json.dumps(
            {
                "plan_id": dry_run.lock.plan_id,
                "plan_version": dry_run.lock.plan_version,
                "state": dry_run.frozen_plan_state,
                "dry_run_hash": dry_run.dry_run_hash,
                "implementation_plan_hash": dry_run.implementation_plan_hash,
                "feasibility_hash": dry_run.feasibility_hash,
                "output_dir": args.output_dir,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def plan_lock(args: argparse.Namespace) -> int:
    dry_run = dry_run_implementation_plan(Path(args.manifest_root))
    print(json.dumps(dry_run.lock.as_dict(), indent=2, sort_keys=True))
    return 0 if dry_run.ok else 1


def import_frozen_plan(args: argparse.Namespace) -> int:
    artifacts = load_runtime_handoff_artifacts(
        manifest_root=Path(args.manifest_root),
        output_dir=Path(args.output_dir),
    )
    settings = load_database_settings(Path(args.env_file))
    database = Database(settings)
    try:
        with database.session() as session:
            result = RuntimePlanImporter().import_frozen_plan(
                session,
                artifacts,
                runtime_project_id=args.project,
                repository_root=Path.cwd(),
            )
            print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
            return 0 if result.ok else 1
    finally:
        database.dispose()


def import_external_frozen_plan(args: argparse.Namespace) -> int:
    frozen_plan = load_external_project_frozen_plan(Path(args.plan_path))
    settings = load_database_settings(Path(args.env_file))
    database = Database(settings)
    try:
        with database.session() as session:
            receipt = ExternalProjectRuntimeImporter().import_runtime_binding(
                session,
                frozen_plan,
                runtime_project_id=args.project,
                repository_root=Path(args.repository_root),
                require_clean_git=args.require_clean_git,
                require_codex_command=args.require_codex_command,
            )
            print(json.dumps(receipt.as_dict(), indent=2, sort_keys=True))
            return 0 if receipt.ok else 1
    finally:
        database.dispose()


def runtime_plan_status(args: argparse.Namespace) -> int:
    settings = load_database_settings(Path(args.env_file))
    database = Database(settings)
    try:
        with database.session() as session:
            status = RuntimePlanImporter().status(
                session,
                project_id=args.project,
                include_guarded_dry_run=args.guarded_dry_run,
            )
            print(json.dumps(status.as_dict(), indent=2, sort_keys=True))
            return 0
    finally:
        database.dispose()


def post_implementation_review(args: argparse.Namespace) -> int:
    settings = load_database_settings(Path(args.env_file))
    database = Database(settings)
    try:
        with database.session() as session:
            review = write_post_implementation_review(
                session,
                manifest_root=Path(args.manifest_root),
                output_dir=Path(args.output_dir),
                artifacts_dir=Path(args.artifacts_dir),
                project_id=args.project,
                plan_id=args.plan_id,
                plan_version=args.plan_version,
                repository_root=Path.cwd(),
            )
            print(
                json.dumps(
                    {
                        "result": review.result,
                        "recommendation": review.as_dict()["recommendation"],
                        "head": review.head,
                        "plan_id": review.plan_id,
                        "plan_version": review.plan_version,
                        "post_compiled_hash": review.post_compiled_hash,
                        "post_capability_resolution_hash": review.post_capability_resolution_hash,
                        "post_trace_validation_hash": review.post_trace_validation_hash,
                        "residual_gap_hash": review.residual_gap_hash,
                        "residual_gap_count": len(review.residual_gaps),
                        "output_dir": args.output_dir,
                        "artifact_dir": str(Path(args.artifacts_dir) / "pir-001"),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
    finally:
        database.dispose()


def post_residual_implementation_review(args: argparse.Namespace) -> int:
    settings = load_database_settings(Path(args.env_file))
    database = Database(settings)
    try:
        with database.session() as session:
            review = write_post_residual_implementation_review(
                session,
                manifest_root=Path(args.manifest_root),
                output_dir=Path(args.output_dir),
                artifacts_dir=Path(args.artifacts_dir),
                project_id=args.project,
                prior_plan_id=args.prior_plan_id,
                residual_plan_id=args.residual_plan_id,
                plan_version=args.plan_version,
                repository_root=Path.cwd(),
            )
            print(
                json.dumps(
                    {
                        "result": review.result,
                        "recommendation": review.as_dict()["recommendation"],
                        "head": review.head,
                        "plan_id": review.plan_id,
                        "plan_version": review.plan_version,
                        "pir_002_compiled_hash": review.post_compiled_hash,
                        "pir_002_capability_hash": review.post_capability_resolution_hash,
                        "pir_002_trace_hash": review.post_trace_validation_hash,
                        "pir_002_residual_gap_hash": review.residual_gap_hash,
                        "residual_gap_count": len(review.residual_gaps),
                        "output_dir": args.output_dir,
                        "artifact_dir": str(Path(args.artifacts_dir) / "pir-002"),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
    finally:
        database.dispose()


def system_acceptance(args: argparse.Namespace) -> int:
    settings = load_database_settings(Path(args.env_file))
    database = Database(settings)
    try:
        with database.session() as session:
            result = write_system_acceptance(
                session,
                manifest_root=Path(args.manifest_root),
                artifacts_dir=Path(args.artifacts_dir),
                project_id=args.project,
                repository_root=Path.cwd(),
            )
            print(
                json.dumps(
                    {
                        "result": result.result,
                        "recommendation": result.recommendation,
                        "head": result.head,
                        "acceptance_hash": result.acceptance_hash,
                        "proof_count": len(result.proofs),
                        "negative_test_count": len(result.negative_tests),
                        "artifact_dir": str(Path(args.artifacts_dir) / "saag-001"),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0 if result.result in {"ACCEPTED", "ACCEPTED_WITH_LIMITATIONS"} else 1
    finally:
        database.dispose()


def first_application_creation_proof(args: argparse.Namespace) -> int:
    settings = load_database_settings(Path(args.env_file))
    database = Database(settings)
    try:
        with database.session() as session:
            result = write_first_application_creation_proof(
                session,
                target_workspace=Path(args.target_workspace),
                artifacts_dir=Path(args.artifacts_dir),
                project_id=args.project,
                run_docker=not args.skip_docker,
                repository_root=Path.cwd(),
            )
            print(
                json.dumps(
                    {
                        "result": result.result,
                        "recommendation": result.recommendation,
                        "head": result.head,
                        "target_project_id": result.target_project_id,
                        "target_workspace": result.target_workspace,
                        "target_plan_hash": result.target_plan_hash,
                        "application_commit": result.application_commit,
                        "application_url": result.application_url,
                        "acceptance_hash": result.acceptance_hash,
                        "proof_count": len(result.proofs),
                        "artifact_dir": str(Path(args.artifacts_dir) / "e2e-001"),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0 if result.result in {"ACCEPTED", "ACCEPTED_WITH_LIMITATIONS"} else 1
    finally:
        database.dispose()


def productization_plan(args: argparse.Namespace) -> int:
    result = write_productization_plan(
        artifacts_dir=Path(args.artifacts_dir),
        output_dir=Path(args.output_dir),
        repository_root=Path.cwd(),
    )
    dag = result.as_dict()["productization_dag"]
    print(
        json.dumps(
            {
                "result": result.result,
                "recommendation": result.recommendation,
                "baseline_head": result.baseline_head,
                "planner_version": result.planner_version,
                "plan_hash": result.plan_hash,
                "task_count": dag["summary"]["task_count"],
                "dependency_edges": dag["summary"]["dependency_edges"],
                "waves": dag["summary"]["waves"],
                "human_gates": dag["summary"]["human_gates"],
                "risk_distribution": dag["risk_distribution"],
                "artifact_dir": str(Path(args.artifacts_dir) / "prd-001"),
                "output": str(Path(args.output_dir) / "productization-plan.json"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result.result in {"ACCEPTED", "ACCEPTED_WITH_LIMITATIONS"} else 1


def product_plan_acceptance(args: argparse.Namespace) -> int:
    result = write_product_plan_acceptance(
        plan_path=Path(args.plan_path),
        artifacts_dir=Path(args.artifacts_dir),
        expected_plan_hash=args.expected_plan_hash,
        repository_root=Path.cwd(),
    )
    print(
        json.dumps(
            {
                "result": result.result,
                "recommendation": result.recommendation,
                "baseline": result.baseline,
                "evaluator_version": result.evaluator_version,
                "productization_plan_hash": result.productization_plan_hash,
                "acceptance_hash": result.acceptance_hash,
                "tasks": result.plan_identity["task_count"],
                "dependency_edges": result.plan_identity["dependency_edges"],
                "waves": result.plan_identity["waves"],
                "human_gates": result.plan_identity["human_gates"],
                "theoretical_parallel_width": result.plan_identity["theoretical_parallel_width"],
                "risk_distribution": result.plan_identity["risk_distribution"],
                "unresolved_decisions": len(result.unresolved_human_decisions),
                "current_blockers": list(result.current_blockers),
                "artifact_dir": str(Path(args.artifacts_dir) / "ppa-001"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result.result in {"ACCEPTED", "ACCEPTED_WITH_DECISIONS_REQUIRED"} else 1


def product_feasibility(args: argparse.Namespace) -> int:
    result = write_product_feasibility(
        plan_path=Path(args.plan_path),
        ppa_path=Path(args.ppa_path),
        artifacts_dir=Path(args.artifacts_dir),
        output_dir=Path(args.output_dir),
        repository_root=Path.cwd(),
        expected_plan_hash=args.expected_plan_hash,
    )
    summary = result.as_dict()["summary"]
    print(
        json.dumps(
            {
                "result": result.result,
                "recommendation": result.recommendation,
                "baseline": result.baseline,
                "evaluator_version": result.evaluator_version,
                "productization_plan_hash": result.productization_plan_hash,
                "product_feasibility_hash": result.product_feasibility_hash,
                "total_tasks": summary["total_tasks"],
                "feasible": summary["feasible"],
                "feasible_with_conditions": summary["feasible_with_conditions"],
                "human_approval_required": summary["human_approval_required"],
                "blocked": summary["blocked"],
                "deferred": summary["deferred"],
                "auto_allowed": summary["auto_allowed"],
                "guarded_allowed": summary["guarded_allowed"],
                "policy_human_approval_required": summary["policy_human_approval_required"],
                "prohibited": summary["prohibited"],
                "human_gates": summary["human_gates"],
                "theoretical_width": summary["theoretical_width"],
                "feasible_width": summary["feasible_width"],
                "technical_blockers": summary["technical_blockers"],
                "decision_blockers": summary["decision_blockers"],
                "artifact_dir": str(Path(args.artifacts_dir) / "pfe-001"),
                "output_dir": args.output_dir,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result.ok else 1


def record_product_stack_decision(args: argparse.Namespace) -> int:
    result = record_prd_dec_001(
        plan_path=Path(args.plan_path),
        ppa_path=Path(args.ppa_path),
        pfe_path=Path(args.pfe_path),
        artifacts_dir=Path(args.artifacts_dir),
        output_dir=Path(args.output_dir),
        repository_root=Path.cwd(),
        decided_by=args.decided_by,
        expected_plan_hash=args.expected_plan_hash,
        expected_pfe_hash=args.expected_pfe_hash,
    )
    print(
        json.dumps(
            {
                "result": result.result,
                "recommendation": result.recommendation,
                "decision_id": result.decision_id,
                "decision_status": result.decision_status,
                "decided_at": result.decided_at,
                "accepted_stack": result.accepted_stack,
                "accepted_product_plan_lineage": result.accepted_product_plan_lineage,
                "prd_dec_002_state": result.prd_dec_002_state,
                "prd_dec_003_state": result.prd_dec_003_state,
                "implementation_tasks_created": result.implementation_tasks_created,
                "implementation_executions": result.implementation_executions,
                "decision_hash": result.decision_hash,
                "artifact": str(
                    Path(args.artifacts_dir) / "product-decisions" / "PRD-DEC-001.json"
                ),
                "findings": list(result.validation_findings),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result.ok else 1


def product_dry_run(args: argparse.Namespace) -> int:
    result = write_product_dry_run(
        accepted_plan_path=Path(args.plan_path),
        ppa_path=Path(args.ppa_path),
        pfe_path=Path(args.pfe_path),
        decision_path=Path(args.decision_path),
        artifacts_dir=Path(args.artifacts_dir),
        output_dir=Path(args.output_dir),
        repository_root=Path.cwd(),
        expected_plan_hash=args.expected_plan_hash,
        expected_ppa_acceptance_hash=args.expected_ppa_acceptance_hash,
        expected_pfe_hash=args.expected_pfe_hash,
        expected_decision_hash=args.expected_decision_hash,
    )
    payload = result.as_dict()
    summary = payload["summary"]
    print(
        json.dumps(
            {
                "result": result.result,
                "recommendation": result.recommendation,
                "baseline_head": result.baseline_head,
                "dry_runner_version": result.dry_runner_version,
                "accepted_prd_plan_hash": result.accepted_prd_plan_hash,
                "regenerated_candidate_hash": result.regenerated_candidate_hash,
                "ppa_acceptance_hash": result.ppa_acceptance_hash,
                "pfe_feasibility_hash": result.pfe_feasibility_hash,
                "prd_dec_001_hash": result.prd_dec_001_hash,
                "lineage_reconciliation": result.lineage_reconciliation["result"],
                "semantic_diff_classification": result.lineage_reconciliation["classification"],
                "product_plan_id": result.product_plan_id,
                "plan_version": result.plan_version,
                "frozen_plan_state": result.frozen_plan_state,
                "product_dry_run_hash": result.product_dry_run_hash,
                "tasks": summary["task_count"],
                "dependency_edges": summary["dependency_edge_count"],
                "waves": summary["wave_count"],
                "human_gates": summary["human_gate_count"],
                "risk_distribution": summary["risk_distribution"],
                "policy_distribution": summary["policy_distribution"],
                "theoretical_parallel_width": summary["theoretical_parallel_width"],
                "effective_concurrency": summary["effective_concurrency"],
                "execution_limits": summary["execution_limits"],
                "prd_dec_001_state": result.decision_boundaries["PRD-DEC-001"]["state"],
                "prd_dec_002_state": result.decision_boundaries["PRD-DEC-002"]["state"],
                "prd_dec_003_state": result.decision_boundaries["PRD-DEC-003"]["state"],
                "dry_run_errors": summary["dry_run_errors"],
                "dry_run_warnings": summary["dry_run_warnings"],
                "artifact_dir": str(Path(args.artifacts_dir) / "pdf-001"),
                "output_dir": args.output_dir,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result.ok else 1


def product_plan_final_acceptance(args: argparse.Namespace) -> int:
    result = write_frozen_product_plan_acceptance(
        plan_path=Path(args.plan_path),
        ppa_path=Path(args.ppa_path),
        pfe_path=Path(args.pfe_path),
        decision_path=Path(args.decision_path),
        pdf_path=Path(args.pdf_path),
        lock_path=Path(args.lock_path),
        import_preview_path=Path(args.import_preview_path),
        artifacts_dir=Path(args.artifacts_dir),
        repository_root=Path.cwd(),
        env_file=Path(args.env_file),
        expected_product_plan_hash=args.expected_plan_hash,
        expected_ppa_hash=args.expected_ppa_acceptance_hash,
        expected_pfe_hash=args.expected_pfe_hash,
        expected_decision_hash=args.expected_decision_hash,
        expected_pdf_hash=args.expected_pdf_hash,
    )
    print(
        json.dumps(
            {
                "result": result.result,
                "recommendation": result.recommendation,
                "baseline_commit": result.baseline_commit,
                "product_plan_id": result.product_plan_id,
                "plan_version": result.plan_version,
                "frozen_state": result.frozen_state,
                "acceptance_hash": result.acceptance_hash,
                "bound_hashes": result.bound_hashes,
                "counts": result.counts,
                "risk_distribution": result.risk_distribution,
                "policy_distribution": result.policy_distribution,
                "prd_dec_002_state": result.decision_states["PRD-DEC-002"]["state"],
                "prd_dec_003_state": result.decision_states["PRD-DEC-003"]["state"],
                "blockers": list(result.blockers),
                "limitations": list(result.limitations),
                "runtime_snapshot": result.runtime_snapshot,
                "artifact_dir": str(Path(args.artifacts_dir) / "ppg-001"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result.ok else 1


def import_product_plan(args: argparse.Namespace) -> int:
    artifacts = load_product_runtime_handoff_artifacts(
        product_plan_path=Path(args.plan_path),
        import_preview_path=Path(args.import_preview_path),
        lock_path=Path(args.lock_path),
        ppg_path=Path(args.ppg_path),
        pdf_path=Path(args.pdf_path),
    )
    settings = load_database_settings(Path(args.env_file))
    database = Database(settings)
    try:
        with database.session() as session:
            result = ProductRuntimePlanImporter().import_product_plan(
                session,
                artifacts,
                runtime_project_id=args.project,
                repository_root=Path.cwd(),
                require_codex_command=args.require_codex_command,
                include_guarded_dry_run=args.guarded_dry_run,
            )
            write_product_runtime_handoff_evidence(
                result,
                artifacts_dir=Path(args.artifacts_dir),
                repository_root=Path.cwd(),
            )
            print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
            return 0 if result.ok else 1
    finally:
        database.dispose()


def generate_residual_dag(args: argparse.Namespace) -> int:
    plan = write_residual_implementation_plan(
        manifest_root=Path(args.manifest_root),
        pir_artifact=Path(args.pir_artifact),
        output_dir=Path(args.output_dir),
        repository_root=Path.cwd(),
    )
    summary = plan.as_dict()["summary"]
    print(
        json.dumps(
            {
                "residual_plan_hash": plan.residual_plan_hash,
                "generator_version": plan.generator_version,
                "post_compiled_hash": plan.post_compiled_hash,
                "post_capability_hash": plan.post_capability_hash,
                "post_trace_hash": plan.post_trace_hash,
                "pir_residual_gap_hash": plan.pir_residual_gap_hash,
                "total_residual_tasks": summary["total_residual_tasks"],
                "implementation_tasks": summary["implementation_tasks"],
                "evidence_closure_tasks": summary["evidence_closure_tasks"],
                "verification_tasks": summary["verification_tasks"],
                "dependency_edges": summary["dependency_edges"],
                "waves": summary["waves"],
                "critical_path_length": summary["critical_path_length"],
                "maximum_parallel_width": summary["maximum_parallel_width"],
                "human_gates": summary["human_gates"],
                "risk_distribution": summary["risk_distribution"],
                "output_dir": args.output_dir,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if plan.ok else 1


def evaluate_residual_feasibility_command(args: argparse.Namespace) -> int:
    evaluation = write_residual_feasibility(
        output_dir=Path(args.output_dir),
        repository_root=Path.cwd(),
        manifest_root=Path(args.manifest_root),
        pir_artifact=Path(args.pir_artifact),
    )
    summary = evaluation.as_dict()["summary"]
    print(
        json.dumps(
            {
                "residual_feasibility_hash": evaluation.residual_feasibility_hash,
                "evaluator_version": evaluation.evaluator_version,
                "residual_plan_hash": evaluation.residual_plan_hash,
                "plan_status": evaluation.plan_status,
                "total_tasks": summary["total_tasks"],
                "feasible": summary["feasible"],
                "feasible_with_conditions": summary["feasible_with_conditions"],
                "human_approval_required": summary["human_approval_required"],
                "blocked": summary["blocked"],
                "deferred": summary["deferred"],
                "auto_allowed": summary["auto_allowed"],
                "guarded_allowed": summary["guarded_allowed"],
                "prohibited": summary["prohibited"],
                "human_gates": summary["human_gates"],
                "feasible_parallel_width": summary["feasible_parallel_width"],
                "technical_blockers": summary["technical_blockers"],
                "output_dir": args.output_dir,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if evaluation.ok else 1


def residual_feasibility_summary(args: argparse.Namespace) -> int:
    evaluation = evaluate_residual_feasibility(
        repository_root=Path.cwd(),
        manifest_root=Path(args.manifest_root),
        pir_artifact=Path(args.pir_artifact),
    )
    print(
        json.dumps(
            {
                "residual_feasibility_hash": evaluation.residual_feasibility_hash,
                "plan_status": evaluation.plan_status,
                "summary": evaluation.as_dict()["summary"],
                "technical_blockers": list(evaluation.technical_blockers),
                "task_statuses": [
                    {
                        "task_id": result.task_id,
                        "task_type": result.task_type,
                        "status": result.feasibility_status,
                        "policy_decision": result.policy_decision,
                        "risk": result.risk,
                        "human_gate_ids": list(result.human_gate_ids),
                        "conditions": list(result.conditions),
                        "blockers": list(result.blockers),
                    }
                    for result in evaluation.task_results
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if evaluation.ok else 1


def dry_run_residual_plan_command(args: argparse.Namespace) -> int:
    dry_run = write_residual_dry_run_plan(
        output_dir=Path(args.output_dir),
        repository_root=Path.cwd(),
        manifest_root=Path(args.manifest_root),
        pir_artifact=Path(args.pir_artifact),
        expected_residual_plan_hash=args.expected_residual_plan_hash,
    )
    print(
        json.dumps(
            {
                "residual_dry_run_hash": dry_run.residual_dry_run_hash,
                "dry_runner_version": dry_run.dry_runner_version,
                "residual_plan_hash": dry_run.residual_plan_hash,
                "residual_feasibility_hash": dry_run.residual_feasibility_hash,
                "plan_acceptance_state": dry_run.plan_acceptance_state,
                "frozen_plan_state": dry_run.frozen_plan_state,
                "tasks_simulated": dry_run.task_count,
                "dependency_edges": dry_run.dependency_edge_count,
                "waves": dry_run.wave_count,
                "theoretical_parallel_width": dry_run.theoretical_parallel_width,
                "effective_parallel_width": dry_run.effective_parallel_width,
                "human_gates": len(dry_run.lock.human_gate_definitions),
                "execution_batches": len(dry_run.execution_batches),
                "execution_prerequisites": list(dry_run.execution_prerequisites),
                "dry_run_errors": len(
                    [finding for finding in dry_run.findings if finding.severity == "ERROR"]
                ),
                "dry_run_warnings": len(
                    [finding for finding in dry_run.findings if finding.severity == "WARNING"]
                ),
                "output_dir": args.output_dir,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if dry_run.ok else 1


def freeze_residual_plan_command(args: argparse.Namespace) -> int:
    dry_run = freeze_residual_plan(
        output_dir=Path(args.output_dir),
        repository_root=Path.cwd(),
        manifest_root=Path(args.manifest_root),
        pir_artifact=Path(args.pir_artifact),
        expected_residual_plan_hash=args.expected_residual_plan_hash,
    )
    print(
        json.dumps(
            {
                "residual_plan_id": dry_run.lock.residual_plan_id,
                "plan_version": dry_run.lock.plan_version,
                "state": dry_run.frozen_plan_state,
                "residual_plan_hash": dry_run.residual_plan_hash,
                "residual_feasibility_hash": dry_run.residual_feasibility_hash,
                "residual_dry_run_hash": dry_run.residual_dry_run_hash,
                "output_dir": args.output_dir,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def residual_plan_lock(args: argparse.Namespace) -> int:
    dry_run = dry_run_residual_plan(
        repository_root=Path.cwd(),
        manifest_root=Path(args.manifest_root),
        pir_artifact=Path(args.pir_artifact),
        expected_residual_plan_hash=args.expected_residual_plan_hash,
    )
    print(json.dumps(dry_run.lock.as_dict(), indent=2, sort_keys=True))
    return 0 if dry_run.ok else 1


def accept_residual_plan_command(args: argparse.Namespace) -> int:
    gate = write_residual_plan_acceptance(
        artifacts_dir=Path(args.artifacts_dir),
        output_dir=Path(args.output_dir),
        repository_root=Path.cwd(),
        manifest_root=Path(args.manifest_root),
        pir_artifact=Path(args.pir_artifact),
        expected_residual_plan_hash=args.expected_residual_plan_hash,
    )
    print(
        json.dumps(
            {
                "gate_id": gate.gate_id,
                "gate_result": gate.gate_result,
                "recommendation": gate.recommendation,
                "residual_plan_id": gate.residual_plan_id,
                "plan_version": gate.plan_version,
                "residual_plan_hash": gate.bound_hashes["residual_plan_hash"],
                "residual_feasibility_hash": gate.bound_hashes["residual_feasibility_hash"],
                "residual_dry_run_hash": gate.bound_hashes["residual_dry_run_hash"],
                "tasks": gate.counts["tasks"],
                "dependency_edges": gate.counts["dependency_edges"],
                "human_gates": gate.counts["human_gates"],
                "defects": list(gate.defects),
                "limitations": list(gate.limitations),
                "artifacts_dir": args.artifacts_dir,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if gate.ok else 1


def residual_acceptance_summary(args: argparse.Namespace) -> int:
    gate = accept_residual_plan(
        repository_root=Path.cwd(),
        manifest_root=Path(args.manifest_root),
        pir_artifact=Path(args.pir_artifact),
        expected_residual_plan_hash=args.expected_residual_plan_hash,
    )
    print(
        json.dumps(
            {
                "gate_id": gate.gate_id,
                "gate_result": gate.gate_result,
                "recommendation": gate.recommendation,
                "counts": gate.counts,
                "risk_distribution": gate.risk_distribution,
                "human_gates": list(gate.human_gates),
                "execution_prerequisites": list(gate.execution_prerequisites),
                "defects": list(gate.defects),
                "limitations": list(gate.limitations),
                "validations": gate.validations,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if gate.ok else 1


def import_residual_plan(args: argparse.Namespace) -> int:
    artifacts = load_residual_runtime_handoff_artifacts(
        manifest_root=Path(args.manifest_root),
        pir_artifact=Path(args.pir_artifact),
        acceptance_artifact_path=Path(args.acceptance_artifact),
    )
    settings = load_database_settings(Path(args.env_file))
    database = Database(settings)
    try:
        with database.session() as session:
            result = ResidualRuntimePlanImporter().import_residual_plan(
                session,
                artifacts,
                runtime_project_id=args.project,
                repository_root=Path.cwd(),
                include_guarded_dry_run=args.guarded_dry_run,
            )
            print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
            return 0 if result.ok else 1
    finally:
        database.dispose()


def residual_runtime_plan_status(args: argparse.Namespace) -> int:
    settings = load_database_settings(Path(args.env_file))
    database = Database(settings)
    try:
        with database.session() as session:
            import_id = f"rhi-{args.plan_id.lower()}-v{args.plan_version}"
            task_ids = [
                task.id
                for task in session.query(Task)
                .filter_by(project_id=args.project)
                .filter(Task.id.like("RES-%"))
                .order_by(Task.id)
                .all()
            ]
            result = {
                "project_id": args.project,
                "import_id": import_id,
                "residual_plan_id": args.plan_id,
                "plan_version": args.plan_version,
                "residual_tasks": len(task_ids),
                "task_ids": task_ids,
                "receipt_exists": session.get(RuntimePlanImport, import_id) is not None,
            }
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
    finally:
        database.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI-Enterprise project manifest tools")
    subparsers = parser.add_subparsers(required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    validate_parser.add_argument("--report")
    validate_parser.set_defaults(func=validate)

    compile_parser = subparsers.add_parser("compile")
    compile_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    compile_parser.add_argument("--output-dir", default=".build/compiled")
    compile_parser.set_defaults(func=compile_manifest)

    hash_parser = subparsers.add_parser("show-hash")
    hash_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    hash_parser.set_defaults(func=show_hash)

    resolve_parser = subparsers.add_parser("resolve-capabilities")
    resolve_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    resolve_parser.add_argument("--output-dir", default=".build/compiled")
    resolve_parser.set_defaults(func=resolve_capabilities_command)

    gaps_parser = subparsers.add_parser("capability-gaps")
    gaps_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    gaps_parser.set_defaults(func=capability_gaps)

    trace_parser = subparsers.add_parser("validate-traces")
    trace_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    trace_parser.add_argument("--output-dir", default=".build/compiled")
    trace_parser.set_defaults(func=validate_traces_command)

    trace_gaps_parser = subparsers.add_parser("trace-gaps")
    trace_gaps_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    trace_gaps_parser.set_defaults(func=trace_gaps)

    dag_parser = subparsers.add_parser("generate-dag")
    dag_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    dag_parser.add_argument("--output-dir", default=".build/compiled")
    dag_parser.set_defaults(func=generate_dag)

    summary_parser = subparsers.add_parser("plan-summary")
    summary_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    summary_parser.set_defaults(func=plan_summary)

    critical_path_parser = subparsers.add_parser("critical-path")
    critical_path_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    critical_path_parser.set_defaults(func=critical_path)

    feasibility_parser = subparsers.add_parser("evaluate-feasibility")
    feasibility_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    feasibility_parser.add_argument("--output-dir", default=".build/compiled")
    feasibility_parser.set_defaults(func=evaluate_feasibility_command)

    feasibility_summary_parser = subparsers.add_parser("feasibility-summary")
    feasibility_summary_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    feasibility_summary_parser.set_defaults(func=feasibility_summary)

    dry_run_parser = subparsers.add_parser("dry-run-plan")
    dry_run_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    dry_run_parser.add_argument("--output-dir", default=".build/compiled")
    dry_run_parser.set_defaults(func=dry_run_plan)

    freeze_parser = subparsers.add_parser("freeze-plan")
    freeze_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    freeze_parser.add_argument("--output-dir", default=".build/compiled")
    freeze_parser.set_defaults(func=freeze_plan)

    lock_parser = subparsers.add_parser("plan-lock")
    lock_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    lock_parser.set_defaults(func=plan_lock)

    import_parser = subparsers.add_parser("import-frozen-plan")
    import_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    import_parser.add_argument("--output-dir", default=".build/compiled")
    import_parser.add_argument("--env-file", default=".env")
    import_parser.add_argument("--project", default=DEFAULT_RUNTIME_PROJECT_ID)
    import_parser.set_defaults(func=import_frozen_plan)

    external_import_parser = subparsers.add_parser("import-external-frozen-plan")
    external_import_parser.add_argument("--plan-path", required=True)
    external_import_parser.add_argument("--env-file", default=".env")
    external_import_parser.add_argument("--project")
    external_import_parser.add_argument("--repository-root", default=".")
    external_import_parser.add_argument(
        "--require-clean-git",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    external_import_parser.add_argument(
        "--require-codex-command",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    external_import_parser.set_defaults(func=import_external_frozen_plan)

    status_parser = subparsers.add_parser("runtime-plan-status")
    status_parser.add_argument("--env-file", default=".env")
    status_parser.add_argument("--project", default=DEFAULT_RUNTIME_PROJECT_ID)
    status_parser.add_argument("--guarded-dry-run", action="store_true")
    status_parser.set_defaults(func=runtime_plan_status)

    pir_parser = subparsers.add_parser("post-implementation-review")
    pir_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    pir_parser.add_argument("--output-dir", default=".build/compiled")
    pir_parser.add_argument("--artifacts-dir", default="artifacts")
    pir_parser.add_argument("--env-file", default=".env")
    pir_parser.add_argument("--project", default=DEFAULT_RUNTIME_PROJECT_ID)
    pir_parser.add_argument("--plan-id", default="PLAN-1a75a2e3c5a7")
    pir_parser.add_argument("--plan-version", default="1")
    pir_parser.set_defaults(func=post_implementation_review)

    pir_alias_parser = subparsers.add_parser("post-implementation")
    pir_alias_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    pir_alias_parser.add_argument("--output-dir", default=".build/compiled")
    pir_alias_parser.add_argument("--artifacts-dir", default="artifacts")
    pir_alias_parser.add_argument("--env-file", default=".env")
    pir_alias_parser.add_argument("--project", default=DEFAULT_RUNTIME_PROJECT_ID)
    pir_alias_parser.add_argument("--plan-id", default="PLAN-1a75a2e3c5a7")
    pir_alias_parser.add_argument("--plan-version", default="1")
    pir_alias_parser.set_defaults(func=post_implementation_review)

    pir2_parser = subparsers.add_parser("post-residual-implementation")
    pir2_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    pir2_parser.add_argument("--output-dir", default=".build/compiled")
    pir2_parser.add_argument("--artifacts-dir", default="artifacts")
    pir2_parser.add_argument("--env-file", default=".env")
    pir2_parser.add_argument("--project", default=DEFAULT_RUNTIME_PROJECT_ID)
    pir2_parser.add_argument("--prior-plan-id", default="PLAN-1a75a2e3c5a7")
    pir2_parser.add_argument("--residual-plan-id", default=RESIDUAL_PLAN_ID)
    pir2_parser.add_argument("--plan-version", default="1")
    pir2_parser.set_defaults(func=post_residual_implementation_review)

    pir2_alias_parser = subparsers.add_parser("pir-002")
    pir2_alias_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    pir2_alias_parser.add_argument("--output-dir", default=".build/compiled")
    pir2_alias_parser.add_argument("--artifacts-dir", default="artifacts")
    pir2_alias_parser.add_argument("--env-file", default=".env")
    pir2_alias_parser.add_argument("--project", default=DEFAULT_RUNTIME_PROJECT_ID)
    pir2_alias_parser.add_argument("--prior-plan-id", default="PLAN-1a75a2e3c5a7")
    pir2_alias_parser.add_argument("--residual-plan-id", default=RESIDUAL_PLAN_ID)
    pir2_alias_parser.add_argument("--plan-version", default="1")
    pir2_alias_parser.set_defaults(func=post_residual_implementation_review)

    saag_parser = subparsers.add_parser("system-acceptance")
    saag_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    saag_parser.add_argument("--artifacts-dir", default="artifacts")
    saag_parser.add_argument("--env-file", default=".env")
    saag_parser.add_argument("--project", default=DEFAULT_RUNTIME_PROJECT_ID)
    saag_parser.set_defaults(func=system_acceptance)

    saag_alias_parser = subparsers.add_parser("saag-001")
    saag_alias_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    saag_alias_parser.add_argument("--artifacts-dir", default="artifacts")
    saag_alias_parser.add_argument("--env-file", default=".env")
    saag_alias_parser.add_argument("--project", default=DEFAULT_RUNTIME_PROJECT_ID)
    saag_alias_parser.set_defaults(func=system_acceptance)

    e2e_parser = subparsers.add_parser("first-application-proof")
    e2e_parser.add_argument("--artifacts-dir", default="artifacts")
    e2e_parser.add_argument("--env-file", default=".env")
    e2e_parser.add_argument("--project", default=DEFAULT_RUNTIME_PROJECT_ID)
    e2e_parser.add_argument(
        "--target-workspace", default="/home/user/projects/e2e-team-work-tracker"
    )
    e2e_parser.add_argument("--skip-docker", action="store_true")
    e2e_parser.set_defaults(func=first_application_creation_proof)

    e2e_alias_parser = subparsers.add_parser("e2e-001")
    e2e_alias_parser.add_argument("--artifacts-dir", default="artifacts")
    e2e_alias_parser.add_argument("--env-file", default=".env")
    e2e_alias_parser.add_argument("--project", default=DEFAULT_RUNTIME_PROJECT_ID)
    e2e_alias_parser.add_argument(
        "--target-workspace", default="/home/user/projects/e2e-team-work-tracker"
    )
    e2e_alias_parser.add_argument("--skip-docker", action="store_true")
    e2e_alias_parser.set_defaults(func=first_application_creation_proof)

    prd_parser = subparsers.add_parser("productization-plan")
    prd_parser.add_argument("--artifacts-dir", default="artifacts")
    prd_parser.add_argument("--output-dir", default=".build/compiled")
    prd_parser.set_defaults(func=productization_plan)

    prd_alias_parser = subparsers.add_parser("prd-001")
    prd_alias_parser.add_argument("--artifacts-dir", default="artifacts")
    prd_alias_parser.add_argument("--output-dir", default=".build/compiled")
    prd_alias_parser.set_defaults(func=productization_plan)

    ppa_parser = subparsers.add_parser("product-plan-acceptance")
    ppa_parser.add_argument("--plan-path", default=".build/compiled/productization-plan.json")
    ppa_parser.add_argument("--artifacts-dir", default="artifacts")
    ppa_parser.add_argument("--expected-plan-hash", default=EXPECTED_PRODUCTIZATION_PLAN_HASH)
    ppa_parser.set_defaults(func=product_plan_acceptance)

    ppa_alias_parser = subparsers.add_parser("ppa-001")
    ppa_alias_parser.add_argument("--plan-path", default=".build/compiled/productization-plan.json")
    ppa_alias_parser.add_argument("--artifacts-dir", default="artifacts")
    ppa_alias_parser.add_argument("--expected-plan-hash", default=EXPECTED_PRODUCTIZATION_PLAN_HASH)
    ppa_alias_parser.set_defaults(func=product_plan_acceptance)

    pfe_parser = subparsers.add_parser("product-feasibility")
    pfe_parser.add_argument("--plan-path", default=".build/compiled/productization-plan.json")
    pfe_parser.add_argument("--ppa-path", default="artifacts/ppa-001/PPA-001.json")
    pfe_parser.add_argument("--artifacts-dir", default="artifacts")
    pfe_parser.add_argument("--output-dir", default=".build/compiled")
    pfe_parser.add_argument("--expected-plan-hash", default=EXPECTED_PRODUCTIZATION_PLAN_HASH)
    pfe_parser.set_defaults(func=product_feasibility)

    pfe_alias_parser = subparsers.add_parser("pfe-001")
    pfe_alias_parser.add_argument("--plan-path", default=".build/compiled/productization-plan.json")
    pfe_alias_parser.add_argument("--ppa-path", default="artifacts/ppa-001/PPA-001.json")
    pfe_alias_parser.add_argument("--artifacts-dir", default="artifacts")
    pfe_alias_parser.add_argument("--output-dir", default=".build/compiled")
    pfe_alias_parser.add_argument("--expected-plan-hash", default=EXPECTED_PRODUCTIZATION_PLAN_HASH)
    pfe_alias_parser.set_defaults(func=product_feasibility)

    prd_dec_parser = subparsers.add_parser("record-product-stack-decision")
    prd_dec_parser.add_argument("--plan-path", default=".build/compiled/productization-plan.json")
    prd_dec_parser.add_argument("--ppa-path", default="artifacts/ppa-001/PPA-001.json")
    prd_dec_parser.add_argument("--pfe-path", default="artifacts/pfe-001/PFE-001.json")
    prd_dec_parser.add_argument("--artifacts-dir", default="artifacts")
    prd_dec_parser.add_argument("--output-dir", default=".build/compiled")
    prd_dec_parser.add_argument("--decided-by", default="operator")
    prd_dec_parser.add_argument("--expected-plan-hash", default=EXPECTED_PRODUCTIZATION_PLAN_HASH)
    prd_dec_parser.add_argument(
        "--expected-pfe-hash",
        default="25cbfa87927d3486e096364c76c532459f75081d2819b0dd2961dfdcbc4855f9",
    )
    prd_dec_parser.set_defaults(func=record_product_stack_decision)

    prd_dec_alias_parser = subparsers.add_parser("prd-dec-001")
    prd_dec_alias_parser.add_argument(
        "--plan-path", default=".build/compiled/productization-plan.json"
    )
    prd_dec_alias_parser.add_argument("--ppa-path", default="artifacts/ppa-001/PPA-001.json")
    prd_dec_alias_parser.add_argument("--pfe-path", default="artifacts/pfe-001/PFE-001.json")
    prd_dec_alias_parser.add_argument("--artifacts-dir", default="artifacts")
    prd_dec_alias_parser.add_argument("--output-dir", default=".build/compiled")
    prd_dec_alias_parser.add_argument("--decided-by", default="operator")
    prd_dec_alias_parser.add_argument(
        "--expected-plan-hash", default=EXPECTED_PRODUCTIZATION_PLAN_HASH
    )
    prd_dec_alias_parser.add_argument(
        "--expected-pfe-hash",
        default="25cbfa87927d3486e096364c76c532459f75081d2819b0dd2961dfdcbc4855f9",
    )
    prd_dec_alias_parser.set_defaults(func=record_product_stack_decision)

    pdf_parser = subparsers.add_parser("product-dry-run")
    pdf_parser.add_argument("--plan-path", default=".build/compiled/productization-plan.json")
    pdf_parser.add_argument("--ppa-path", default="artifacts/ppa-001/PPA-001.json")
    pdf_parser.add_argument("--pfe-path", default="artifacts/pfe-001/PFE-001.json")
    pdf_parser.add_argument(
        "--decision-path", default="artifacts/product-decisions/PRD-DEC-001.json"
    )
    pdf_parser.add_argument("--artifacts-dir", default="artifacts")
    pdf_parser.add_argument("--output-dir", default=".build/compiled")
    pdf_parser.add_argument("--expected-plan-hash", default=EXPECTED_PRODUCTIZATION_PLAN_HASH)
    pdf_parser.add_argument(
        "--expected-ppa-acceptance-hash",
        default="cf97940d5848f8c651232e23b733325f4dd274aa7a45078abab0039d80e638e9",
    )
    pdf_parser.add_argument(
        "--expected-pfe-hash",
        default="25cbfa87927d3486e096364c76c532459f75081d2819b0dd2961dfdcbc4855f9",
    )
    pdf_parser.add_argument("--expected-decision-hash", default=EXPECTED_PRD_DEC_001_HASH)
    pdf_parser.set_defaults(func=product_dry_run)

    pdf_alias_parser = subparsers.add_parser("pdf-001")
    pdf_alias_parser.add_argument("--plan-path", default=".build/compiled/productization-plan.json")
    pdf_alias_parser.add_argument("--ppa-path", default="artifacts/ppa-001/PPA-001.json")
    pdf_alias_parser.add_argument("--pfe-path", default="artifacts/pfe-001/PFE-001.json")
    pdf_alias_parser.add_argument(
        "--decision-path", default="artifacts/product-decisions/PRD-DEC-001.json"
    )
    pdf_alias_parser.add_argument("--artifacts-dir", default="artifacts")
    pdf_alias_parser.add_argument("--output-dir", default=".build/compiled")
    pdf_alias_parser.add_argument(
        "--expected-plan-hash", default=EXPECTED_PRODUCTIZATION_PLAN_HASH
    )
    pdf_alias_parser.add_argument(
        "--expected-ppa-acceptance-hash",
        default="cf97940d5848f8c651232e23b733325f4dd274aa7a45078abab0039d80e638e9",
    )
    pdf_alias_parser.add_argument(
        "--expected-pfe-hash",
        default="25cbfa87927d3486e096364c76c532459f75081d2819b0dd2961dfdcbc4855f9",
    )
    pdf_alias_parser.add_argument(
        "--expected-decision-hash", default=EXPECTED_PRD_DEC_001_HASH
    )
    pdf_alias_parser.set_defaults(func=product_dry_run)

    ppg_parser = subparsers.add_parser("product-plan-final-acceptance")
    ppg_parser.add_argument("--plan-path", default=".build/compiled/productization-plan.json")
    ppg_parser.add_argument("--ppa-path", default="artifacts/ppa-001/PPA-001.json")
    ppg_parser.add_argument("--pfe-path", default="artifacts/pfe-001/PFE-001.json")
    ppg_parser.add_argument(
        "--decision-path", default="artifacts/product-decisions/PRD-DEC-001.json"
    )
    ppg_parser.add_argument("--pdf-path", default="artifacts/pdf-001/PDF-001.json")
    ppg_parser.add_argument("--lock-path", default=".build/compiled/product-plan.lock")
    ppg_parser.add_argument(
        "--import-preview-path", default=".build/compiled/product-task-import-preview.json"
    )
    ppg_parser.add_argument("--artifacts-dir", default="artifacts")
    ppg_parser.add_argument("--env-file", default=".env")
    ppg_parser.add_argument("--expected-plan-hash", default=EXPECTED_PRODUCTIZATION_PLAN_HASH)
    ppg_parser.add_argument(
        "--expected-ppa-acceptance-hash",
        default="cf97940d5848f8c651232e23b733325f4dd274aa7a45078abab0039d80e638e9",
    )
    ppg_parser.add_argument(
        "--expected-pfe-hash",
        default="25cbfa87927d3486e096364c76c532459f75081d2819b0dd2961dfdcbc4855f9",
    )
    ppg_parser.add_argument("--expected-decision-hash", default=EXPECTED_PRD_DEC_001_HASH)
    ppg_parser.add_argument("--expected-pdf-hash", default=EXPECTED_PRODUCT_DRY_RUN_HASH)
    ppg_parser.set_defaults(func=product_plan_final_acceptance)

    ppg_alias_parser = subparsers.add_parser("ppg-001")
    ppg_alias_parser.add_argument("--plan-path", default=".build/compiled/productization-plan.json")
    ppg_alias_parser.add_argument("--ppa-path", default="artifacts/ppa-001/PPA-001.json")
    ppg_alias_parser.add_argument("--pfe-path", default="artifacts/pfe-001/PFE-001.json")
    ppg_alias_parser.add_argument(
        "--decision-path", default="artifacts/product-decisions/PRD-DEC-001.json"
    )
    ppg_alias_parser.add_argument("--pdf-path", default="artifacts/pdf-001/PDF-001.json")
    ppg_alias_parser.add_argument("--lock-path", default=".build/compiled/product-plan.lock")
    ppg_alias_parser.add_argument(
        "--import-preview-path", default=".build/compiled/product-task-import-preview.json"
    )
    ppg_alias_parser.add_argument("--artifacts-dir", default="artifacts")
    ppg_alias_parser.add_argument("--env-file", default=".env")
    ppg_alias_parser.add_argument(
        "--expected-plan-hash", default=EXPECTED_PRODUCTIZATION_PLAN_HASH
    )
    ppg_alias_parser.add_argument(
        "--expected-ppa-acceptance-hash",
        default="cf97940d5848f8c651232e23b733325f4dd274aa7a45078abab0039d80e638e9",
    )
    ppg_alias_parser.add_argument(
        "--expected-pfe-hash",
        default="25cbfa87927d3486e096364c76c532459f75081d2819b0dd2961dfdcbc4855f9",
    )
    ppg_alias_parser.add_argument(
        "--expected-decision-hash", default=EXPECTED_PRD_DEC_001_HASH
    )
    ppg_alias_parser.add_argument("--expected-pdf-hash", default=EXPECTED_PRODUCT_DRY_RUN_HASH)
    ppg_alias_parser.set_defaults(func=product_plan_final_acceptance)

    phi_parser = subparsers.add_parser("import-product-plan")
    phi_parser.add_argument("--plan-path", default=".build/compiled/productization-plan.json")
    phi_parser.add_argument(
        "--import-preview-path", default=".build/compiled/product-task-import-preview.json"
    )
    phi_parser.add_argument("--lock-path", default=".build/compiled/product-plan.lock")
    phi_parser.add_argument("--ppg-path", default="artifacts/ppg-001/PPG-001.json")
    phi_parser.add_argument("--pdf-path", default="artifacts/pdf-001/PDF-001.json")
    phi_parser.add_argument("--artifacts-dir", default="artifacts")
    phi_parser.add_argument("--env-file", default=".env")
    phi_parser.add_argument("--project", default=DEFAULT_RUNTIME_PROJECT_ID)
    phi_parser.add_argument("--guarded-dry-run", action="store_true")
    phi_parser.add_argument("--require-codex-command", action="store_true")
    phi_parser.add_argument("--expected-ppg-acceptance-hash", default=EXPECTED_PPG_ACCEPTANCE_HASH)
    phi_parser.set_defaults(func=import_product_plan)

    phi_alias_parser = subparsers.add_parser("phi-001")
    phi_alias_parser.add_argument("--plan-path", default=".build/compiled/productization-plan.json")
    phi_alias_parser.add_argument(
        "--import-preview-path", default=".build/compiled/product-task-import-preview.json"
    )
    phi_alias_parser.add_argument("--lock-path", default=".build/compiled/product-plan.lock")
    phi_alias_parser.add_argument("--ppg-path", default="artifacts/ppg-001/PPG-001.json")
    phi_alias_parser.add_argument("--pdf-path", default="artifacts/pdf-001/PDF-001.json")
    phi_alias_parser.add_argument("--artifacts-dir", default="artifacts")
    phi_alias_parser.add_argument("--env-file", default=".env")
    phi_alias_parser.add_argument("--project", default=DEFAULT_RUNTIME_PROJECT_ID)
    phi_alias_parser.add_argument("--guarded-dry-run", action="store_true")
    phi_alias_parser.add_argument("--require-codex-command", action="store_true")
    phi_alias_parser.add_argument("--expected-ppg-acceptance-hash", default=EXPECTED_PPG_ACCEPTANCE_HASH)
    phi_alias_parser.set_defaults(func=import_product_plan)

    residual_parser = subparsers.add_parser("generate-residual-dag")
    residual_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    residual_parser.add_argument("--pir-artifact", default="artifacts/pir-001/PIR-001.json")
    residual_parser.add_argument("--output-dir", default=".build/compiled")
    residual_parser.set_defaults(func=generate_residual_dag)

    residual_feasibility_parser = subparsers.add_parser("evaluate-residual-feasibility")
    residual_feasibility_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    residual_feasibility_parser.add_argument(
        "--pir-artifact", default="artifacts/pir-001/PIR-001.json"
    )
    residual_feasibility_parser.add_argument("--output-dir", default=".build/compiled")
    residual_feasibility_parser.set_defaults(func=evaluate_residual_feasibility_command)

    residual_feasibility_summary_parser = subparsers.add_parser("residual-feasibility-summary")
    residual_feasibility_summary_parser.add_argument(
        "--manifest-root", default="manifest/project/ai-ent"
    )
    residual_feasibility_summary_parser.add_argument(
        "--pir-artifact", default="artifacts/pir-001/PIR-001.json"
    )
    residual_feasibility_summary_parser.set_defaults(func=residual_feasibility_summary)

    residual_dry_run_parser = subparsers.add_parser("dry-run-residual-plan")
    residual_dry_run_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    residual_dry_run_parser.add_argument("--pir-artifact", default="artifacts/pir-001/PIR-001.json")
    residual_dry_run_parser.add_argument("--output-dir", default=".build/compiled")
    residual_dry_run_parser.add_argument("--expected-residual-plan-hash")
    residual_dry_run_parser.set_defaults(func=dry_run_residual_plan_command)

    residual_freeze_parser = subparsers.add_parser("freeze-residual-plan")
    residual_freeze_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    residual_freeze_parser.add_argument("--pir-artifact", default="artifacts/pir-001/PIR-001.json")
    residual_freeze_parser.add_argument("--output-dir", default=".build/compiled")
    residual_freeze_parser.add_argument("--expected-residual-plan-hash")
    residual_freeze_parser.set_defaults(func=freeze_residual_plan_command)

    residual_lock_parser = subparsers.add_parser("residual-plan-lock")
    residual_lock_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    residual_lock_parser.add_argument("--pir-artifact", default="artifacts/pir-001/PIR-001.json")
    residual_lock_parser.add_argument("--expected-residual-plan-hash")
    residual_lock_parser.set_defaults(func=residual_plan_lock)

    residual_accept_parser = subparsers.add_parser("accept-residual-plan")
    residual_accept_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    residual_accept_parser.add_argument("--pir-artifact", default="artifacts/pir-001/PIR-001.json")
    residual_accept_parser.add_argument("--output-dir", default=".build/compiled")
    residual_accept_parser.add_argument("--artifacts-dir", default="artifacts/rpg-001")
    residual_accept_parser.add_argument("--expected-residual-plan-hash")
    residual_accept_parser.set_defaults(func=accept_residual_plan_command)

    residual_accept_summary_parser = subparsers.add_parser("residual-acceptance-summary")
    residual_accept_summary_parser.add_argument(
        "--manifest-root", default="manifest/project/ai-ent"
    )
    residual_accept_summary_parser.add_argument(
        "--pir-artifact", default="artifacts/pir-001/PIR-001.json"
    )
    residual_accept_summary_parser.add_argument("--expected-residual-plan-hash")
    residual_accept_summary_parser.set_defaults(func=residual_acceptance_summary)

    import_residual_parser = subparsers.add_parser("import-residual-plan")
    import_residual_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    import_residual_parser.add_argument("--pir-artifact", default="artifacts/pir-001/PIR-001.json")
    import_residual_parser.add_argument(
        "--acceptance-artifact", default="artifacts/rpg-001/RPG-001.json"
    )
    import_residual_parser.add_argument("--env-file", default=".env")
    import_residual_parser.add_argument("--project", default=DEFAULT_RUNTIME_PROJECT_ID)
    import_residual_parser.add_argument("--guarded-dry-run", action="store_true")
    import_residual_parser.set_defaults(func=import_residual_plan)

    residual_status_parser = subparsers.add_parser("runtime-residual-plan-status")
    residual_status_parser.add_argument("--env-file", default=".env")
    residual_status_parser.add_argument("--project", default=DEFAULT_RUNTIME_PROJECT_ID)
    residual_status_parser.add_argument("--plan-id", default=RESIDUAL_PLAN_ID)
    residual_status_parser.add_argument("--plan-version", default="1")
    residual_status_parser.set_defaults(func=residual_runtime_plan_status)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
