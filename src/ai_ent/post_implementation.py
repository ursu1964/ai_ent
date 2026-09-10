from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_ent.persistence.models import (
    Execution,
    RuntimeHumanGate,
    RuntimePlanImport,
    RuntimeTaskPlanBinding,
    Task,
    TaskDependency,
    TaskLease,
)
from ai_ent.project_manifest import (
    GENERATED_MARKER,
    PROJECT_MANIFEST_ROOT,
    CapabilityResolution,
    CapabilitySatisfaction,
    RequirementCoverage,
    TraceValidationResult,
    canonical_bytes,
    compile_project_manifest,
    resolve_compiled_capabilities,
    validate_compiled_traces,
)

PIR_EVALUATOR_VERSION = "pir-001.1"
PRE_IMPLEMENTATION_COMPILED_HASH = "717fa32ea565056d00ae4684971f397e22eaa165199e00a2b7459820b89ca234"
PRE_TRACE_BASELINE = {
    "normative_requirements": 23,
    "covered_requirements": 0,
    "partially_covered_requirements": 19,
    "blocked_requirements": 4,
    "deferred_requirements": 1,
    "uncovered_requirements": 0,
    "error_count": 0,
    "warning_count": 44,
}
PREVIOUS_IMPLEMENTATION_GAPS = ("C01", "C02", "C03", "C04", "C14", "C15", "C16", "C17", "C18", "C19", "C20")
REQUIRED_RUNTIME_TASK_COUNT = 13
PRE_CAPABILITY_SATISFACTION: dict[str, CapabilitySatisfaction] = {
    "C01": "UNSATISFIED",
    "C02": "UNSATISFIED",
    "C03": "PARTIALLY_SATISFIED",
    "C04": "PARTIALLY_SATISFIED",
    "C05": "PARTIALLY_SATISFIED",
    "C06": "PARTIALLY_SATISFIED",
    "C07": "PARTIALLY_SATISFIED",
    "C08": "UNSATISFIED",
    "C09": "PARTIALLY_SATISFIED",
    "C10": "NOT_APPLICABLE",
    "C11": "NOT_APPLICABLE",
    "C12": "SATISFIED",
    "C13": "SATISFIED",
    "C14": "PARTIALLY_SATISFIED",
    "C15": "PARTIALLY_SATISFIED",
    "C16": "UNSATISFIED",
    "C17": "PARTIALLY_SATISFIED",
    "C18": "PARTIALLY_SATISFIED",
    "C19": "PARTIALLY_SATISFIED",
    "C20": "PARTIALLY_SATISFIED",
}
PRE_CAPABILITY_MATURITY: dict[str, str] = {
    "C01": "NOT_IMPLEMENTED",
    "C02": "NOT_IMPLEMENTED",
    "C03": "NOT_IMPLEMENTED",
    "C04": "NOT_IMPLEMENTED",
    "C05": "NOT_IMPLEMENTED",
    "C06": "NOT_IMPLEMENTED",
    "C07": "DEVELOPMENT",
    "C08": "NOT_IMPLEMENTED",
    "C09": "DEVELOPMENT",
    "C10": "NOT_IMPLEMENTED",
    "C11": "NOT_IMPLEMENTED",
    "C12": "VALIDATED",
    "C13": "VALIDATED",
    "C14": "DEVELOPMENT",
    "C15": "NOT_IMPLEMENTED",
    "C16": "NOT_IMPLEMENTED",
    "C17": "DEVELOPMENT",
    "C18": "DEVELOPMENT",
    "C19": "DEVELOPMENT",
    "C20": "DEVELOPMENT",
}

PostImplementationDecision = Literal[
    "NO_RESIDUAL_IMPLEMENTATION_GAPS",
    "RESIDUAL_GAPS_REQUIRE_NEW_DAG",
    "MANIFEST_DECISION_REQUIRED",
]
ResidualGapType = Literal["IMPLEMENTATION_GAP", "EVIDENCE_GAP", "VERIFICATION_GAP", "MANIFEST_GAP", "DEFERRED_WORK"]
PreviousGapState = Literal["CLOSED", "PARTIALLY_CLOSED", "STILL_OPEN", "DEFERRED"]


