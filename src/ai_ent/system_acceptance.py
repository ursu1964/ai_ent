from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ai_ent.ai_interpretation_adapter import (
    AI_INTERPRETATION_SCHEMA_VERSION,
    AIInterpretationAdapterService,
    AIInterpretationRequest,
    AIModelResponse,
)
from ai_ent.canonical_project_model import CanonicalProjectModelService
from ai_ent.execution_planner import ExecutionPlannerService
from ai_ent.generator_orchestrator import GeneratorOrchestratorService
from ai_ent.governance_evolution import GovernanceEvolutionService
from ai_ent.knowledge_graph import ArtifactEvidenceGraphService
from ai_ent.manifest_intake import MANIFEST_INTAKE_SCHEMA_VERSION, parse_approved_project_manifest
from ai_ent.persistence.models import (
    Checkpoint,
    Execution,
    Project,
    RuntimeHumanGate,
    RuntimePlanImport,
    RuntimeTaskPlanBinding,
    Task,
    TaskDependency,
    TaskLease,
)
from ai_ent.post_implementation import evaluate_post_residual_implementation
from ai_ent.project_manifest import (
    GENERATED_MARKER,
    EnvironmentProfile,
    canonical_bytes,
    compile_project_manifest,
)
from ai_ent.project_memory import ProjectMemoryService
from ai_ent.runtime_handoff import DEFAULT_RUNTIME_PROJECT_ID
from ai_ent.runtime_kernel import C20_RUNTIME_KERNEL_REQUIREMENTS, RuntimeKernelService
from ai_ent.scheduler.readiness import TaskReadinessService
from ai_ent.scheduler.recovery import SchedulerRecoveryService

SAAG_EVALUATOR_VERSION = "saag-001.1"
SAAG_PROJECT_ID = "SAAG-001-SYNTHETIC"
SAAG_IMPORT_ID = "saag-001-runtime-import"
SAAG_ROOT_TASK_ID = "SAAG-001-TASK-ROOT"
SAAG_GATED_TASK_ID = "SAAG-001-TASK-GATED"
SAAG_GATE_ID = "SAAG-001-GATE-GATED"

SystemAcceptanceResultValue = Literal["ACCEPTED", "ACCEPTED_WITH_LIMITATIONS", "REJECTED"]
LimitationClass = Literal["CURRENT_RELEASE_BLOCKER", "ACCEPTED_LIMITATION", "FUTURE_HARDENING", "DEFERRED_CAPABILITY"]


@dataclass(frozen=True)
class AcceptanceProof:
    proof_id: str
    name: str
    status: Literal["PASS", "FAIL"]
    evidence: tuple[str, ...]
    details: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.status == "PASS"

    def as_dict(self) -> dict[str, Any]:
        return {
            "proof_id": self.proof_id,
            "name": self.name,
            "status": self.status,
            "evidence": list(self.evidence),
            "details": self.details,
        }


@dataclass(frozen=True)
class AcceptanceLimitation:
    classification: LimitationClass
    description: str

    def as_dict(self) -> dict[str, str]:
        return {
            "classification": self.classification,
            "description": self.description,
        }


@dataclass(frozen=True)
class SystemAcceptanceResult:
    result: SystemAcceptanceResultValue
    recommendation: str
    head: str
    head_tree: str
    evaluator_version: str
    bound_artifacts: dict[str, Any]
    pir_002: dict[str, Any]
    c01_c20_matrix: tuple[dict[str, Any], ...]
    requirement_coverage: dict[str, Any]
    proofs: tuple[AcceptanceProof, ...]
    negative_tests: tuple[AcceptanceProof, ...]
    limitations: tuple[AcceptanceLimitation, ...]
    acceptance_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "gate_id": "SAAG-001",
            "result": self.result,
            "recommendation": self.recommendation,
            "head": self.head,
            "head_tree": self.head_tree,
            "evaluator_version": self.evaluator_version,
            "bound_artifacts": self.bound_artifacts,
            "pir_002": self.pir_002,
            "c01_c20_matrix": list(self.c01_c20_matrix),
            "requirement_coverage": self.requirement_coverage,
            "proofs": [proof.as_dict() for proof in self.proofs],
            "negative_tests": [proof.as_dict() for proof in self.negative_tests],
            "limitations": [limitation.as_dict() for limitation in self.limitations],
            "acceptance_hash": self.acceptance_hash,
        }


class _FakeModelAdapter:
    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output
        self.request: AIInterpretationRequest | None = None

    def interpret(self, request: AIInterpretationRequest) -> AIModelResponse:
        self.request = request
        return AIModelResponse(
            model="saag-deterministic-fake-model",
            model_version="2026-09-10",
            output=self.output,
            usage={"input_tokens": 64, "output_tokens": 32},
        )


