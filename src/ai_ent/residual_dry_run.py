from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ai_ent.project_manifest import (
    GENERATED_MARKER,
    DryRunFinding,
    ExecutionBatch,
    FrozenPlanState,
    canonical_bytes,
)
from ai_ent.residual_dag import (
    RESIDUAL_DAG_GENERATOR_VERSION,
    ResidualGeneratedTask,
    ResidualImplementationPlan,
    generate_residual_implementation_plan,
)
from ai_ent.residual_feasibility import (
    RFE_EVALUATOR_VERSION,
    ResidualFeasibilityEvaluation,
    evaluate_residual_feasibility,
)

RESIDUAL_DRY_RUNNER_VERSION = "rdf-001.1"
ResidualPlanAcceptanceState = Literal[
    "READY_FOR_RESIDUAL_PLAN_ACCEPTANCE",
    "READY_WITH_EXECUTION_PREREQUISITES",
    "BLOCKED",
    "INVALID",
]


@dataclass(frozen=True)
class ResidualTaskImportPreview:
    task_id: str
    fingerprint: str
    depends_on: tuple[str, ...]
    residual_gap_refs: tuple[str, ...]
    task_type: str
    execution_class: str
    schedulable: bool
    risk_level: str
    human_gate_ids: tuple[str, ...]
    agent_role: str
    model_profile: str
    executor: str
    verification_profile: str
    allowed_write_scope: tuple[str, ...]
    prohibited_paths: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "fingerprint": self.fingerprint,
            "depends_on": list(self.depends_on),
            "residual_gap_refs": list(self.residual_gap_refs),
            "task_type": self.task_type,
            "execution_class": self.execution_class,
            "schedulable": self.schedulable,
            "risk_level": self.risk_level,
            "human_gate_ids": list(self.human_gate_ids),
            "agent_role": self.agent_role,
            "model_profile": self.model_profile,
            "executor": self.executor,
            "verification_profile": self.verification_profile,
            "allowed_write_scope": list(self.allowed_write_scope),
            "prohibited_paths": list(self.prohibited_paths),
        }


@dataclass(frozen=True)
class ResidualPlanLock:
    residual_plan_id: str
    plan_version: str
    state: FrozenPlanState
    baseline_head: str
    post_compiled_hash: str
    post_capability_hash: str
    post_trace_hash: str
    pir_residual_gap_hash: str
    residual_plan_hash: str
    residual_feasibility_hash: str
    residual_dry_run_hash: str
    task_generator_version: str
    feasibility_evaluator_version: str
    dry_runner_version: str
    task_fingerprints: dict[str, str]
    dependency_edges: tuple[tuple[str, str], ...]
    dependency_graph_hash: str
    human_gate_definitions: tuple[dict[str, Any], ...]
    effective_concurrency: int
    execution_prerequisites: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "residual_plan_id": self.residual_plan_id,
            "plan_version": self.plan_version,
            "state": self.state,
            "baseline_head": self.baseline_head,
            "post_compiled_hash": self.post_compiled_hash,
            "post_capability_hash": self.post_capability_hash,
            "post_trace_hash": self.post_trace_hash,
            "pir_residual_gap_hash": self.pir_residual_gap_hash,
            "residual_plan_hash": self.residual_plan_hash,
            "residual_feasibility_hash": self.residual_feasibility_hash,
            "residual_dry_run_hash": self.residual_dry_run_hash,
            "task_generator_version": self.task_generator_version,
            "feasibility_evaluator_version": self.feasibility_evaluator_version,
            "dry_runner_version": self.dry_runner_version,
            "task_fingerprints": self.task_fingerprints,
            "dependency_edges": [list(edge) for edge in self.dependency_edges],
            "dependency_graph_hash": self.dependency_graph_hash,
            "human_gate_definitions": list(self.human_gate_definitions),
            "effective_concurrency": self.effective_concurrency,
            "execution_prerequisites": list(self.execution_prerequisites),
        }