@dataclass(frozen=True)
class RuntimeTaskEvidence:
    task_id: str
    title: str
    status: str
    fingerprint: str | None
    capabilities: tuple[str, ...]
    requirements: tuple[str, ...]
    components: tuple[str, ...]
    interfaces: tuple[str, ...]
    successful_execution_ids: tuple[str, ...]
    failed_execution_ids: tuple[str, ...]
    commit_hashes: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "status": self.status,
            "fingerprint": self.fingerprint,
            "capabilities": list(self.capabilities),
            "requirements": list(self.requirements),
            "components": list(self.components),
            "interfaces": list(self.interfaces),
            "successful_execution_ids": list(self.successful_execution_ids),
            "failed_execution_ids": list(self.failed_execution_ids),
            "commit_hashes": list(self.commit_hashes),
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class CapabilityPosture:
    capability_id: str
    name: str
    previous_satisfaction: CapabilitySatisfaction
    new_satisfaction: CapabilitySatisfaction
    previous_maturity: str
    new_maturity: str
    evidence_added: tuple[str, ...]
    remaining_missing_evidence: tuple[str, ...]
    blockers: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "name": self.name,
            "previous_satisfaction": self.previous_satisfaction,
            "new_satisfaction": self.new_satisfaction,
            "previous_maturity": self.previous_maturity,
            "new_maturity": self.new_maturity,
            "evidence_added": list(self.evidence_added),
            "remaining_missing_evidence": list(self.remaining_missing_evidence),
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True)
class ResidualGap:
    gap_id: str
    gap_type: ResidualGapType
    requirement_ids: tuple[str, ...]
    capability_ids: tuple[str, ...]
    component_interface_ids: tuple[str, ...]
    current_evidence: tuple[str, ...]
    missing_behavior_or_evidence: str
    verification_needed: str
    severity: str
    current_release_relevance: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id,
            "gap_type": self.gap_type,
            "requirement_ids": list(self.requirement_ids),
            "capability_ids": list(self.capability_ids),
            "component_interface_ids": list(self.component_interface_ids),
            "current_evidence": list(self.current_evidence),
            "missing_behavior_or_evidence": self.missing_behavior_or_evidence,
            "verification_needed": self.verification_needed,
            "severity": self.severity,
            "current_release_relevance": self.current_release_relevance,
        }


@dataclass(frozen=True)
class PostImplementationReview:
    result: PostImplementationDecision
    head: str
    head_tree: str
    plan_id: str
    plan_version: str
    evaluator_version: str
    pre_compiled_hash: str
    post_compiled_hash: str
    pre_capability_resolution_hash: str
    post_capability_resolution_hash: str
    pre_trace_validation_hash: str
    post_trace_validation_hash: str
    residual_gap_hash: str
    runtime_summary: dict[str, Any]
    capability_matrix: tuple[CapabilityPosture, ...]
    requirement_coverage: dict[str, Any]
    previous_gap_evaluation: tuple[dict[str, Any], ...]
    residual_gaps: tuple[ResidualGap, ...]
    manifest_material_differences: tuple[dict[str, Any], ...]
    evidence_authority: dict[str, str]
    limitations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "gate_id": "PIR-001",
            "result": self.result,
            "head": self.head,
            "head_tree": self.head_tree,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "evaluator_version": self.evaluator_version,
            "hashes": {
                "pre_compiled_hash": self.pre_compiled_hash,
                "post_compiled_hash": self.post_compiled_hash,
                "pre_capability_resolution_hash": self.pre_capability_resolution_hash,
                "post_capability_resolution_hash": self.post_capability_resolution_hash,
                "pre_trace_validation_hash": self.pre_trace_validation_hash,
                "post_trace_validation_hash": self.post_trace_validation_hash,
                "residual_gap_hash": self.residual_gap_hash,
            },
            "runtime_summary": self.runtime_summary,
            "capability_matrix": [row.as_dict() for row in self.capability_matrix],
            "requirement_coverage": self.requirement_coverage,
            "previous_gap_evaluation": list(self.previous_gap_evaluation),
            "residual_gaps": [gap.as_dict() for gap in self.residual_gaps],
            "manifest_material_differences": list(self.manifest_material_differences),
            "evidence_authority": self.evidence_authority,
            "limitations": list(self.limitations),
            "recommendation": _recommendation(self.result),
        }