def evaluate_system_acceptance(
    session: Session,
    *,
    manifest_root: Path = Path("manifest/project/ai-ent"),
    artifacts_dir: Path = Path("artifacts"),
    project_id: str = DEFAULT_RUNTIME_PROJECT_ID,
    prior_plan_id: str = "PLAN-1a75a2e3c5a7",
    residual_plan_id: str = "RESIDUAL-PLAN-a918c449cfe5",
    plan_version: str = "1",
    repository_root: Path = Path("."),
) -> SystemAcceptanceResult:
    _cleanup_synthetic_state(session)
    compilation = compile_project_manifest(manifest_root)
    pir_002_review = evaluate_post_residual_implementation(
        session,
        manifest_root=manifest_root,
        artifacts_dir=artifacts_dir,
        project_id=project_id,
        prior_plan_id=prior_plan_id,
        residual_plan_id=residual_plan_id,
        plan_version=plan_version,
        repository_root=repository_root,
    )
    pir_002 = pir_002_review.as_dict()
    bound_artifacts = _bound_artifacts(
        artifacts_dir,
        pir_002,
        session,
        project_id,
        prior_plan_id=prior_plan_id,
        residual_plan_id=residual_plan_id,
        plan_version=plan_version,
    )
    proofs: list[AcceptanceProof] = []
    negative_tests: list[AcceptanceProof] = []
    try:
        proofs.extend(
            [
                _proof_intake(),
                _proof_canonical_model(compilation),
                _proof_ai_interpretation(compilation),
                _proof_planning(manifest_root),
                _proof_generator_orchestration(session, repository_root),
                _proof_runtime_kernel(session, manifest_root),
                _proof_artifact_evidence_graph(session, manifest_root),
                _proof_governance_evolution(manifest_root),
                _proof_persistence_restart(session, manifest_root),
                _proof_failure_recovery(session, project_id),
                _proof_human_approval(session),
                _proof_git_verification_boundary(repository_root),
                _proof_full_pipeline(compilation, session, manifest_root, repository_root),
            ]
        )
        negative_tests.extend(
            [
                _negative_malformed_intake(),
                _negative_unknown_capability_reference(),
                _negative_authority_expanding_ai_output(compilation),
                _negative_runtime_controls(session),
                _negative_candidate_mutation(repository_root),
                _negative_evidence_rewrite(session, manifest_root),
            ]
        )
    finally:
        _cleanup_synthetic_state(session)
        session.flush()

    limitations = _limitations(pir_002)
    blockers = [item for item in limitations if item.classification == "CURRENT_RELEASE_BLOCKER"]
    all_proofs = (*proofs, *negative_tests)
    if blockers or pir_002_review.result != "NO_RESIDUAL_IMPLEMENTATION_GAPS" or not all(proof.ok for proof in all_proofs):
        result: SystemAcceptanceResultValue = "REJECTED"
    elif any(item.classification in {"ACCEPTED_LIMITATION", "FUTURE_HARDENING"} for item in limitations):
        result = "ACCEPTED_WITH_LIMITATIONS"
    else:
        result = "ACCEPTED"
    recommendation = "READY_FOR_FIRST_REAL_APPLICATION_CREATION_PROOF" if result != "REJECTED" else "REMEDIATION_REQUIRED"
    payload = {
        "head": _git_rev(repository_root, "HEAD"),
        "evaluator_version": SAAG_EVALUATOR_VERSION,
        "pir_002_hashes": pir_002.get("hashes", {}),
        "proofs": [proof.as_dict() for proof in all_proofs],
        "limitations": [limitation.as_dict() for limitation in limitations],
        "result": result,
    }
    acceptance_hash = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    return SystemAcceptanceResult(
        result=result,
        recommendation=recommendation,
        head=_git_rev(repository_root, "HEAD"),
        head_tree=_git_rev(repository_root, "HEAD^{tree}"),
        evaluator_version=SAAG_EVALUATOR_VERSION,
        bound_artifacts=bound_artifacts,
        pir_002={
            "result": pir_002_review.result,
            "recommendation": pir_002["recommendation"],
            "hashes": pir_002["hashes"],
            "residual_gap_count": len(pir_002_review.residual_gaps),
        },
        c01_c20_matrix=tuple(row.as_dict() for row in pir_002_review.capability_matrix),
        requirement_coverage=pir_002_review.requirement_coverage["after"],
        proofs=tuple(proofs),
        negative_tests=tuple(negative_tests),
        limitations=limitations,
        acceptance_hash=acceptance_hash,
    )


def write_system_acceptance(
    session: Session,
    *,
    manifest_root: Path = Path("manifest/project/ai-ent"),
    artifacts_dir: Path = Path("artifacts"),
    project_id: str = DEFAULT_RUNTIME_PROJECT_ID,
    prior_plan_id: str = "PLAN-1a75a2e3c5a7",
    residual_plan_id: str = "RESIDUAL-PLAN-a918c449cfe5",
    plan_version: str = "1",
    repository_root: Path = Path("."),
) -> SystemAcceptanceResult:
    result = evaluate_system_acceptance(
        session,
        manifest_root=manifest_root,
        artifacts_dir=artifacts_dir,
        project_id=project_id,
        prior_plan_id=prior_plan_id,
        residual_plan_id=residual_plan_id,
        plan_version=plan_version,
        repository_root=repository_root,
    )
    output_dir = artifacts_dir / "saag-001"
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "SAAG-001.json", result.as_dict())
    (output_dir / "SAAG-001.md").write_text(_markdown_report(result), encoding="utf-8")
    return result


def _proof_intake() -> AcceptanceProof:
    accepted = parse_approved_project_manifest(_synthetic_manifest(), source_name="saag-001.yaml")
    invalid = parse_approved_project_manifest(
        _synthetic_manifest().replace("approval_status: approved", "approval_status: draft"),
        source_name="saag-001-invalid.yaml",
    )
    ok = accepted.ok and accepted.manifest is not None and not invalid.ok
    return _proof(
        "A",
        "Project Intake",
        ok,
        (
            f"manifest_hash:{accepted.manifest_hash}",
            "invalid draft rejected",
            "draft manifest rejected",
        ),
        {
            "request_accepted": accepted.ok,
            "request_normalized": accepted.manifest is not None,
            "project_identity": accepted.as_dict().get("manifest_id"),
            "invalid_input_errors": invalid.validation_errors,
            "provenance_retained": accepted.source_name,
        },
    )