@dataclass(frozen=True)
class ResidualDryRun:
    baseline_head: str
    post_compiled_hash: str
    post_capability_hash: str
    post_trace_hash: str
    pir_residual_gap_hash: str
    residual_plan_hash: str
    residual_feasibility_hash: str
    dry_runner_version: str
    residual_dry_run_hash: str
    plan_acceptance_state: ResidualPlanAcceptanceState
    frozen_plan_state: FrozenPlanState
    task_count: int
    implementation_task_count: int
    evidence_task_count: int
    verification_task_count: int
    dependency_edge_count: int
    wave_count: int
    theoretical_parallel_width: int
    effective_parallel_width: int
    execution_batches: tuple[ExecutionBatch, ...]
    import_preview: tuple[ResidualTaskImportPreview, ...]
    recovery_points: tuple[str, ...]
    repair_path_coverage: tuple[str, ...]
    evidence_semantics: tuple[dict[str, str], ...]
    c08_sequence: tuple[str, ...]
    prior_plan_coexistence: tuple[str, ...]
    recommended_limits: dict[str, int]
    write_scope_conflicts: tuple[str, ...]
    execution_prerequisites: tuple[str, ...]
    findings: tuple[DryRunFinding, ...]
    lock: ResidualPlanLock

    @property
    def ok(self) -> bool:
        return not any(finding.severity == "ERROR" for finding in self.findings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "baseline_head": self.baseline_head,
            "post_compiled_hash": self.post_compiled_hash,
            "post_capability_hash": self.post_capability_hash,
            "post_trace_hash": self.post_trace_hash,
            "pir_residual_gap_hash": self.pir_residual_gap_hash,
            "residual_plan_hash": self.residual_plan_hash,
            "residual_feasibility_hash": self.residual_feasibility_hash,
            "dry_runner_version": self.dry_runner_version,
            "residual_dry_run_hash": self.residual_dry_run_hash,
            "plan_acceptance_state": self.plan_acceptance_state,
            "frozen_plan_state": self.frozen_plan_state,
            "task_count": self.task_count,
            "implementation_task_count": self.implementation_task_count,
            "evidence_task_count": self.evidence_task_count,
            "verification_task_count": self.verification_task_count,
            "dependency_edge_count": self.dependency_edge_count,
            "wave_count": self.wave_count,
            "theoretical_parallel_width": self.theoretical_parallel_width,
            "effective_parallel_width": self.effective_parallel_width,
            "execution_batches": [batch.as_dict() for batch in self.execution_batches],
            "task_import_preview": [preview.as_dict() for preview in self.import_preview],
            "recovery_points": list(self.recovery_points),
            "repair_path_coverage": list(self.repair_path_coverage),
            "evidence_semantics": list(self.evidence_semantics),
            "c08_sequence": list(self.c08_sequence),
            "prior_plan_coexistence": list(self.prior_plan_coexistence),
            "recommended_limits": self.recommended_limits,
            "write_scope_conflicts": list(self.write_scope_conflicts),
            "execution_prerequisites": list(self.execution_prerequisites),
            "findings": [finding.as_dict() for finding in self.findings],
            "residual_plan_lock": self.lock.as_dict(),
            "summary": {
                "tasks_simulated": self.task_count,
                "implementation_tasks": self.implementation_task_count,
                "evidence_tasks": self.evidence_task_count,
                "verification_tasks": self.verification_task_count,
                "dependency_edges": self.dependency_edge_count,
                "waves": self.wave_count,
                "theoretical_parallel_width": self.theoretical_parallel_width,
                "effective_parallel_width": self.effective_parallel_width,
                "human_gates": len(self.lock.human_gate_definitions),
                "execution_batches": len(self.execution_batches),
                "dry_run_errors": len([finding for finding in self.findings if finding.severity == "ERROR"]),
                "dry_run_warnings": len([finding for finding in self.findings if finding.severity == "WARNING"]),
                "execution_prerequisites": len(self.execution_prerequisites),
            },
        }