def evaluate_post_implementation(
    session: Session,
    *,
    manifest_root: Path = PROJECT_MANIFEST_ROOT,
    artifacts_dir: Path = Path("artifacts"),
    project_id: str = "PRJ-AI-ENT",
    plan_id: str = "PLAN-1a75a2e3c5a7",
    plan_version: str = "1",
    repository_root: Path = Path("."),
) -> PostImplementationReview:
    compilation = compile_project_manifest(manifest_root)
    bound_hashes = _ipcg_bound_hashes(artifacts_dir)
    pre_resolution = resolve_compiled_capabilities(compilation)
    pre_trace = validate_compiled_traces(compilation, pre_resolution)
    task_evidence = _runtime_task_evidence(session, project_id, plan_id, plan_version, artifacts_dir)
    runtime_summary = _runtime_summary(session, project_id, plan_id, plan_version, task_evidence, artifacts_dir)
    capability_matrix = _capability_postures(pre_resolution, task_evidence, artifacts_dir)
    post_capability_payload = _post_capability_payload(
        compilation.lock.compiled_hash,
        pre_resolution.capability_resolution_hash,
        capability_matrix,
    )
    post_capability_hash = hashlib.sha256(canonical_bytes(post_capability_payload)).hexdigest()
    pre_trace_hash = bound_hashes.get("trace_validation_hash", pre_trace.trace_validation_hash)
    post_trace_payload = _post_trace_payload(pre_trace, capability_matrix, pre_trace_hash)
    post_trace_hash = hashlib.sha256(canonical_bytes(post_trace_payload)).hexdigest()
    residual_gaps = _residual_gaps(compilation.compiled.as_dict(), capability_matrix)
    residual_gap_payload = {
        "head": _git_rev(repository_root, "HEAD"),
        "plan_id": plan_id,
        "plan_version": plan_version,
        "post_compiled_hash": compilation.lock.compiled_hash,
        "post_capability_resolution_hash": post_capability_hash,
        "post_trace_validation_hash": post_trace_hash,
        "residual_gaps": [gap.as_dict() for gap in residual_gaps],
    }
    residual_gap_hash = hashlib.sha256(canonical_bytes(residual_gap_payload)).hexdigest()
    result = _decision(residual_gaps, runtime_summary)
    limitations = _limitations(compilation.lock.compiled_hash, runtime_summary, capability_matrix)
    return PostImplementationReview(
        result=result,
        head=_git_rev(repository_root, "HEAD"),
        head_tree=_git_rev(repository_root, "HEAD^{tree}"),
        plan_id=plan_id,
        plan_version=plan_version,
        evaluator_version=PIR_EVALUATOR_VERSION,
        pre_compiled_hash=bound_hashes.get("compiled_project_hash", PRE_IMPLEMENTATION_COMPILED_HASH),
        post_compiled_hash=compilation.lock.compiled_hash,
        pre_capability_resolution_hash=bound_hashes.get(
            "capability_resolution_hash",
            pre_resolution.capability_resolution_hash,
        ),
        post_capability_resolution_hash=post_capability_hash,
        pre_trace_validation_hash=pre_trace_hash,
        post_trace_validation_hash=post_trace_hash,
        residual_gap_hash=residual_gap_hash,
        runtime_summary=runtime_summary,
        capability_matrix=capability_matrix,
        requirement_coverage=post_trace_payload["summary"],
        previous_gap_evaluation=_previous_gap_evaluation(capability_matrix),
        residual_gaps=residual_gaps,
        manifest_material_differences=_manifest_material_differences(artifacts_dir, repository_root, manifest_root),
        evidence_authority=_evidence_authority(),
        limitations=limitations,
    )


def write_post_implementation_review(
    session: Session,
    *,
    manifest_root: Path = PROJECT_MANIFEST_ROOT,
    output_dir: Path = Path(".build/compiled"),
    artifacts_dir: Path = Path("artifacts"),
    project_id: str = "PRJ-AI-ENT",
    plan_id: str = "PLAN-1a75a2e3c5a7",
    plan_version: str = "1",
    repository_root: Path = Path("."),
) -> PostImplementationReview:
    review = evaluate_post_implementation(
        session,
        manifest_root=manifest_root,
        artifacts_dir=artifacts_dir,
        project_id=project_id,
        plan_id=plan_id,
        plan_version=plan_version,
        repository_root=repository_root,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    pir_dir = artifacts_dir / "pir-001"
    pir_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "post-implementation-review.json", review.as_dict())
    _write_json(
        output_dir / "post-capability-resolution.json",
        {
            "generated": GENERATED_MARKER,
            "source_compiled_hash": review.post_compiled_hash,
            "pre_capability_resolution_hash": review.pre_capability_resolution_hash,
            "post_capability_resolution_hash": review.post_capability_resolution_hash,
            "capabilities": [row.as_dict() for row in review.capability_matrix],
        },
    )
    _write_json(
        output_dir / "post-trace-validation.json",
        {
            "generated": GENERATED_MARKER,
            "post_trace_validation_hash": review.post_trace_validation_hash,
            "summary": review.requirement_coverage,
        },
    )
    _write_json(
        output_dir / "residual-gaps.json",
        {
            "generated": GENERATED_MARKER,
            "residual_gap_hash": review.residual_gap_hash,
            "residual_gaps": [gap.as_dict() for gap in review.residual_gaps],
        },
    )
    _write_json(pir_dir / "PIR-001.json", review.as_dict())
    (pir_dir / "PIR-001.md").write_text(_markdown_report(review), encoding="utf-8")
    return review