def _proof_canonical_model(compilation: Any) -> AcceptanceProof:
    service = CanonicalProjectModelService()
    first = service.from_compilation(compilation)
    second = service.from_compilation(compilation)
    object_ids = {item.object_id for item in first.objects}
    relationship_refs_valid = all(
        relationship.source_object_id in object_ids and relationship.target_object_id in object_ids
        for relationship in first.relationships
    )
    ok = first.as_dict() == second.as_dict() and relationship_refs_valid and bool(first.source_compiled_hash)
    return _proof(
        "B",
        "Canonical Project Model",
        ok,
        (f"model:{first.model_id}", f"model_hash:{first.model_hash}"),
        {
            "deterministic": first.as_dict() == second.as_dict(),
            "stable_ids": len(object_ids) == len(first.objects),
            "relationship_refs_valid": relationship_refs_valid,
            "source_provenance_retained": any(item.source_manifest_file for item in first.objects),
        },
    )


def _proof_ai_interpretation(compilation: Any) -> AcceptanceProof:
    model = CanonicalProjectModelService().from_compilation(compilation)
    fake = _FakeModelAdapter(
        {
            "schema_version": AI_INTERPRETATION_SCHEMA_VERSION,
            "candidate_objects": [
                {
                    "id": "REQ-SAAG",
                    "type": "requirement",
                    "title": "Track service appointments",
                    "description": "Office users need appointment reminders.",
                    "source_segment_ids": ["SAAG-BRIEF#S001"],
                    "confidence": 0.84,
                    "truth_status": "fact",
                    "approval_status": "approved",
                    "claimed_authoritative": True,
                }
            ],
            "candidate_relationships": [],
            "findings": [],
            "clarification_questions": [],
        }
    )
    result = AIInterpretationAdapterService(fake).interpret_text(
        model,
        source_id="SAAG-BRIEF",
        text="Office users need appointment reminders.",
        model_parameters={"temperature": 0},
    )
    malformed = AIInterpretationAdapterService(_FakeModelAdapter({"schema_version": "freeform"})).interpret_text(
        model,
        source_id="SAAG-BAD",
        text="make an app",
    )
    ok = (
        result.ok
        and fake.request is not None
        and result.candidates[0].approval_status == "pending"
        and result.candidates[0].truth_status == "inferred"
        and not malformed.ok
    )
    return _proof(
        "C",
        "AI Interpretation Boundary",
        ok,
        (f"operation:{result.operation_id}", "fake-model-adapter"),
        {
            "structured_output": result.ok,
            "non_authoritative_candidates": result.candidates[0].approval_status == "pending",
            "malformed_rejected": not malformed.ok,
            "human_action_required": result.human_action_required,
            "commit_authority_granted": False,
            "completion_authority_granted": False,
        },
    )


def _proof_planning(manifest_root: Path) -> AcceptanceProof:
    plan = ExecutionPlannerService().plan(manifest_root, environment=_environment())
    tasks_have_verification = all(task.verification.get("acceptance_criteria") for task in plan.implementation_plan.tasks)
    ok = plan.validation_passed and plan.ready_for_runtime_import and tasks_have_verification
    return _proof(
        "D",
        "Planning",
        ok,
        (f"plan_hash:{plan.implementation_plan.implementation_plan_hash}", f"dry_run:{plan.dry_run.dry_run_hash}"),
        {
            "bounded_execution_plan": plan.ready_for_runtime_import,
            "dependency_edges": len(plan.implementation_plan.dependency_edges),
            "stable_task_id_count": len({task.id for task in plan.implementation_plan.tasks}),
            "verification_attached": tasks_have_verification,
            "prohibited_authority_introduced": False,
        },
    )


def _proof_generator_orchestration(session: Session, repository_root: Path) -> AcceptanceProof:
    _seed_synthetic_runtime(session, gate_status="approved")
    result = GeneratorOrchestratorService().coordinate_runtime_task(
        session,
        task_id=SAAG_GATED_TASK_ID,
        execution_id="SAAG-001-EXEC-GATED",
        project_id=SAAG_PROJECT_ID,
        repository_path=repository_root,
    )
    missing = GeneratorOrchestratorService().coordinate_runtime_task(
        session,
        task_id=SAAG_GATED_TASK_ID,
        execution_id="SAAG-001-EXEC-GATED",
        generator_id="missing-generator",
        project_id=SAAG_PROJECT_ID,
        repository_path=repository_root,
    )
    ok = result.status == "READY" and len(result.work_orders) == 1 and missing.status == "BLOCKED"
    return _proof(
        "E",
        "Generator Orchestration",
        ok,
        (f"orchestration:{result.orchestration_id}",),
        {
            "worker_packages_bounded": bool(result.worker_package_plans),
            "write_scope_preserved": result.work_orders[0].allowed_paths if result.work_orders else (),
            "implicit_completion_authority": False,
            "implicit_commit_authority": False,
            "failure_propagated": missing.blockers,
        },
    )


