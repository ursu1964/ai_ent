from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ai_ent.project_manifest import GENERATED_MARKER, canonical_bytes
from ai_ent.residual_dag import ResidualImplementationPlan, generate_residual_implementation_plan
from ai_ent.residual_dry_run import ResidualDryRun, dry_run_residual_plan, freeze_residual_plan
from ai_ent.residual_feasibility import ResidualFeasibilityEvaluation, evaluate_residual_feasibility

RPG_GATE_ID = "RPG-001"
RPG_ACCEPTANCE_VERSION = "rpg-001.1"
ResidualGateResult = Literal["ACCEPTED", "ACCEPTED_WITH_PREREQUISITES", "REJECTED"]


@dataclass(frozen=True)
class ResidualPlanAcceptance:
    gate_id: str
    gate_version: str
    generated_at: str
    baseline_commit: str
    residual_plan_id: str
    plan_version: str
    gate_result: ResidualGateResult
    recommendation: str
    bound_hashes: dict[str, str]
    counts: dict[str, int]
    risk_distribution: dict[str, int]
    human_gates: tuple[dict[str, Any], ...]
    execution_prerequisites: tuple[str, ...]
    validations: dict[str, str]
    defects: tuple[str, ...]
    limitations: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.gate_result in {"ACCEPTED", "ACCEPTED_WITH_PREREQUISITES"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "gate_id": self.gate_id,
            "gate_version": self.gate_version,
            "generated_at": self.generated_at,
            "baseline_commit": self.baseline_commit,
            "residual_plan_id": self.residual_plan_id,
            "plan_version": self.plan_version,
            "gate_result": self.gate_result,
            "recommendation": self.recommendation,
            "bound_hashes": self.bound_hashes,
            "counts": self.counts,
            "risk_distribution": self.risk_distribution,
            "human_gates": list(self.human_gates),
            "execution_prerequisites": list(self.execution_prerequisites),
            "validations": self.validations,
            "defects": list(self.defects),
            "limitations": list(self.limitations),
        }


def accept_residual_plan(
    *,
    plan: ResidualImplementationPlan | None = None,
    feasibility: ResidualFeasibilityEvaluation | None = None,
    dry_run: ResidualDryRun | None = None,
    repository_root: Path = Path("."),
    manifest_root: Path = Path("manifest/project/ai-ent"),
    pir_artifact: Path = Path("artifacts/pir-001/PIR-001.json"),
    expected_residual_plan_hash: str | None = None,
) -> ResidualPlanAcceptance:
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
    residual_dry_run = dry_run or dry_run_residual_plan(
        plan=residual_plan,
        feasibility=residual_feasibility,
        repository_root=repository_root,
        manifest_root=manifest_root,
        pir_artifact=pir_artifact,
        expected_residual_plan_hash=expected_residual_plan_hash,
    )
    validations = _validations(residual_plan, residual_feasibility, residual_dry_run, expected_residual_plan_hash)
    defects = tuple(sorted(name for name, status in validations.items() if status != "PASS"))
    prerequisites = residual_dry_run.execution_prerequisites
    if defects:
        gate_result: ResidualGateResult = "REJECTED"
        recommendation = "REMEDIATION_REQUIRED"
    elif prerequisites:
        gate_result = "ACCEPTED_WITH_PREREQUISITES"
        recommendation = "READY_FOR_RESIDUAL_RUNTIME_IMPORT_WITH_EXECUTION_PREREQUISITES"
    else:
        gate_result = "ACCEPTED"
        recommendation = "READY_FOR_RESIDUAL_RUNTIME_IMPORT"
    limitations = tuple(f"Execution prerequisite: {item}" for item in prerequisites)
    return ResidualPlanAcceptance(
        gate_id=RPG_GATE_ID,
        gate_version=RPG_ACCEPTANCE_VERSION,
        generated_at=dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        baseline_commit=residual_dry_run.baseline_head,
        residual_plan_id=residual_dry_run.lock.residual_plan_id,
        plan_version=residual_dry_run.lock.plan_version,
        gate_result=gate_result,
        recommendation=recommendation,
        bound_hashes=_bound_hashes(residual_dry_run),
        counts=_counts(residual_plan, residual_feasibility, residual_dry_run),
        risk_distribution=_risk_distribution(residual_plan),
        human_gates=residual_dry_run.lock.human_gate_definitions,
        execution_prerequisites=prerequisites,
        validations=validations,
        defects=defects,
        limitations=limitations,
    )