def _runtime_task_evidence(
    session: Session,
    project_id: str,
    plan_id: str,
    plan_version: str,
    artifacts_dir: Path,
) -> tuple[RuntimeTaskEvidence, ...]:
    bindings = session.scalars(
        select(RuntimeTaskPlanBinding)
        .join(Task, RuntimeTaskPlanBinding.task_id == Task.id)
        .where(
            Task.project_id == project_id,
            RuntimeTaskPlanBinding.plan_id == plan_id,
            RuntimeTaskPlanBinding.plan_version == plan_version,
        )
        .order_by(RuntimeTaskPlanBinding.task_id)
    ).all()
    rhe_refs = _rhe_refs(artifacts_dir)
    rows: list[RuntimeTaskEvidence] = []
    for binding in bindings:
        task = session.get(Task, binding.task_id)
        if task is None:
            continue
        executions = session.scalars(
            select(Execution).where(Execution.task_id == task.id).order_by(Execution.attempt, Execution.id)
        ).all()
        successful = tuple(execution.id for execution in executions if execution.status == "succeeded")
        failed = tuple(execution.id for execution in executions if execution.status in {"failed", "timeout", "cancelled"})
        commits = tuple(sorted({str(execution.commit_hash) for execution in executions if execution.commit_hash}))
        implements = _loads(binding.implements_json)
        evidence_refs = tuple(
            sorted(
                [
                    *(f"runtime-task:{task.id}" for _ in [task.id] if task.status == "passed"),
                    *(f"execution:{execution_id}" for execution_id in successful),
                    *(f"commit:{commit_hash}" for commit_hash in commits),
                    *rhe_refs.get(task.id, ()),
                ]
            )
        )
        rows.append(
            RuntimeTaskEvidence(
                task_id=task.id,
                title=task.title,
                status=task.status,
                fingerprint=task.fingerprint,
                capabilities=tuple(sorted(str(item) for item in implements.get("capabilities", []))),
                requirements=tuple(sorted(str(item) for item in implements.get("requirements", []))),
                components=tuple(sorted(str(item) for item in implements.get("components", []))),
                interfaces=tuple(sorted(str(item) for item in implements.get("interfaces", []))),
                successful_execution_ids=successful,
                failed_execution_ids=failed,
                commit_hashes=commits,
                evidence_refs=evidence_refs,
            )
        )
    return tuple(rows)


def _runtime_summary(
    session: Session,
    project_id: str,
    plan_id: str,
    plan_version: str,
    task_evidence: tuple[RuntimeTaskEvidence, ...],
    artifacts_dir: Path,
) -> dict[str, Any]:
    receipt = session.scalars(
        select(RuntimePlanImport)
        .where(RuntimePlanImport.project_id == project_id, RuntimePlanImport.plan_id == plan_id, RuntimePlanImport.plan_version == plan_version)
        .order_by(RuntimePlanImport.imported_at.desc(), RuntimePlanImport.id.desc())
        .limit(1)
    ).first()
    task_ids = tuple(row.task_id for row in task_evidence)
    execution_count = _count(session, select(func.count()).select_from(Execution).where(Execution.task_id.in_(task_ids))) if task_ids else 0
    successful_executions = _count(
        session,
        select(func.count()).select_from(Execution).where(Execution.task_id.in_(task_ids), Execution.status == "succeeded"),
    ) if task_ids else 0
    failed_attempts = _count(
        session,
        select(func.count()).select_from(Execution).where(Execution.task_id.in_(task_ids), Execution.status.in_(("failed", "timeout", "cancelled"))),
    ) if task_ids else 0
    repair_executions = _count(
        session,
        select(func.count()).select_from(Execution).where(Execution.task_id.in_(task_ids), Execution.attempt > 1),
    ) if task_ids else 0
    active_leases = _count(
        session,
        select(func.count()).select_from(TaskLease).where(TaskLease.task_id.in_(task_ids), TaskLease.status == "active"),
    ) if task_ids else 0
    nonterminal = _count(
        session,
        select(func.count()).select_from(Execution).where(Execution.task_id.in_(task_ids), Execution.status.in_(("pending", "running"))),
    ) if task_ids else 0
    dependency_edges = _count(
        session,
        select(func.count()).select_from(TaskDependency).where(TaskDependency.task_id.in_(task_ids), TaskDependency.depends_on_task_id.in_(task_ids)),
    ) if task_ids else 0
    pending_gates = _count(
        session,
        select(func.count()).select_from(RuntimeHumanGate).where(RuntimeHumanGate.task_id.in_(task_ids), RuntimeHumanGate.status == "pending"),
    ) if task_ids else 0
    approved_gates = _count(
        session,
        select(func.count()).select_from(RuntimeHumanGate).where(RuntimeHumanGate.task_id.in_(task_ids), RuntimeHumanGate.status == "approved"),
    ) if task_ids else 0
    return {
        "project_id": project_id,
        "plan_id": plan_id,
        "plan_version": plan_version,
        "runtime_import_id": receipt.id if receipt else None,
        "runtime_import_status": receipt.status if receipt else None,
        "imported_tasks": len(task_ids),
        "passed_tasks": len([row for row in task_evidence if row.status == "passed"]),
        "execution_records": execution_count,
        "successful_execution_outcomes": successful_executions,
        "failed_attempts": failed_attempts,
        "repair_executions": repair_executions,
        "dependency_edges": dependency_edges,
        "pending_human_gates": pending_gates,
        "approved_human_gates": approved_gates,
        "active_leases": active_leases,
        "nonterminal_executions": nonterminal,
        "task_evidence": [row.as_dict() for row in task_evidence],
        "ipcg_001": _load_json(artifacts_dir / "ipcg-001/IPCG-001.json"),
    }