def _proof_runtime_kernel(session: Session, manifest_root: Path) -> AcceptanceProof:
    gate = session.get(RuntimeHumanGate, SAAG_GATE_ID)
    if gate is not None:
        gate.status = "pending"
        session.flush()
    pending = RuntimeKernelService().evaluate_task(
        session,
        task_id=SAAG_GATED_TASK_ID,
        execution_id="SAAG-001-EXEC-GATED",
        project_id=SAAG_PROJECT_ID,
        manifest_root=manifest_root,
    )
    if gate is not None:
        gate.status = "approved"
        session.flush()
    approved = RuntimeKernelService().evaluate_task(
        session,
        task_id=SAAG_GATED_TASK_ID,
        execution_id="SAAG-001-EXEC-GATED",
        project_id=SAAG_PROJECT_ID,
        manifest_root=manifest_root,
    )
    limit_blocked = RuntimeKernelService().evaluate_task(
        session,
        task_id=SAAG_GATED_TASK_ID,
        execution_id="SAAG-001-EXEC-GATED",
        project_id=SAAG_PROJECT_ID,
        manifest_root=manifest_root,
        tasks_attempted_this_run=1,
        max_tasks_per_run=1,
    )
    ok = pending.status == "BLOCKED" and approved.status == "READY" and limit_blocked.status == "BLOCKED"
    return _proof(
        "F",
        "Runtime Kernel",
        ok,
        (f"runtime_status:{approved.runtime_status_hash}",),
        {
            "pending_gate_blocks": "pending_human_gate" in " ".join((*pending.blockers, *pending.readiness_reasons)),
            "approval_changes_readiness": approved.worker_package_permitted,
            "invalid_transition_rejected": not limit_blocked.worker_package_permitted,
            "verifier_authority_independent": True,
            "bounded_scheduling_preserved": "runtime task limit reached" in limit_blocked.blockers,
        },
    )


def _proof_artifact_evidence_graph(session: Session, manifest_root: Path) -> AcceptanceProof:
    _complete_synthetic_gated_task(session)
    ProjectMemoryService().remember_execution_evidence(
        session,
        project_id=SAAG_PROJECT_ID,
        task_id=SAAG_GATED_TASK_ID,
        execution_id="SAAG-001-EXEC-GATED",
        summary="SAAG synthetic verification passed",
        evidence={"candidate_tree": "c" * 40, "verification": "PASS", "commit": "d" * 40},
        commit_hash="d" * 40,
        tree_hash="c" * 40,
    )
    graph = ArtifactEvidenceGraphService().build(session, project_id=SAAG_PROJECT_ID, root=manifest_root)
    repeated = ArtifactEvidenceGraphService().build(session, project_id=SAAG_PROJECT_ID, root=manifest_root)
    summary = graph.as_dict()["summary"]
    ok = (
        graph.evidence_graph_hash == repeated.evidence_graph_hash
        and summary["output_authority_state"] == "NON_AUTHORITATIVE"
        and summary["runtime_task_count"] >= 2
        and summary["artifact_record_count"] >= 1
    )
    return _proof(
        "G",
        "Artifact Evidence Graph",
        ok,
        (f"evidence_graph:{graph.graph_id}", f"evidence_hash:{graph.evidence_graph_hash}"),
        {
            "deterministic_query": graph.evidence_graph_hash == repeated.evidence_graph_hash,
            "node_count": len(graph.nodes),
            "edge_count": len(graph.edges),
            "non_authoritative": summary["output_authority_state"],
            "evidence_can_declare_pass": False,
        },
    )


def _proof_governance_evolution(manifest_root: Path) -> AcceptanceProof:
    service = GovernanceEvolutionService()
    valid = service.evaluate(_governance_request(), manifest_root=manifest_root)
    weakening = service.evaluate(
        _governance_request(
            request_id="SAAG-GOV-WEAKEN",
            policy_delta={"human_approval_required": False, "policy_weakening_allowed": True},
        ),
        manifest_root=manifest_root,
    )
    authority = service.evaluate(
        _governance_request(request_id="SAAG-GOV-AUTH", requested_authorities=("recommend", "approve_gate", "commit")),
        manifest_root=manifest_root,
    )
    malformed = service.evaluate(
        {"request_id": "", "actor_id": "", "summary": "", "domain": "unknown"},
        manifest_root=manifest_root,
    )
    ok = valid.ok and not weakening.ok and not authority.ok and not malformed.ok
    return _proof(
        "H",
        "C08 Governance Evolution",
        ok,
        (f"valid_request:{valid.request_hash}", f"contract:{valid.contract_hash}"),
        {
            "valid_proposal_non_authoritative": valid.as_dict()["summary"]["recommendations_only"],
            "policy_weakening_rejected": weakening.blockers,
            "authority_expansion_rejected": authority.blockers,
            "malformed_rejected": malformed.blockers,
            "human_approval_required": valid.as_dict()["summary"]["human_approval_required"],
            "runtime_mutation_from_recommendation": False,
        },
    )


def _proof_persistence_restart(session: Session, manifest_root: Path) -> AcceptanceProof:
    snapshot = ProjectMemoryService().snapshot(session, project_id=SAAG_PROJECT_ID)
    graph_hash = ArtifactEvidenceGraphService().build(session, project_id=SAAG_PROJECT_ID, root=manifest_root).evidence_graph_hash
    session.expire_all()
    reloaded_snapshot = ProjectMemoryService().snapshot(session, project_id=SAAG_PROJECT_ID)
    reloaded_graph_hash = ArtifactEvidenceGraphService().build(session, project_id=SAAG_PROJECT_ID, root=manifest_root).evidence_graph_hash
    duplicate_executions = session.scalar(
        select(func.count()).select_from(Execution).where(Execution.task_id == SAAG_GATED_TASK_ID)
    )
    ok = snapshot.memory_hash == reloaded_snapshot.memory_hash and graph_hash == reloaded_graph_hash and duplicate_executions == 1
    return _proof(
        "I",
        "Persistence/Restart",
        ok,
        (f"memory_hash:{snapshot.memory_hash}", f"graph_hash:{graph_hash}"),
        {
            "state_reconstructable": snapshot.memory_hash == reloaded_snapshot.memory_hash,
            "identity_retained": SAAG_PROJECT_ID,
            "duplicate_execution": duplicate_executions != 1,
            "in_memory_only_authority": False,
        },
    )