def dry_run_residual_plan(
    *,
    plan: ResidualImplementationPlan | None = None,
    feasibility: ResidualFeasibilityEvaluation | None = None,
    repository_root: Path = Path("."),
    manifest_root: Path = Path("manifest/project/ai-ent"),
    pir_artifact: Path = Path("artifacts/pir-001/PIR-001.json"),
    expected_residual_plan_hash: str | None = None,
) -> ResidualDryRun:
    residual_plan = plan or generate_residual_implementation_plan(
        manifest_root=manifest_root,
        pir_artifact=pir_artifact,
        repository_root=repository_root,
    )
    residual_feasibility = feasibility or evaluate_residual_feasibility(
        plan=residual_plan,
        repository_root=repository_root,
        manifest_root=manifest_root,
        pir_artifact=pir_artifact,
    )
    return dry_run_evaluated_residual_plan(
        residual_plan,
        residual_feasibility,
        expected_residual_plan_hash=expected_residual_plan_hash,
    )


def dry_run_evaluated_residual_plan(
    plan: ResidualImplementationPlan,
    feasibility: ResidualFeasibilityEvaluation,
    *,
    expected_residual_plan_hash: str | None = None,
) -> ResidualDryRun:
    findings = _precondition_findings(plan, feasibility, expected_residual_plan_hash)
    execution_prerequisites = _execution_prerequisites(feasibility)
    findings.extend(
        DryRunFinding(
            f"RDF-PREREQ-{index:03d}",
            "EXECUTION_PREREQUISITE",
            "execution",
            prerequisite,
        )
        for index, prerequisite in enumerate(execution_prerequisites, start=1)
    )
    if any(result.feasibility_status == "HUMAN_APPROVAL_REQUIRED" for result in feasibility.task_results):
        findings.append(
            DryRunFinding(
                "RDF-HUMAN-GATES",
                "INFO",
                "human-gates",
                "residual human gates are represented as explicit stop/resume boundaries",
            )
        )
    effective_width = feasibility.feasible_parallel_width
    theoretical_width = max((len(wave["task_ids"]) for wave in plan.waves), default=0)
    batches = _execution_batches(plan)
    import_preview = tuple(_import_preview(task, plan) for task in plan.tasks)
    recovery_points = (
        "after_claim",
        "after_worktree_created",
        "after_executor",
        "after_verification",
        "after_verified_commit",
        "before_db_completion",
        "between_tasks",
        "at_human_gates",
    )
    repair_path_coverage = tuple(sorted(task.id for task in plan.tasks))
    evidence_semantics = tuple(_evidence_semantic(task) for task in plan.tasks if task.task_type == "EVIDENCE_CLOSURE_TASK")
    c08_sequence = ("RES-C08-CONTRACT", "RES-C08-SERVICE", "RES-C08-VERIFICATION")
    _validate_c08_sequence(plan, findings)
    _validate_import_preview(import_preview, findings)
    write_scope_conflicts = _write_scope_conflicts(plan)
    state = _acceptance_state(findings, execution_prerequisites, feasibility)
    frozen_state: FrozenPlanState = "FROZEN" if state in {"READY_FOR_RESIDUAL_PLAN_ACCEPTANCE", "READY_WITH_EXECUTION_PREREQUISITES"} else "INVALIDATED"
    dependency_graph_hash = hashlib.sha256(canonical_bytes([list(edge) for edge in plan.dependency_edges])).hexdigest()
    dry_payload = {
        "baseline_head": plan.head,
        "post_compiled_hash": plan.post_compiled_hash,
        "post_capability_hash": plan.post_capability_hash,
        "post_trace_hash": plan.post_trace_hash,
        "pir_residual_gap_hash": plan.pir_residual_gap_hash,
        "residual_plan_hash": plan.residual_plan_hash,
        "residual_feasibility_hash": feasibility.residual_feasibility_hash,
        "dry_runner_version": RESIDUAL_DRY_RUNNER_VERSION,
        "plan_acceptance_state": state,
        "frozen_plan_state": frozen_state,
        "dependency_edge_count": len(plan.dependency_edges),
        "wave_count": len(plan.waves),
        "theoretical_parallel_width": theoretical_width,
        "effective_parallel_width": effective_width,
        "execution_batches": [batch.as_dict() for batch in batches],
        "task_import_preview": [preview.as_dict() for preview in import_preview],
        "recovery_points": list(recovery_points),
        "repair_path_coverage": list(repair_path_coverage),
        "evidence_semantics": list(evidence_semantics),
        "c08_sequence": list(c08_sequence),
        "prior_plan_coexistence": list(_prior_plan_coexistence()),
        "recommended_limits": _recommended_limits(effective_width),
        "write_scope_conflicts": list(write_scope_conflicts),
        "execution_prerequisites": list(execution_prerequisites),
        "findings": [finding.as_dict() for finding in sorted(findings, key=lambda item: item.finding_id)],
    }
    dry_run_hash = hashlib.sha256(canonical_bytes(dry_payload)).hexdigest()
    lock = ResidualPlanLock(
        residual_plan_id=f"RESIDUAL-PLAN-{plan.residual_plan_hash[:12]}",
        plan_version="1",
        state=frozen_state,
        baseline_head=plan.head,
        post_compiled_hash=plan.post_compiled_hash,
        post_capability_hash=plan.post_capability_hash,
        post_trace_hash=plan.post_trace_hash,
        pir_residual_gap_hash=plan.pir_residual_gap_hash,
        residual_plan_hash=plan.residual_plan_hash,
        residual_feasibility_hash=feasibility.residual_feasibility_hash,
        residual_dry_run_hash=dry_run_hash,
        task_generator_version=RESIDUAL_DAG_GENERATOR_VERSION,
        feasibility_evaluator_version=RFE_EVALUATOR_VERSION,
        dry_runner_version=RESIDUAL_DRY_RUNNER_VERSION,
        task_fingerprints={task.id: task.fingerprint for task in plan.tasks},
        dependency_edges=plan.dependency_edges,
        dependency_graph_hash=dependency_graph_hash,
        human_gate_definitions=plan.human_gates,
        effective_concurrency=effective_width,
        execution_prerequisites=execution_prerequisites,
    )
    return ResidualDryRun(
        baseline_head=plan.head,
        post_compiled_hash=plan.post_compiled_hash,
        post_capability_hash=plan.post_capability_hash,
        post_trace_hash=plan.post_trace_hash,
        pir_residual_gap_hash=plan.pir_residual_gap_hash,
        residual_plan_hash=plan.residual_plan_hash,
        residual_feasibility_hash=feasibility.residual_feasibility_hash,
        dry_runner_version=RESIDUAL_DRY_RUNNER_VERSION,
        residual_dry_run_hash=dry_run_hash,
        plan_acceptance_state=state,
        frozen_plan_state=frozen_state,
        task_count=len(plan.tasks),
        implementation_task_count=len([task for task in plan.tasks if task.task_type == "IMPLEMENTATION_TASK"]),
        evidence_task_count=len([task for task in plan.tasks if task.task_type == "EVIDENCE_CLOSURE_TASK"]),
        verification_task_count=len([task for task in plan.tasks if task.task_type == "VERIFICATION_TASK"]),
        dependency_edge_count=len(plan.dependency_edges),
        wave_count=len(plan.waves),
        theoretical_parallel_width=theoretical_width,
        effective_parallel_width=effective_width,
        execution_batches=batches,
        import_preview=import_preview,
        recovery_points=recovery_points,
        repair_path_coverage=repair_path_coverage,
        evidence_semantics=evidence_semantics,
        c08_sequence=c08_sequence,
        prior_plan_coexistence=_prior_plan_coexistence(),
        recommended_limits=_recommended_limits(effective_width),
        write_scope_conflicts=write_scope_conflicts,
        execution_prerequisites=execution_prerequisites,
        findings=tuple(sorted(findings, key=lambda item: item.finding_id)),
        lock=lock,
    )