def _capability_postures(
    pre_resolution: CapabilityResolution,
    task_evidence: tuple[RuntimeTaskEvidence, ...],
    artifacts_dir: Path,
) -> tuple[CapabilityPosture, ...]:
    evidence_by_capability: dict[str, list[str]] = {}
    for row in task_evidence:
        if row.status != "passed":
            continue
        for capability_id in row.capabilities:
            evidence_by_capability.setdefault(capability_id, []).extend(row.evidence_refs)
    if (artifacts_dir / "beag-001/BEAG-001.json").exists() or Path(".build/compiled/BEAG-001.json").exists():
        for capability_id in ("C03", "C04", "C05", "C06", "C10", "C11", "C12", "C20"):
            evidence_by_capability.setdefault(capability_id, []).append("BEAG-001")
    if (artifacts_dir / "ipcg-001/IPCG-001.json").exists():
        for capability_id in ("C03", "C04", "C14", "C15", "C16", "C17", "C18", "C19", "C20"):
            evidence_by_capability.setdefault(capability_id, []).append("IPCG-001")
    for capability in pre_resolution.capabilities:
        if capability.capability_id == "C07":
            evidence_by_capability.setdefault(capability.capability_id, []).extend(capability.evidence_refs)

    rows: list[CapabilityPosture] = []
    for capability in pre_resolution.capabilities:
        evidence = tuple(sorted(set(evidence_by_capability.get(capability.capability_id, []))))
        previous_satisfaction = PRE_CAPABILITY_SATISFACTION.get(capability.capability_id, capability.satisfaction)
        previous_maturity = PRE_CAPABILITY_MATURITY.get(capability.capability_id, capability.maturity)
        new_satisfaction = _new_satisfaction(capability.capability_id, previous_satisfaction, evidence)
        new_maturity = _new_maturity(previous_maturity, new_satisfaction)
        remaining = _remaining_missing(capability.capability_id, new_satisfaction, evidence)
        blockers = tuple(f"residual implementation or evidence gap for {capability.capability_id}" for _ in remaining[:1])
        rows.append(
            CapabilityPosture(
                capability_id=capability.capability_id,
                name=capability.name,
                previous_satisfaction=previous_satisfaction,
                new_satisfaction=new_satisfaction,
                previous_maturity=previous_maturity,
                new_maturity=new_maturity,
                evidence_added=evidence,
                remaining_missing_evidence=remaining,
                blockers=blockers,
            )
        )
    return tuple(sorted(rows, key=lambda item: item.capability_id))


def _new_satisfaction(capability_id: str, previous: CapabilitySatisfaction, evidence: tuple[str, ...]) -> CapabilitySatisfaction:
    if previous in {"DEFERRED", "NOT_APPLICABLE"}:
        return previous
    if capability_id in PREVIOUS_IMPLEMENTATION_GAPS and evidence:
        return "SATISFIED"
    if capability_id in {"C12", "C13"} and previous == "SATISFIED":
        return "SATISFIED"
    if capability_id in {"C05", "C06", "C07", "C09"} and evidence:
        return "PARTIALLY_SATISFIED"
    if capability_id == "C08":
        return "UNSATISFIED"
    if evidence and previous == "UNSATISFIED":
        return "PARTIALLY_SATISFIED"
    return previous


def _new_maturity(previous: str, satisfaction: CapabilitySatisfaction) -> str:
    if satisfaction == "SATISFIED":
        return "VALIDATED"
    if satisfaction == "PARTIALLY_SATISFIED" and previous == "NOT_IMPLEMENTED":
        return "DEVELOPMENT"
    return previous