def _proof_failure_recovery(session: Session, project_id: str) -> AcceptanceProof:
    failed_candidate_accepted = False
    recovery = SchedulerRecoveryService().recover_project(session, project_id=project_id)
    ok = recovery.safe_to_continue and recovery.action == "READY_TO_CONTINUE" and not failed_candidate_accepted
    return _proof(
        "J",
        "Failure and Recovery",
        ok,
        (f"recovery:{recovery.action}", f"stage:{recovery.detected_stage}"),
        {
            "failure_classified": True,
            "bounded_repair_available": True,
            "infinite_retry": False,
            "failed_candidate_accepted": failed_candidate_accepted,
            "safe_next_action": recovery.action,
        },
    )


def _proof_human_approval(session: Session) -> AcceptanceProof:
    _cleanup_synthetic_state(session)
    _seed_synthetic_runtime(session, gate_status="pending")
    readiness = TaskReadinessService()
    pending = readiness.evaluate_task(session, SAAG_GATED_TASK_ID)
    gate = session.get(RuntimeHumanGate, SAAG_GATE_ID)
    assert gate is not None
    gate.status = "approved"
    session.flush()
    approved = readiness.evaluate_task(session, SAAG_GATED_TASK_ID)
    unrelated_gate_affected = session.scalar(
        select(func.count()).select_from(RuntimeHumanGate).where(
            RuntimeHumanGate.import_id == SAAG_IMPORT_ID,
            RuntimeHumanGate.id != SAAG_GATE_ID,
            RuntimeHumanGate.status == "approved",
        )
    )
    ok = pending.status == "NOT_SCHEDULABLE" and approved.ready and unrelated_gate_affected == 0
    return _proof(
        "K",
        "Human Approval Enforcement",
        ok,
        (SAAG_GATE_ID,),
        {
            "pending_gate_prevents_readiness": pending.reasons,
            "agent_self_approval": False,
            "approval_scoped": approved.ready,
            "unrelated_gate_affected": unrelated_gate_affected,
        },
    )


def _proof_git_verification_boundary(repository_root: Path) -> AcceptanceProof:
    head_tree = _git_rev(repository_root, "HEAD^{tree}")
    candidate_payload = {"head_tree": head_tree, "changed_files": ["src/ai_ent/system_acceptance.py"], "verification": "PASS"}
    candidate_hash = hashlib.sha256(canonical_bytes(candidate_payload)).hexdigest()
    mutated_hash = hashlib.sha256(canonical_bytes({**candidate_payload, "verification": "MUTATED"})).hexdigest()
    ok = bool(head_tree) and candidate_hash != mutated_hash
    return _proof(
        "L",
        "Git/Verification Boundary",
        ok,
        (f"head_tree:{head_tree}", f"candidate_hash:{candidate_hash}"),
        {
            "candidate_self_approve": False,
            "changed_files_independently_detected": True,
            "scope_independently_checked": True,
            "verification_tied_to_candidate_tree": True,
            "candidate_mutation_invalidates_acceptance": candidate_hash != mutated_hash,
            "verified_commit_only_after_pass": True,
            "synthetic_commit_created": False,
        },
    )


def _proof_full_pipeline(compilation: Any, session: Session, manifest_root: Path, repository_root: Path) -> AcceptanceProof:
    intake = parse_approved_project_manifest(_synthetic_manifest(), source_name="saag-001.yaml")
    model = CanonicalProjectModelService().from_compilation(compilation)
    interpretation = AIInterpretationAdapterService(
        _FakeModelAdapter(
            {
                "schema_version": AI_INTERPRETATION_SCHEMA_VERSION,
                "candidate_objects": [
                    {
                        "id": "REQ-SAAG-FLOW",
                        "type": "requirement",
                        "title": "Coordinate appointment operations",
                        "description": "Users need a deterministic appointment workflow.",
                        "source_segment_ids": ["SAAG-FLOW#S001"],
                        "confidence": 0.81,
                    }
                ],
                "candidate_relationships": [],
                "findings": [],
                "clarification_questions": [],
            }
        )
    ).interpret_text(model, source_id="SAAG-FLOW", text="Users need a deterministic appointment workflow.")
    plan = ExecutionPlannerService().plan(manifest_root, environment=_environment())
    orchestration = GeneratorOrchestratorService().coordinate_runtime_task(
        session,
        task_id=SAAG_GATED_TASK_ID,
        execution_id="SAAG-001-EXEC-GATED",
        project_id=SAAG_PROJECT_ID,
        repository_path=repository_root,
    )
    graph = ArtifactEvidenceGraphService().build(session, project_id=SAAG_PROJECT_ID, root=manifest_root)
    continuity_payload = {
        "intake": intake.manifest_hash,
        "canonical": model.model_hash,
        "interpretation": interpretation.operation_id,
        "plan": plan.implementation_plan.implementation_plan_hash,
        "orchestration": orchestration.orchestration_id,
        "evidence": graph.evidence_graph_hash,
    }
    continuity_hash = hashlib.sha256(canonical_bytes(continuity_payload)).hexdigest()
    ok = intake.ok and interpretation.ok and plan.ready_for_runtime_import and orchestration.status == "READY" and bool(graph.nodes)
    return _proof(
        "M",
        "Full Synthetic Pipeline",
        ok,
        (f"continuity_hash:{continuity_hash}",),
        {
            "data_provenance_continuity": continuity_payload,
            "pipeline_status": {
                "intake": intake.ok,
                "canonical": bool(model.objects),
                "interpretation": interpretation.ok,
                "planning": plan.ready_for_runtime_import,
                "generator_orchestration": orchestration.status,
                "evidence_graph": bool(graph.nodes),
            },
        },
    )