def write_residual_dry_run_plan(
    *,
    output_dir: Path = Path(".build/compiled"),
    repository_root: Path = Path("."),
    manifest_root: Path = Path("manifest/project/ai-ent"),
    pir_artifact: Path = Path("artifacts/pir-001/PIR-001.json"),
    expected_residual_plan_hash: str | None = None,
) -> ResidualDryRun:
    dry_run = dry_run_residual_plan(
        repository_root=repository_root,
        manifest_root=manifest_root,
        pir_artifact=pir_artifact,
        expected_residual_plan_hash=expected_residual_plan_hash,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "residual-implementation-dry-run.json", dry_run.as_dict())
    _write_json(
        output_dir / "residual-execution-batches.json",
        {
            "generated": GENERATED_MARKER,
            "residual_dry_run_hash": dry_run.residual_dry_run_hash,
            "execution_batches": [batch.as_dict() for batch in dry_run.execution_batches],
        },
    )
    _write_json(
        output_dir / "residual-task-import-preview.json",
        {
            "generated": GENERATED_MARKER,
            "residual_dry_run_hash": dry_run.residual_dry_run_hash,
            "task_import_preview": [preview.as_dict() for preview in dry_run.import_preview],
        },
    )
    _write_json(
        output_dir / "residual-recovery-plan.json",
        {
            "generated": GENERATED_MARKER,
            "residual_dry_run_hash": dry_run.residual_dry_run_hash,
            "recovery_points": list(dry_run.recovery_points),
        },
    )
    _write_json(
        output_dir / "residual-dry-run-findings.json",
        {
            "generated": GENERATED_MARKER,
            "residual_dry_run_hash": dry_run.residual_dry_run_hash,
            "findings": [finding.as_dict() for finding in dry_run.findings],
        },
    )
    return dry_run