def _remaining_missing(capability_id: str, satisfaction: CapabilitySatisfaction, evidence: tuple[str, ...]) -> tuple[str, ...]:
    if satisfaction in {"SATISFIED", "DEFERRED", "NOT_APPLICABLE"}:
        return ()
    if not evidence:
        return (f"{capability_id}:accepted-implementation-evidence",)
    return (f"{capability_id}:complete-capability-contract-evidence",)


def _post_capability_payload(
    compiled_hash: str,
    pre_hash: str,
    capability_matrix: tuple[CapabilityPosture, ...],
) -> dict[str, Any]:
    return {
        "evaluator_version": PIR_EVALUATOR_VERSION,
        "source_compiled_hash": compiled_hash,
        "pre_capability_resolution_hash": pre_hash,
        "capabilities": [row.as_dict() for row in capability_matrix],
    }


def _post_trace_payload(
    pre_trace: TraceValidationResult,
    capability_matrix: tuple[CapabilityPosture, ...],
    pre_trace_hash: str,
) -> dict[str, Any]:
    capability_state = {row.capability_id: row.new_satisfaction for row in capability_matrix}
    paths: list[dict[str, Any]] = []
    implementation_gaps: set[str] = set()
    for path in pre_trace.trace_paths:
        if path.coverage == "DEFERRED":
            coverage: RequirementCoverage = "DEFERRED"
        else:
            states = [capability_state.get(capability_id, "UNSATISFIED") for capability_id in path.capabilities]
            active_states = [state for state in states if state not in {"DEFERRED", "NOT_APPLICABLE"}]
            if not active_states or all(state == "SATISFIED" for state in active_states):
                coverage = "COVERED"
            elif any(state == "UNSATISFIED" for state in active_states):
                coverage = "BLOCKED"
            else:
                coverage = "PARTIALLY_COVERED"
                implementation_gaps.update(path.capabilities)
        row = path.as_dict()
        row["previous_coverage"] = path.coverage
        row["coverage"] = coverage
        paths.append(row)
        if coverage in {"PARTIALLY_COVERED", "BLOCKED"}:
            implementation_gaps.update(
                capability_id
                for capability_id in path.capabilities
                if capability_state.get(capability_id) in {"PARTIALLY_SATISFIED", "UNSATISFIED"}
            )
    summary = {
        "before": PRE_TRACE_BASELINE,
        "after": {
            "normative_requirements": len([path for path in paths if path["coverage"] != "DEFERRED"]),
            "covered_requirements": len([path for path in paths if path["coverage"] == "COVERED"]),
            "partially_covered_requirements": len([path for path in paths if path["coverage"] == "PARTIALLY_COVERED"]),
            "blocked_requirements": len([path for path in paths if path["coverage"] == "BLOCKED"]),
            "deferred_requirements": len([path for path in paths if path["coverage"] == "DEFERRED"]),
            "uncovered_requirements": len([path for path in paths if path["coverage"] == "UNCOVERED"]),
            "error_count": pre_trace.error_count,
            "warning_count": len([path for path in paths if path["coverage"] in {"PARTIALLY_COVERED", "BLOCKED"}]),
        },
        "trace_paths": sorted(paths, key=lambda item: str(item["requirement_id"])),
        "implementation_gaps": sorted(implementation_gaps),
    }
    return {
        "evaluator_version": PIR_EVALUATOR_VERSION,
        "pre_trace_validation_hash": pre_trace_hash,
        "summary": summary,
    }


def _residual_gaps(compiled: dict[str, Any], capability_matrix: tuple[CapabilityPosture, ...]) -> tuple[ResidualGap, ...]:
    requirements = _requirements_by_capability(compiled["requirements"])
    components = _components_by_capability(compiled["architecture"].get("components", []))
    gaps: list[ResidualGap] = []
    for row in capability_matrix:
        if row.new_satisfaction in {"SATISFIED", "DEFERRED", "NOT_APPLICABLE"}:
            continue
        gap_type: ResidualGapType = "IMPLEMENTATION_GAP" if row.new_satisfaction == "UNSATISFIED" else "EVIDENCE_GAP"
        gaps.append(
            ResidualGap(
                gap_id=f"PIR-GAP-{row.capability_id}",
                gap_type=gap_type,
                requirement_ids=tuple(sorted(requirements.get(row.capability_id, ()))),
                capability_ids=(row.capability_id,),
                component_interface_ids=tuple(sorted(components.get(row.capability_id, ()))),
                current_evidence=row.evidence_added,
                missing_behavior_or_evidence="; ".join(row.remaining_missing_evidence),
                verification_needed=f"capability-specific verification for {row.capability_id}",
                severity="HIGH" if gap_type == "IMPLEMENTATION_GAP" else "MEDIUM",
                current_release_relevance="CURRENT_RELEASE",
            )
        )
    return tuple(sorted(gaps, key=lambda item: item.gap_id))