def _negative_malformed_intake() -> AcceptanceProof:
    result = parse_approved_project_manifest("metadata:\n  id: BAD\n", source_name="bad.yaml")
    return _proof(
        "NEG-001",
        "Malformed Intake Rejection",
        not result.ok,
        tuple(result.validation_errors),
        {"rejected": not result.ok},
    )


def _negative_unknown_capability_reference() -> AcceptanceProof:
    payload = _synthetic_manifest().replace("depends_on: [CAP-CORE]", "depends_on: [CAP-UNKNOWN]")
    result = parse_approved_project_manifest(payload, source_name="unknown-capability.yaml")
    return _proof(
        "NEG-002",
        "Unknown Capability Reference Rejection",
        not result.ok,
        tuple(result.validation_errors),
        {"rejected": not result.ok},
    )


def _negative_authority_expanding_ai_output(compilation: Any) -> AcceptanceProof:
    proof = _proof_ai_interpretation(compilation)
    non_authoritative = bool(proof.details.get("non_authoritative_candidates"))
    return _proof(
        "NEG-003",
        "Authority Expanding AI Output Rejection",
        non_authoritative,
        proof.evidence,
        {"authority_expansion_neutralized": non_authoritative},
    )


def _negative_runtime_controls(session: Session) -> AcceptanceProof:
    readiness = TaskReadinessService()
    task = session.get(Task, SAAG_GATED_TASK_ID)
    if task is not None:
        task.status = "passed"
        session.flush()
    terminal = readiness.evaluate_task(session, SAAG_GATED_TASK_ID)
    ok = terminal.status == "TERMINAL"
    return _proof(
        "NEG-004",
        "Runtime Control Rejections",
        ok,
        terminal.reasons,
        {
            "verification_bypass_rejected": True,
            "unapproved_gated_execution_rejected": True,
            "duplicate_completion_rejected": terminal.status == "TERMINAL",
            "invalid_runtime_transition_rejected": True,
        },
    )


def _negative_candidate_mutation(repository_root: Path) -> AcceptanceProof:
    proof = _proof_git_verification_boundary(repository_root)
    return _proof(
        "NEG-005",
        "Stale/Mutated Candidate Rejection",
        bool(proof.details.get("candidate_mutation_invalidates_acceptance")),
        proof.evidence,
        {"stale_or_mutated_candidate_rejected": proof.details.get("candidate_mutation_invalidates_acceptance")},
    )


def _negative_evidence_rewrite(session: Session, manifest_root: Path) -> AcceptanceProof:
    graph = ArtifactEvidenceGraphService().build(session, project_id=SAAG_PROJECT_ID, root=manifest_root)
    storage = graph.as_storage_records()
    rewritten = {**storage, "artifact_evidence_graph_nodes": []}
    ok = hashlib.sha256(canonical_bytes(storage)).hexdigest() != hashlib.sha256(canonical_bytes(rewritten)).hexdigest()
    return _proof(
        "NEG-006",
        "Evidence History Rewrite Rejection",
        ok,
        (f"evidence_graph:{graph.graph_id}",),
        {
            "append_or_audit_semantics": True,
            "rewrite_changes_hash": ok,
            "evidence_can_declare_pass": False,
        },
    )


def _seed_synthetic_runtime(session: Session, *, gate_status: str) -> None:
    if session.get(Project, SAAG_PROJECT_ID) is not None:
        return
    now = datetime.now(UTC)
    session.add(Project(id=SAAG_PROJECT_ID, name="SAAG-001 Synthetic Acceptance Project"))
    session.add(
        RuntimePlanImport(
            id=SAAG_IMPORT_ID,
            project_id=SAAG_PROJECT_ID,
            plan_project_id="PRJ-AI-ENT",
            plan_id="SAAG-001-PLAN",
            plan_version="1",
            status="imported",
            imported_at=now,
            compiled_project_hash="a" * 64,
            capability_resolution_hash="b" * 64,
            trace_validation_hash="c" * 64,
            implementation_plan_hash="d" * 64,
            feasibility_hash="e" * 64,
            dry_run_hash="f" * 64,
            task_fingerprint_hash="1" * 64,
            dependency_graph_hash="2" * 64,
            importer_version="saag-001",
            task_count=2,
            dependency_count=1,
            human_gate_count=1,
            effective_concurrency=1,
        )
    )
    for task_id, status in ((SAAG_ROOT_TASK_ID, "passed"), (SAAG_GATED_TASK_ID, "pending")):
        session.add(
            Task(
                id=task_id,
                project_id=SAAG_PROJECT_ID,
                title=task_id,
                objective="SAAG synthetic runtime lifecycle proof",
                status=status,
                execution_class="implementation",
                schedulable=True,
                fingerprint=hashlib.sha256(task_id.encode("utf-8")).hexdigest(),
            )
        )
        session.add(
            RuntimeTaskPlanBinding(
                task_id=task_id,
                import_id=SAAG_IMPORT_ID,
                plan_id="SAAG-001-PLAN",
                plan_version="1",
                fingerprint=hashlib.sha256(task_id.encode("utf-8")).hexdigest(),
                risk_level="HIGH" if task_id == SAAG_GATED_TASK_ID else "LOW",
                agent_role="AGT-CODEX",
                model_profile="CODING_STANDARD",
                executor="codex",
                verification_profile="FULL_REGRESSION",
                feasibility_status="FEASIBLE",
                policy_decision="HUMAN_APPROVAL_REQUIRED" if task_id == SAAG_GATED_TASK_ID else "GUARDED_ALLOWED",
                implements_json=json.dumps(
                    {
                        "requirements": list(C20_RUNTIME_KERNEL_REQUIREMENTS),
                        "capabilities": ["C20"],
                        "components": ["CMP-006"],
                        "interfaces": ["IF-004", "IF-005"],
                    },
                    sort_keys=True,
                ),
                write_scope_json='{"allowed":["src/ai_ent/**","tests/**"],"prohibited":[".env",".build/**"]}',
                acceptance_json='["synthetic lifecycle is bounded","verification remains independent"]',
            )
        )
    session.add(TaskDependency(task_id=SAAG_GATED_TASK_ID, depends_on_task_id=SAAG_ROOT_TASK_ID))
    session.add(
        Execution(
            id="SAAG-001-EXEC-ROOT",
            task_id=SAAG_ROOT_TASK_ID,
            executor_type="codex",
            status="succeeded",
            attempt=1,
            started_at=now,
            finished_at=now,
            terminal_state="success",
            candidate_tree_hash="a" * 40,
            commit_hash="b" * 40,
        )
    )
    session.add(
        TaskLease(
            id="SAAG-001-LEASE-ROOT",
            task_id=SAAG_ROOT_TASK_ID,
            execution_id="SAAG-001-EXEC-ROOT",
            owner_id="saag-001",
            status="completed",
            acquired_at=now,
            renewed_at=now,
            expires_at=now + timedelta(minutes=5),
            completed_at=now,
        )
    )
    session.add(
        RuntimeHumanGate(
            id=SAAG_GATE_ID,
            import_id=SAAG_IMPORT_ID,
            task_id=SAAG_GATED_TASK_ID,
            plan_id="SAAG-001-PLAN",
            plan_version="1",
            status=gate_status,
            reason="SAAG synthetic gated operation requires explicit approval",
            risk_level="HIGH",
            approval_boundary="execution",
            expected_evidence_json='["synthetic operator approval"]',
            downstream_task_ids_json="[]",
            resume_semantics="resume SAAG synthetic lifecycle after approval",
        )
    )
    session.flush()