def freeze_residual_plan(
    *,
    output_dir: Path = Path(".build/compiled"),
    repository_root: Path = Path("."),
    manifest_root: Path = Path("manifest/project/ai-ent"),
    pir_artifact: Path = Path("artifacts/pir-001/PIR-001.json"),
    expected_residual_plan_hash: str | None = None,
) -> ResidualDryRun:
    dry_run = write_residual_dry_run_plan(
        output_dir=output_dir,
        repository_root=repository_root,
        manifest_root=manifest_root,
        pir_artifact=pir_artifact,
        expected_residual_plan_hash=expected_residual_plan_hash,
    )
    if any(finding.severity == "ERROR" for finding in dry_run.findings):
        raise ValueError("residual dry-run has ERROR findings and cannot be frozen")
    _write_json(output_dir / "residual-implementation-plan.lock", dry_run.lock.as_dict())
    _write_json(
        output_dir / "residual-plan-freeze.json",
        {
            "generated": GENERATED_MARKER,
            "residual_plan_id": dry_run.lock.residual_plan_id,
            "plan_version": dry_run.lock.plan_version,
            "state": dry_run.frozen_plan_state,
            "residual_plan_hash": dry_run.residual_plan_hash,
            "residual_feasibility_hash": dry_run.residual_feasibility_hash,
            "residual_dry_run_hash": dry_run.residual_dry_run_hash,
            "invalidation_rules": residual_plan_invalidation_rules(),
        },
    )
    return dry_run


def residual_plan_invalidation_rules() -> tuple[str, ...]:
    return (
        "residual gap hash change",
        "residual plan hash change",
        "residual task fingerprint change",
        "residual dependency graph change",
        "residual feasibility hash change",
        "residual human gate definition change",
        "material residual environment or policy profile change",
    )