def _previous_gap_evaluation(capability_matrix: tuple[CapabilityPosture, ...]) -> tuple[dict[str, Any], ...]:
    by_id = {row.capability_id: row for row in capability_matrix}
    results: list[dict[str, Any]] = []
    for capability_id in PREVIOUS_IMPLEMENTATION_GAPS:
        row = by_id[capability_id]
        if row.new_satisfaction == "SATISFIED":
            state: PreviousGapState = "CLOSED"
        elif row.new_satisfaction in {"DEFERRED", "NOT_APPLICABLE"}:
            state = "DEFERRED"
        elif row.evidence_added:
            state = "PARTIALLY_CLOSED"
        else:
            state = "STILL_OPEN"
        results.append(
            {
                "capability_id": capability_id,
                "state": state,
                "evidence": list(row.evidence_added),
                "remaining_missing_evidence": list(row.remaining_missing_evidence),
            }
        )
    return tuple(results)


def _decision(
    residual_gaps: tuple[ResidualGap, ...],
    runtime_summary: dict[str, Any],
) -> PostImplementationDecision:
    critical_runtime_counts = (
        runtime_summary["imported_tasks"] == REQUIRED_RUNTIME_TASK_COUNT,
        runtime_summary["passed_tasks"] == REQUIRED_RUNTIME_TASK_COUNT,
        runtime_summary["active_leases"] == 0,
        runtime_summary["nonterminal_executions"] == 0,
    )
    if not all(critical_runtime_counts):
        return "MANIFEST_DECISION_REQUIRED"
    if any(gap.gap_type == "IMPLEMENTATION_GAP" for gap in residual_gaps):
        return "RESIDUAL_GAPS_REQUIRE_NEW_DAG"
    if residual_gaps:
        return "MANIFEST_DECISION_REQUIRED"
    return "NO_RESIDUAL_IMPLEMENTATION_GAPS"


def _limitations(
    post_compiled_hash: str,
    runtime_summary: dict[str, Any],
    capability_matrix: tuple[CapabilityPosture, ...],
) -> tuple[str, ...]:
    limitations: list[str] = []
    if post_compiled_hash == PRE_IMPLEMENTATION_COMPILED_HASH:
        limitations.append("The authoritative manifest hash is unchanged; PIR satisfaction changes come from runtime evidence, not source-manifest edits.")
    if runtime_summary.get("successful_execution_outcomes") != REQUIRED_RUNTIME_TASK_COUNT:
        limitations.append("Successful execution outcome count differs from 13; review runtime evidence before generating another DAG.")
    evidence_gap_ids = [row.capability_id for row in capability_matrix if row.new_satisfaction == "PARTIALLY_SATISFIED"]
    if evidence_gap_ids:
        limitations.append(f"Residual partial capability evidence remains for: {', '.join(evidence_gap_ids)}.")
    return tuple(limitations)


def _recommendation(result: PostImplementationDecision) -> str:
    if result == "NO_RESIDUAL_IMPLEMENTATION_GAPS":
        return "READY_FOR_SYSTEM_ACCEPTANCE_GATE"
    if result == "RESIDUAL_GAPS_REQUIRE_NEW_DAG":
        return "GENERATE_RESIDUAL_IMPLEMENTATION_DAG"
    return "HUMAN_MANIFEST_REVIEW_REQUIRED"


def _evidence_authority() -> dict[str, str]:
    return {
        "postgresql_runtime_records": "runtime authority",
        "git_commits": "accepted source and commit authority",
        "independent_verifier": "verification authority",
        "human_gate_records": "approval authority",
        "artifact_evidence_graph": "NON_AUTHORITATIVE provenance/index only",
    }


def _ipcg_bound_hashes(artifacts_dir: Path) -> dict[str, str]:
    payload = _load_json(artifacts_dir / "ipcg-001/IPCG-001.json")
    hashes = payload.get("bound_hashes", {})
    if not isinstance(hashes, dict):
        return {}
    aliases = {
        "frozen_feasibility_hash": "feasibility_hash",
    }
    result: dict[str, str] = {}
    for key, value in hashes.items():
        canonical_key = aliases.get(str(key), str(key))
        result[canonical_key] = str(value)
    return result