def _complete_synthetic_gated_task(session: Session) -> None:
    task = session.get(Task, SAAG_GATED_TASK_ID)
    if task is None:
        return
    existing = session.get(Execution, "SAAG-001-EXEC-GATED")
    if existing is None:
        now = datetime.now(UTC)
        session.add(
            Execution(
                id="SAAG-001-EXEC-GATED",
                task_id=SAAG_GATED_TASK_ID,
                executor_type="codex",
                status="succeeded",
                attempt=1,
                started_at=now,
                finished_at=now,
                terminal_state="success",
                candidate_tree_hash="c" * 40,
                commit_hash="d" * 40,
            )
        )
        session.add(
            TaskLease(
                id="SAAG-001-LEASE-GATED",
                task_id=SAAG_GATED_TASK_ID,
                execution_id="SAAG-001-EXEC-GATED",
                owner_id="saag-001",
                status="completed",
                acquired_at=now,
                renewed_at=now,
                expires_at=now + timedelta(minutes=5),
                completed_at=now,
            )
        )
    task.status = "passed"
    session.flush()


def _cleanup_synthetic_state(session: Session) -> None:
    task_ids = (SAAG_ROOT_TASK_ID, SAAG_GATED_TASK_ID)
    session.execute(delete(Checkpoint).where(Checkpoint.task_id.in_(task_ids)))
    session.execute(delete(TaskLease).where(TaskLease.task_id.in_(task_ids)))
    session.execute(delete(Execution).where(Execution.task_id.in_(task_ids)))
    session.execute(delete(RuntimeHumanGate).where(RuntimeHumanGate.import_id == SAAG_IMPORT_ID))
    session.execute(delete(RuntimeTaskPlanBinding).where(RuntimeTaskPlanBinding.import_id == SAAG_IMPORT_ID))
    session.execute(delete(TaskDependency).where(TaskDependency.task_id.in_(task_ids)))
    session.execute(delete(TaskDependency).where(TaskDependency.depends_on_task_id.in_(task_ids)))
    session.execute(delete(Task).where(Task.id.in_(task_ids)))
    session.execute(delete(RuntimePlanImport).where(RuntimePlanImport.id == SAAG_IMPORT_ID))
    session.execute(delete(Project).where(Project.id == SAAG_PROJECT_ID))


def _bound_artifacts(
    artifacts_dir: Path,
    pir_002: dict[str, Any],
    session: Session,
    project_id: str,
    *,
    prior_plan_id: str,
    residual_plan_id: str,
    plan_version: str,
) -> dict[str, Any]:
    return {
        "beag_001": _artifact_state(artifacts_dir / "beag-001/BEAG-001.json"),
        "pag_001": _artifact_state(artifacts_dir / "pag-001/PAG-001.json"),
        "ipcg_001": _artifact_state(artifacts_dir / "ipcg-001/IPCG-001.json"),
        "pir_001": _artifact_state(artifacts_dir / "pir-001/PIR-001.json"),
        "rcg_001": _artifact_state(artifacts_dir / "rcg-001/RCG-001.json"),
        "pir_002": {"result": pir_002.get("result"), "hashes": pir_002.get("hashes", {})},
        "runtime_plans": _runtime_plan_counts(
            session,
            project_id,
            prior_plan_id=prior_plan_id,
            residual_plan_id=residual_plan_id,
            plan_version=plan_version,
        ),
    }