def write_residual_plan_acceptance(
    *,
    artifacts_dir: Path = Path("artifacts/rpg-001"),
    output_dir: Path = Path(".build/compiled"),
    repository_root: Path = Path("."),
    manifest_root: Path = Path("manifest/project/ai-ent"),
    pir_artifact: Path = Path("artifacts/pir-001/PIR-001.json"),
    expected_residual_plan_hash: str | None = None,
) -> ResidualPlanAcceptance:
    del output_dir
    dry_run = freeze_residual_plan(
        repository_root=repository_root,
        manifest_root=manifest_root,
        pir_artifact=pir_artifact,
        expected_residual_plan_hash=expected_residual_plan_hash,
    )
    gate = accept_residual_plan(
        dry_run=dry_run,
        repository_root=repository_root,
        manifest_root=manifest_root,
        pir_artifact=pir_artifact,
        expected_residual_plan_hash=expected_residual_plan_hash,
    )
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "RPG-001.json").write_bytes(canonical_bytes(gate.as_dict()) + b"\n")
    (artifacts_dir / "RPG-001.md").write_text(_markdown(gate), encoding="utf-8")
    return gate


def _validations(
    plan: ResidualImplementationPlan,
    feasibility: ResidualFeasibilityEvaluation,
    dry_run: ResidualDryRun,
    expected_residual_plan_hash: str | None,
) -> dict[str, str]:
    task_ids = {task.id for task in plan.tasks}
    preview_ids = {preview.task_id for preview in dry_run.import_preview}
    fingerprint_by_task = {task.id: task.fingerprint for task in plan.tasks}
    preview_fingerprints = {preview.task_id: preview.fingerprint for preview in dry_run.import_preview}
    return {
        "expected_residual_plan_hash": _pass(expected_residual_plan_hash is None or plan.residual_plan_hash == expected_residual_plan_hash),
        "state_frozen": _pass(dry_run.frozen_plan_state == "FROZEN" and dry_run.lock.state == "FROZEN"),
        "residual_plan_id_version": _pass(dry_run.lock.residual_plan_id == f"RESIDUAL-PLAN-{plan.residual_plan_hash[:12]}" and dry_run.lock.plan_version == "1"),
        "tasks_7": _pass(len(plan.tasks) == 7 and dry_run.task_count == 7),
        "edges_6": _pass(len(plan.dependency_edges) == 6 and dry_run.dependency_edge_count == 6),
        "waves_6": _pass(len(plan.waves) == 6 and dry_run.wave_count == 6),
        "effective_concurrency_1": _pass(dry_run.effective_parallel_width == 1 and dry_run.lock.effective_concurrency == 1),
        "theoretical_width_2": _pass(dry_run.theoretical_parallel_width == 2),
        "human_gates_2": _pass(len(plan.human_gates) == 2 and len(dry_run.lock.human_gate_definitions) == 2),
        "dry_run_clean": _pass(not any(finding.severity in {"ERROR", "WARNING"} for finding in dry_run.findings)),
        "technical_blockers_zero": _pass(not feasibility.technical_blockers),
        "task_fingerprints_match_lock": _pass(dry_run.lock.task_fingerprints == fingerprint_by_task),
        "task_fingerprints_match_preview": _pass(preview_fingerprints == fingerprint_by_task),
        "task_ids_match_preview": _pass(task_ids == preview_ids),
        "hash_chain_matches": _pass(
            plan.residual_plan_hash == feasibility.residual_plan_hash
            and plan.residual_plan_hash == dry_run.residual_plan_hash
            and feasibility.residual_feasibility_hash == dry_run.residual_feasibility_hash
            and dry_run.residual_dry_run_hash == dry_run.lock.residual_dry_run_hash
        ),
        "import_preview_schema_compatible": _pass(
            all(
                preview.task_id
                and preview.fingerprint
                and preview.execution_class == "residual_implementation"
                and preview.verification_profile
                and preview.agent_role
                and preview.model_profile
                and preview.executor
                for preview in dry_run.import_preview
            )
        ),
        "prior_plan_coexistence_validated": _pass(bool(dry_run.prior_plan_coexistence)),
        "guarded_runner_compatible": _pass(dry_run.effective_parallel_width == 1 and dry_run.recommended_limits["max_tasks_per_run"] == 1),
        "recovery_compatible": _pass(
            {
                "after_claim",
                "after_worktree_created",
                "after_executor",
                "after_verification",
                "after_verified_commit",
                "before_db_completion",
                "between_tasks",
                "at_human_gates",
            }
            <= set(dry_run.recovery_points)
        ),
        "execution_prerequisite_enforced": _pass(
            not dry_run.execution_prerequisites
            or "AIENT_CODEX_COMMAND must be configured before runtime import/execution" in dry_run.execution_prerequisites
        ),
        "no_runtime_import_or_execution_performed": "PASS",
    }


