from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_ent.persistence.config import load_database_settings
from ai_ent.persistence.database import Database
from ai_ent.post_implementation import write_post_implementation_review
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
from ai_ent.residual_feasibility import (
    evaluate_residual_feasibility,
    write_residual_feasibility,
)
from ai_ent.runtime_handoff import (
    DEFAULT_RUNTIME_PROJECT_ID,
    RuntimePlanImporter,
    load_runtime_handoff_artifacts,
)


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
                "partially_satisfied_capabilities": list(resolution.partially_satisfied_capabilities),
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
                    finding.as_dict() for finding in result.findings if finding.severity == "WARNING"
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
                    {capability for task in plan.tasks for capability in task.implements["capabilities"]}
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
                "dry_run_errors": len([finding for finding in dry_run.findings if finding.severity == "ERROR"]),
                "dry_run_warnings": len([finding for finding in dry_run.findings if finding.severity == "WARNING"]),
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

    residual_parser = subparsers.add_parser("generate-residual-dag")
    residual_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    residual_parser.add_argument("--pir-artifact", default="artifacts/pir-001/PIR-001.json")
    residual_parser.add_argument("--output-dir", default=".build/compiled")
    residual_parser.set_defaults(func=generate_residual_dag)

    residual_feasibility_parser = subparsers.add_parser("evaluate-residual-feasibility")
    residual_feasibility_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    residual_feasibility_parser.add_argument("--pir-artifact", default="artifacts/pir-001/PIR-001.json")
    residual_feasibility_parser.add_argument("--output-dir", default=".build/compiled")
    residual_feasibility_parser.set_defaults(func=evaluate_residual_feasibility_command)

    residual_feasibility_summary_parser = subparsers.add_parser("residual-feasibility-summary")
    residual_feasibility_summary_parser.add_argument("--manifest-root", default="manifest/project/ai-ent")
    residual_feasibility_summary_parser.add_argument("--pir-artifact", default="artifacts/pir-001/PIR-001.json")
    residual_feasibility_summary_parser.set_defaults(func=residual_feasibility_summary)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