def _precondition_findings(
    plan: ResidualImplementationPlan,
    feasibility: ResidualFeasibilityEvaluation,
    expected_residual_plan_hash: str | None,
) -> list[DryRunFinding]:
    findings: list[DryRunFinding] = []
    if expected_residual_plan_hash and plan.residual_plan_hash != expected_residual_plan_hash:
        findings.append(
            DryRunFinding(
                "RDF-EXPECTED-RESIDUAL-PLAN-HASH",
                "ERROR",
                "residual-plan",
                f"residual plan hash {plan.residual_plan_hash} does not match expected {expected_residual_plan_hash}",
            )
        )
    if plan.residual_plan_hash != feasibility.residual_plan_hash:
        findings.append(DryRunFinding("RDF-PLAN-HASH", "ERROR", "residual-plan", "residual plan hash mismatch"))
    if plan.head != feasibility.head:
        findings.append(DryRunFinding("RDF-HEAD", "ERROR", "git", "residual plan and feasibility HEAD mismatch"))
    if plan.post_compiled_hash != feasibility.post_compiled_hash:
        findings.append(DryRunFinding("RDF-COMPILED-HASH", "ERROR", "compiled-project", "post compiled hash mismatch"))
    if plan.post_capability_hash != feasibility.post_capability_hash:
        findings.append(DryRunFinding("RDF-CAPABILITY-HASH", "ERROR", "capabilities", "post capability hash mismatch"))
    if plan.post_trace_hash != feasibility.post_trace_hash:
        findings.append(DryRunFinding("RDF-TRACE-HASH", "ERROR", "trace", "post trace hash mismatch"))
    if plan.pir_residual_gap_hash != feasibility.pir_residual_gap_hash:
        findings.append(DryRunFinding("RDF-GAP-HASH", "ERROR", "pir", "PIR residual gap hash mismatch"))
    if len(plan.tasks) != len(feasibility.task_results):
        findings.append(DryRunFinding("RDF-TASK-COUNT", "ERROR", "tasks", "feasibility did not evaluate every residual task"))
    if len(plan.tasks) != 7:
        findings.append(DryRunFinding("RDF-EXPECTED-TASKS", "ERROR", "tasks", "residual plan must contain exactly 7 tasks"))
    if len(plan.dependency_edges) != 6:
        findings.append(DryRunFinding("RDF-EXPECTED-EDGES", "ERROR", "dependencies", "residual plan must contain exactly 6 edges"))
    if len(plan.waves) != 6:
        findings.append(DryRunFinding("RDF-EXPECTED-WAVES", "ERROR", "waves", "residual plan must contain exactly 6 waves"))
    if len(plan.human_gates) != 2:
        findings.append(DryRunFinding("RDF-EXPECTED-GATES", "ERROR", "human-gates", "residual plan must contain exactly 2 gates"))
    if feasibility.technical_blockers:
        findings.append(DryRunFinding("RDF-TECHNICAL-BLOCKERS", "ERROR", "feasibility", "technical blockers remain"))
    if any(result.feasibility_status == "BLOCKED" for result in feasibility.task_results):
        findings.append(DryRunFinding("RDF-BLOCKED-TASKS", "ERROR", "feasibility", "one or more residual tasks are blocked"))
    if feasibility.plan_status == "INVALID":
        findings.append(DryRunFinding("RDF-FEASIBILITY-INVALID", "ERROR", "feasibility", "residual feasibility is invalid"))
    return findings


def _execution_prerequisites(feasibility: ResidualFeasibilityEvaluation) -> tuple[str, ...]:
    prerequisites = set()
    for result in feasibility.task_results:
        for condition in result.conditions:
            if "AIENT_CODEX_COMMAND" in condition:
                prerequisites.add("AIENT_CODEX_COMMAND must be configured before runtime import/execution")
            else:
                prerequisites.add(condition)
    return tuple(sorted(prerequisites))


def _execution_batches(plan: ResidualImplementationPlan) -> tuple[ExecutionBatch, ...]:
    gate_by_task = {str(gate["task_id"]): str(gate["id"]) for gate in plan.human_gates}
    ordered_tasks = [task_id for wave in plan.waves for task_id in wave["task_ids"]]
    batches: list[ExecutionBatch] = []
    current: list[str] = []
    resume_after: str | None = None

    def flush() -> None:
        nonlocal current, resume_after
        if current:
            batches.append(
                ExecutionBatch(
                    id=f"RES-RUN-BATCH-{len(batches) + 1:03d}",
                    task_ids=tuple(current),
                    required_human_gates=(),
                    resume_after=resume_after,
                )
            )
            current = []
            resume_after = None

    for task_id in ordered_tasks:
        gate_id = gate_by_task.get(task_id)
        if gate_id:
            flush()
            batches.append(
                ExecutionBatch(
                    id=f"RES-RUN-BATCH-{len(batches) + 1:03d}",
                    task_ids=(task_id,),
                    required_human_gates=(gate_id,),
                    resume_after=gate_id,
                )
            )
            resume_after = gate_id
            continue
        current.append(task_id)
    flush()
    return tuple(batches)