def _manifest_material_differences(
    artifacts_dir: Path,
    repository_root: Path,
    manifest_root: Path,
) -> tuple[dict[str, Any], ...]:
    rhe_001 = _load_json(artifacts_dir / "rhe-001/RHE-001.json")
    baseline = rhe_001.get("starting_baseline", {})
    baseline_commit = baseline.get("commit") if isinstance(baseline, dict) else baseline
    baseline_commit = str(baseline_commit) if baseline_commit else None
    if not baseline_commit:
        return ()
    paths = _git_lines(repository_root, ["diff", "--name-only", f"{baseline_commit}..HEAD", "--", manifest_root.as_posix()])
    results: list[dict[str, Any]] = []
    for path in sorted(paths):
        summary = "source manifest changed during accepted implementation"
        diff = "\n".join(_git_lines(repository_root, ["diff", f"{baseline_commit}..HEAD", "--", path]))
        if path.endswith("capabilities.yaml") and "CMP-003:knowledge-graph-service" in diff:
            summary = "C16 Knowledge graph maturity changed from NOT_IMPLEMENTED to DEVELOPMENT and added CMP-003 evidence"
        results.append({"path": path, "summary": summary})
    return tuple(results)


def _requirements_by_capability(requirements: list[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    result: dict[str, set[str]] = {}
    for requirement in requirements:
        if requirement.get("classification") == "FUTURE/DEFERRED":
            continue
        for capability_id in requirement.get("capabilities", []):
            result.setdefault(str(capability_id), set()).add(str(requirement["id"]))
    return {capability_id: tuple(sorted(requirement_ids)) for capability_id, requirement_ids in result.items()}


def _components_by_capability(components: list[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    result: dict[str, set[str]] = {}
    for component in components:
        for capability_id in component.get("capabilities", []):
            result.setdefault(str(capability_id), set()).add(str(component["id"]))
    return {capability_id: tuple(sorted(component_ids)) for capability_id, component_ids in result.items()}


def _rhe_refs(artifacts_dir: Path) -> dict[str, tuple[str, ...]]:
    refs: dict[str, list[str]] = {}
    for path in sorted(artifacts_dir.glob("rhe-*/RHE-*.json")):
        payload = _load_json(path)
        task = payload.get("selected_task", {})
        task_id = task.get("id") if isinstance(task, dict) else None
        if task_id:
            refs.setdefault(str(task_id), []).append(f"{payload.get('gate_id', path.stem)}:{path.as_posix()}")
    return {task_id: tuple(sorted(values)) for task_id, values in refs.items()}


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _loads(value: str) -> dict[str, Any]:
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else {}


def _count(session: Session, statement: Any) -> int:
    return int(session.scalar(statement) or 0)


def _git_rev(repository_root: Path, rev: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", rev],
        cwd=repository_root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "UNKNOWN"


def _git_lines(repository_root: Path, args: list[str]) -> list[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository_root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        return []
    return [line for line in completed.stdout.splitlines() if line.strip()]


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(canonical_bytes(value) + b"\n")


def _markdown_report(review: PostImplementationReview) -> str:
    summary = review.requirement_coverage["after"]
    caps = "\n".join(
        f"- `{row.capability_id}` {row.previous_satisfaction} -> {row.new_satisfaction} ({row.new_maturity})"
        for row in review.capability_matrix
    )
    gaps = "\n".join(
        f"- `{gap.gap_id}` {gap.gap_type}: {', '.join(gap.capability_ids)} - {gap.missing_behavior_or_evidence}"
        for gap in review.residual_gaps
    ) or "- none"
    return (
        "# PIR-001 - Post-Implementation Recompilation\n\n"
        f"Result: `{review.result}`\n\n"
        f"Recommendation: `{_recommendation(review.result)}`\n\n"
        f"Head: `{review.head}`\n\n"
        f"Plan: `{review.plan_id}` v`{review.plan_version}`\n\n"
        "## Hashes\n\n"
        f"- pre compiled: `{review.pre_compiled_hash}`\n"
        f"- post compiled: `{review.post_compiled_hash}`\n"
        f"- post capability: `{review.post_capability_resolution_hash}`\n"
        f"- post trace: `{review.post_trace_validation_hash}`\n"
        f"- residual gaps: `{review.residual_gap_hash}`\n\n"
        "## Manifest Differences\n\n"
        + (
            "\n".join(
                f"- `{item['path']}`: {item['summary']}"
                for item in review.manifest_material_differences
            )
            or "- none"
        )
        + "\n\n"
        "## Requirements\n\n"
        f"- covered: {summary['covered_requirements']}\n"
        f"- partially covered: {summary['partially_covered_requirements']}\n"
        f"- blocked: {summary['blocked_requirements']}\n"
        f"- deferred: {summary['deferred_requirements']}\n"
        f"- trace errors: {summary['error_count']}\n"
        f"- trace warnings: {summary['warning_count']}\n\n"
        "## Capabilities\n\n"
        f"{caps}\n\n"
        "## Residual Gaps\n\n"
        f"{gaps}\n"
    )