def _bound_hashes(dry_run: ResidualDryRun) -> dict[str, str]:
    return {
        "post_compiled_hash": dry_run.post_compiled_hash,
        "post_capability_hash": dry_run.post_capability_hash,
        "post_trace_hash": dry_run.post_trace_hash,
        "pir_residual_gap_hash": dry_run.pir_residual_gap_hash,
        "residual_plan_hash": dry_run.residual_plan_hash,
        "residual_feasibility_hash": dry_run.residual_feasibility_hash,
        "residual_dry_run_hash": dry_run.residual_dry_run_hash,
        "dependency_graph_hash": dry_run.lock.dependency_graph_hash,
        "task_fingerprint_hash": hashlib.sha256(canonical_bytes(dry_run.lock.task_fingerprints)).hexdigest(),
    }


def _counts(
    plan: ResidualImplementationPlan,
    feasibility: ResidualFeasibilityEvaluation,
    dry_run: ResidualDryRun,
) -> dict[str, int]:
    summary = feasibility.as_dict()["summary"]
    return {
        "tasks": len(plan.tasks),
        "implementation_tasks": dry_run.implementation_task_count,
        "evidence_tasks": dry_run.evidence_task_count,
        "verification_tasks": dry_run.verification_task_count,
        "dependency_edges": len(plan.dependency_edges),
        "waves": len(plan.waves),
        "human_gates": len(plan.human_gates),
        "execution_batches": len(dry_run.execution_batches),
        "effective_concurrency": dry_run.effective_parallel_width,
        "theoretical_parallel_width": dry_run.theoretical_parallel_width,
        "feasible_with_conditions": int(summary["feasible_with_conditions"]),
        "human_approval_required": int(summary["human_approval_required"]),
        "blocked": int(summary["blocked"]),
        "prohibited": int(summary["prohibited"]),
        "technical_blockers": int(summary["technical_blockers"]),
        "dry_run_errors": len([finding for finding in dry_run.findings if finding.severity == "ERROR"]),
        "dry_run_warnings": len([finding for finding in dry_run.findings if finding.severity == "WARNING"]),
    }


def _risk_distribution(plan: ResidualImplementationPlan) -> dict[str, int]:
    distribution: dict[str, int] = {}
    for task in plan.tasks:
        risk = task.risk["level"]
        distribution[risk] = distribution.get(risk, 0) + 1
    return dict(sorted(distribution.items()))


def _pass(condition: bool) -> str:
    return "PASS" if condition else "FAIL"


def _markdown(gate: ResidualPlanAcceptance) -> str:
    lines = [
        "# RPG-001 - Residual Plan Acceptance Gate",
        "",
        f"Result: {gate.gate_result}",
        f"Recommendation: {gate.recommendation}",
        f"Baseline commit: `{gate.baseline_commit}`",
        f"Residual plan: `{gate.residual_plan_id}` version `{gate.plan_version}`",
        "",
        "## Counts",
    ]
    lines.extend(f"- {key}: {value}" for key, value in gate.counts.items())
    lines.extend(["", "## Bound Hashes"])
    lines.extend(f"- {key}: `{value}`" for key, value in gate.bound_hashes.items())
    lines.extend(["", "## Execution Prerequisites"])
    if gate.execution_prerequisites:
        lines.extend(f"- {item}" for item in gate.execution_prerequisites)
    else:
        lines.append("- none")
    lines.extend(["", "## Human Gates"])
    lines.extend(f"- {gate_def['id']} -> {gate_def['task_id']}: {gate_def['reason']}" for gate_def in gate.human_gates)
    lines.extend(["", "## Validations"])
    lines.extend(f"- {key}: {value}" for key, value in gate.validations.items())
    if gate.limitations:
        lines.extend(["", "## Limitations"])
        lines.extend(f"- {item}" for item in gate.limitations)
    if gate.defects:
        lines.extend(["", "## Defects"])
        lines.extend(f"- {item}" for item in gate.defects)
    return "\n".join(lines) + "\n"


def load_residual_plan_acceptance(path: Path = Path("artifacts/rpg-001/RPG-001.json")) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("gate_id") != RPG_GATE_ID:
        raise ValueError(f"{path} is not an RPG-001 artifact")
    return payload