def _import_preview(task: ResidualGeneratedTask, plan: ResidualImplementationPlan) -> ResidualTaskImportPreview:
    gate_ids = tuple(sorted(str(gate["id"]) for gate in plan.human_gates if gate["task_id"] == task.id))
    return ResidualTaskImportPreview(
        task_id=task.id,
        fingerprint=task.fingerprint,
        depends_on=task.depends_on,
        residual_gap_refs=task.gap_ids,
        task_type=task.task_type,
        execution_class="residual_implementation",
        schedulable=True,
        risk_level=task.risk["level"],
        human_gate_ids=gate_ids,
        agent_role=task.execution["agent_role"],
        model_profile=task.execution["model_profile"],
        executor=task.execution["executor"],
        verification_profile=str(task.verification["profile"]),
        allowed_write_scope=task.write_scope["allowed"],
        prohibited_paths=task.write_scope["prohibited"],
    )


def _evidence_semantic(task: ResidualGeneratedTask) -> dict[str, str]:
    return {
        "task_id": task.id,
        "capability_id": task.capabilities[0],
        "behavior": "gather/link evidence and verify closure; do not rebuild existing runtime behavior",
    }


def _validate_c08_sequence(plan: ResidualImplementationPlan, findings: list[DryRunFinding]) -> None:
    dependencies = {task.id: set(task.depends_on) for task in plan.tasks}
    if "RES-C08-CONTRACT" not in dependencies.get("RES-C08-SERVICE", set()):
        findings.append(DryRunFinding("RDF-C08-SERVICE-ORDER", "ERROR", "RES-C08-SERVICE", "C08 service must depend on C08 contract"))
    if "RES-C08-SERVICE" not in dependencies.get("RES-C08-VERIFICATION", set()):
        findings.append(
            DryRunFinding("RDF-C08-VERIFICATION-ORDER", "ERROR", "RES-C08-VERIFICATION", "C08 verification must depend on C08 service")
        )


def _validate_import_preview(previews: tuple[ResidualTaskImportPreview, ...], findings: list[DryRunFinding]) -> None:
    for preview in previews:
        if not preview.fingerprint or not preview.residual_gap_refs:
            findings.append(DryRunFinding(f"RDF-{preview.task_id}-IMPORT", "ERROR", preview.task_id, "runtime import preview lacks identity"))
        if not preview.verification_profile:
            findings.append(DryRunFinding(f"RDF-{preview.task_id}-VERIFY", "ERROR", preview.task_id, "runtime import preview lacks verification profile"))


def _write_scope_conflicts(plan: ResidualImplementationPlan) -> tuple[str, ...]:
    conflicts: set[str] = set()
    by_id = {task.id: task for task in plan.tasks}
    for wave in plan.waves:
        task_ids = list(wave["task_ids"])
        for index, task_id in enumerate(task_ids):
            left = set(by_id[task_id].write_scope["allowed"])
            for other_id in task_ids[index + 1:]:
                overlap = left & set(by_id[other_id].write_scope["allowed"])
                if overlap:
                    conflicts.add(f"{wave['id']}:{task_id}:{other_id}:{','.join(sorted(overlap))}")
    return tuple(sorted(conflicts))


def _acceptance_state(
    findings: list[DryRunFinding],
    execution_prerequisites: tuple[str, ...],
    feasibility: ResidualFeasibilityEvaluation,
) -> ResidualPlanAcceptanceState:
    if any(finding.severity == "ERROR" for finding in findings):
        return "BLOCKED" if feasibility.plan_status == "BLOCKED" else "INVALID"
    if execution_prerequisites:
        return "READY_WITH_EXECUTION_PREREQUISITES"
    return "READY_FOR_RESIDUAL_PLAN_ACCEPTANCE"


def _prior_plan_coexistence() -> tuple[str, ...]:
    return (
        "PLAN-1a75a2e3c5a7 v1 remains completed historical evidence",
        "residual import must not reopen completed tasks or mutate their fingerprints",
        "residual import must not overwrite execution history or historical gate approvals",
    )


def _recommended_limits(effective_width: int) -> dict[str, int]:
    return {
        "max_tasks_per_run": 1,
        "max_wall_clock_seconds": 1800,
        "max_failures_per_run": 1,
        "max_repairs_per_task": 1,
        "effective_concurrency": effective_width,
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(canonical_bytes(value) + b"\n")