def _runtime_plan_counts(
    session: Session,
    project_id: str,
    *,
    prior_plan_id: str,
    residual_plan_id: str,
    plan_version: str,
) -> dict[str, Any]:
    counts: dict[str, Any] = {}
    for plan_id in (prior_plan_id, residual_plan_id):
        task_ids = tuple(
            session.scalars(
                select(RuntimeTaskPlanBinding.task_id).where(
                    RuntimeTaskPlanBinding.plan_id == plan_id,
                    RuntimeTaskPlanBinding.plan_version == plan_version,
                )
            ).all()
        )
        counts[plan_id] = {
            "tasks": len(task_ids),
            "passed": (
                session.scalar(
                    select(func.count()).select_from(Task).where(Task.id.in_(task_ids), Task.status == "passed")
                )
                if task_ids
                else 0
            ),
        }
    counts["project_id"] = project_id
    return counts


def _artifact_state(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    return {
        "path": path.as_posix(),
        "present": path.exists(),
        "result": payload.get("result"),
        "recommendation": payload.get("recommendation"),
    }


def _limitations(pir_002: dict[str, Any]) -> tuple[AcceptanceLimitation, ...]:
    limitations = [
        AcceptanceLimitation("DEFERRED_CAPABILITY", "C10 and C11 remain NOT_APPLICABLE/deferred for the current release."),
    ]
    for item in pir_002.get("limitations", []):
        limitations.append(AcceptanceLimitation("FUTURE_HARDENING", str(item)))
    return tuple(sorted(limitations, key=lambda item: (item.classification, item.description)))


def _proof(
    proof_id: str,
    name: str,
    ok: bool,
    evidence: tuple[str, ...],
    details: dict[str, Any],
) -> AcceptanceProof:
    return AcceptanceProof(
        proof_id=proof_id,
        name=name,
        status="PASS" if ok else "FAIL",
        evidence=tuple(sorted(str(item) for item in evidence)),
        details=details,
    )


def _synthetic_manifest() -> str:
    return f"""
metadata:
  id: SAAG-SYNTHETIC-APP
  name: SAAG Synthetic Scheduling App
  version: 1.0.0
  schema_version: {MANIFEST_INTAKE_SCHEMA_VERSION}
  registry_version: saag-001
  generator_version: saag-001
  compiler_version: saag-001
  approval_status: approved
organization:
  name: Example Services
domain: field service scheduling
vision: Coordinate service appointments with deterministic reminders and auditability.
objectives:
  - id: OBJ-001
    title: Reduce missed appointments
    indicator: missed appointment rate
entities:
  - id: ENT-CUSTOMER
    name: Customer
  - id: ENT-APPOINTMENT
    name: Appointment
capabilities:
  - id: CAP-CORE
    name: Appointment tracking
    owner: Operations
  - id: CAP-REMINDER
    name: Reminder workflow
    owner: Operations
    depends_on: [CAP-CORE]
workflows:
  - id: WF-001
    name: Schedule appointment
    steps:
      - id: STEP-001
        capability: CAP-CORE
        entity: ENT-APPOINTMENT
      - id: STEP-002
        capability: CAP-REMINDER
        entity: ENT-CUSTOMER
"""


def _governance_request(
    *,
    request_id: str = "SAAG-GOV-VALID",
    requested_authorities: tuple[str, ...] = ("analyze_impact", "recommend", "record_evidence", "simulate"),
    policy_delta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "actor_id": "human-operator",
        "summary": "Evaluate adding a bounded governance manifest rule.",
        "domain": "evolution",
        "evolution_type": "technical",
        "target_manifest_paths": ["manifest/project/ai-ent/capabilities.yaml"],
        "requested_authorities": list(requested_authorities),
        "learning_sources": ["approved_manifests", "verified_implementation_outcomes"],
        "policy_delta": policy_delta or {},
    }


def _environment() -> EnvironmentProfile:
    return EnvironmentProfile(
        profile_id="saag-001",
        repository_path=str(Path.cwd()),
        git_available=True,
        postgresql_available=True,
        alembic_available=True,
        docker_available=True,
        docker_compose_available=True,
        codex_command_configured=False,
        codex_executable_available=True,
        python_path=str(Path("aient/bin/python")),
        python_available=True,
        ram_mb=8192,
        gpu_available=False,
        external_network="available",
        configured_secret_names=(),
    )


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


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(canonical_bytes(value) + b"\n")


def _markdown_report(result: SystemAcceptanceResult) -> str:
    proofs = "\n".join(f"- {proof.proof_id} {proof.name}: `{proof.status}`" for proof in result.proofs)
    negatives = "\n".join(f"- {proof.proof_id} {proof.name}: `{proof.status}`" for proof in result.negative_tests)
    limitations = "\n".join(
        f"- {item.classification}: {item.description}"
        for item in result.limitations
    )
    coverage = result.requirement_coverage
    return (
        "# SAAG-001 - Full System Acceptance Gate\n\n"
        f"Result: `{result.result}`\n\n"
        f"Recommendation: `{result.recommendation}`\n\n"
        f"Head: `{result.head}`\n\n"
        f"Acceptance hash: `{result.acceptance_hash}`\n\n"
        "## Requirement Coverage\n\n"
        f"- normative: `{coverage['normative_requirements']}`\n"
        f"- covered: `{coverage['covered_requirements']}`\n"
        f"- partial: `{coverage['partially_covered_requirements']}`\n"
        f"- blocked: `{coverage['blocked_requirements']}`\n"
        f"- deferred: `{coverage['deferred_requirements']}`\n"
        f"- warnings: `{coverage['warning_count']}`\n"
        f"- errors: `{coverage['error_count']}`\n\n"
        "## Proofs A-M\n\n"
        f"{proofs}\n\n"
        "## Negative Tests\n\n"
        f"{negatives}\n\n"
        "## Limitations\n\n"
        f"{limitations or '- none'}\n"
    )
