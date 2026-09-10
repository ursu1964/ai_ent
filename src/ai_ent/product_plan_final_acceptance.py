from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_ent.persistence.config import load_database_settings
from ai_ent.persistence.database import Database
from ai_ent.persistence.models import (
    Execution,
    RuntimeHumanGate,
    RuntimePlanImport,
    RuntimeTaskPlanBinding,
    Task,
    TaskLease,
)
from ai_ent.product_decisions import EXPECTED_PFE_001_HASH
from ai_ent.product_dry_run import (
    EXPECTED_PPA_ACCEPTANCE_HASH,
    EXPECTED_PRD_DEC_001_HASH,
)
from ai_ent.product_plan_acceptance import EXPECTED_PRODUCTIZATION_PLAN_HASH
from ai_ent.project_manifest import GENERATED_MARKER, canonical_bytes

PPG_001_VERSION = "ppg-001.1"
EXPECTED_PRODUCT_DRY_RUN_HASH = "0db76609d8e1d080e1d9786a9fb38529ffd92103703ee192af2aeeaa924ab49f"
PRODUCT_PLAN_ID = "PRODUCT-PLAN-7b0342fb7fd5"

PPGResult = Literal["ACCEPTED", "ACCEPTED_WITH_AFFECTED_TASK_DECISIONS", "REJECTED"]


@dataclass(frozen=True)
class FrozenProductPlanAcceptance:
    gate_id: str
    gate_version: str
    generated_at: str
    baseline_commit: str
    result: PPGResult
    recommendation: str
    product_plan_id: str
    plan_version: str
    frozen_state: str
    bound_hashes: dict[str, str]
    counts: dict[str, int]
    risk_distribution: dict[str, int]
    policy_distribution: dict[str, int]
    decision_states: dict[str, Any]
    validations: dict[str, str]
    blockers: tuple[str, ...]
    limitations: tuple[str, ...]
    acceptance_hash: str
    security_acceptance: dict[str, Any]
    rbac_acceptance: dict[str, Any]
    external_project_runtime_acceptance: dict[str, Any]
    surface_coverage: dict[str, Any]
    networking_policy: dict[str, Any]
    secrets_policy: dict[str, Any]
    docker_boundary: dict[str, Any]
    recovery_compatibility: dict[str, Any]
    import_compatibility: dict[str, Any]
    runtime_snapshot: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.result in {"ACCEPTED", "ACCEPTED_WITH_AFFECTED_TASK_DECISIONS"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "gate_id": self.gate_id,
            "gate_version": self.gate_version,
            "generated_at": self.generated_at,
            "baseline_commit": self.baseline_commit,
            "result": self.result,
            "recommendation": self.recommendation,
            "product_plan_id": self.product_plan_id,
            "plan_version": self.plan_version,
            "frozen_state": self.frozen_state,
            "bound_hashes": self.bound_hashes,
            "counts": self.counts,
            "risk_distribution": self.risk_distribution,
            "policy_distribution": self.policy_distribution,
            "decision_states": self.decision_states,
            "validations": self.validations,
            "blockers": list(self.blockers),
            "limitations": list(self.limitations),
            "acceptance_hash": self.acceptance_hash,
            "security_acceptance": self.security_acceptance,
            "rbac_acceptance": self.rbac_acceptance,
            "external_project_runtime_acceptance": self.external_project_runtime_acceptance,
            "surface_coverage": self.surface_coverage,
            "networking_policy": self.networking_policy,
            "secrets_policy": self.secrets_policy,
            "docker_boundary": self.docker_boundary,
            "recovery_compatibility": self.recovery_compatibility,
            "import_compatibility": self.import_compatibility,
            "runtime_snapshot": self.runtime_snapshot,
        }


def evaluate_frozen_product_plan_acceptance(
    *,
    plan_path: Path = Path(".build/compiled/productization-plan.json"),
    ppa_path: Path = Path("artifacts/ppa-001/PPA-001.json"),
    pfe_path: Path = Path("artifacts/pfe-001/PFE-001.json"),
    decision_path: Path = Path("artifacts/product-decisions/PRD-DEC-001.json"),
    pdf_path: Path = Path("artifacts/pdf-001/PDF-001.json"),
    lock_path: Path = Path(".build/compiled/product-plan.lock"),
    import_preview_path: Path = Path(".build/compiled/product-task-import-preview.json"),
    repository_root: Path = Path("."),
    env_file: Path = Path(".env"),
    expected_product_plan_hash: str = EXPECTED_PRODUCTIZATION_PLAN_HASH,
    expected_ppa_hash: str = EXPECTED_PPA_ACCEPTANCE_HASH,
    expected_pfe_hash: str = EXPECTED_PFE_001_HASH,
    expected_decision_hash: str = EXPECTED_PRD_DEC_001_HASH,
    expected_pdf_hash: str = EXPECTED_PRODUCT_DRY_RUN_HASH,
    generated_at: str | None = None,
    runtime_snapshot_override: dict[str, Any] | None = None,
) -> FrozenProductPlanAcceptance:
    plan = _read_json(plan_path)
    ppa = _read_json(ppa_path)
    pfe = _read_json(pfe_path)
    decision = _read_json(decision_path)
    pdf = _read_json(pdf_path)
    lock = _read_json(lock_path)
    import_preview = _read_json(import_preview_path)
    baseline = _git_rev(repository_root, "HEAD")
    runtime_snapshot = runtime_snapshot_override or _runtime_snapshot(
        env_file=env_file,
        project_id="PRJ-AI-ENT",
        plan_id=PRODUCT_PLAN_ID,
    )
    validations = _validations(
        plan=plan,
        ppa=ppa,
        pfe=pfe,
        decision=decision,
        pdf=pdf,
        lock=lock,
        import_preview=import_preview,
        runtime_snapshot=runtime_snapshot,
        expected_product_plan_hash=expected_product_plan_hash,
        expected_ppa_hash=expected_ppa_hash,
        expected_pfe_hash=expected_pfe_hash,
        expected_decision_hash=expected_decision_hash,
        expected_pdf_hash=expected_pdf_hash,
    )
    blockers = tuple(sorted(name for name, status in validations.items() if status != "PASS"))
    decision_states = _decision_states(pdf)
    affected_decisions = tuple(
        decision_id
        for decision_id in ("PRD-DEC-002", "PRD-DEC-003")
        if decision_states.get(decision_id, {}).get("state")
        == "UNRESOLVED_MUST_RESOLVE_BEFORE_AFFECTED_TASK"
    )
    if blockers:
        result: PPGResult = "REJECTED"
        recommendation = "PRODUCT_PLAN_REMEDIATION_REQUIRED"
    elif affected_decisions:
        result = "ACCEPTED_WITH_AFFECTED_TASK_DECISIONS"
        recommendation = "READY_FOR_PHI_001"
    else:
        result = "ACCEPTED"
        recommendation = "READY_FOR_PHI_001"
    limitations = tuple(
        sorted(
            f"{decision_id} remains unresolved and enforced at affected task boundary"
            for decision_id in affected_decisions
        )
    )
    material = {
        "baseline_commit": baseline,
        "bound_hashes": _bound_hashes(
            plan=plan,
            ppa=ppa,
            pfe=pfe,
            decision=decision,
            pdf=pdf,
            lock=lock,
        ),
        "counts": _counts(plan=plan, pdf=pdf),
        "decision_states": decision_states,
        "gate_id": "PPG-001",
        "gate_version": PPG_001_VERSION,
        "import_compatibility": _import_compatibility(import_preview, runtime_snapshot),
        "policy_distribution": dict(pdf.get("summary", {}).get("policy_distribution", {})),
        "product_plan_id": str(pdf.get("product_plan_id")),
        "result": result,
        "risk_distribution": dict(pdf.get("summary", {}).get("risk_distribution", {})),
        "validations": validations,
    }
    acceptance_hash = hashlib.sha256(canonical_bytes(material)).hexdigest()
    return FrozenProductPlanAcceptance(
        gate_id="PPG-001",
        gate_version=PPG_001_VERSION,
        generated_at=generated_at
        or dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        baseline_commit=baseline,
        result=result,
        recommendation=recommendation,
        product_plan_id=str(pdf.get("product_plan_id")),
        plan_version=str(pdf.get("plan_version")),
        frozen_state=str(pdf.get("frozen_plan_state")),
        bound_hashes=material["bound_hashes"],
        counts=material["counts"],
        risk_distribution=material["risk_distribution"],
        policy_distribution=material["policy_distribution"],
        decision_states=decision_states,
        validations=validations,
        blockers=blockers,
        limitations=limitations,
        acceptance_hash=acceptance_hash,
        security_acceptance=_security_acceptance(pdf),
        rbac_acceptance=_rbac_acceptance(pdf),
        external_project_runtime_acceptance=_external_project_runtime_acceptance(pdf, plan),
        surface_coverage=_surface_coverage(plan, pdf),
        networking_policy=_networking_policy(pdf),
        secrets_policy=_secrets_policy(pdf),
        docker_boundary=_docker_boundary(pdf),
        recovery_compatibility=_recovery_compatibility(pdf),
        import_compatibility=material["import_compatibility"],
        runtime_snapshot=runtime_snapshot,
    )


def write_frozen_product_plan_acceptance(
    *,
    plan_path: Path = Path(".build/compiled/productization-plan.json"),
    ppa_path: Path = Path("artifacts/ppa-001/PPA-001.json"),
    pfe_path: Path = Path("artifacts/pfe-001/PFE-001.json"),
    decision_path: Path = Path("artifacts/product-decisions/PRD-DEC-001.json"),
    pdf_path: Path = Path("artifacts/pdf-001/PDF-001.json"),
    lock_path: Path = Path(".build/compiled/product-plan.lock"),
    import_preview_path: Path = Path(".build/compiled/product-task-import-preview.json"),
    artifacts_dir: Path = Path("artifacts"),
    repository_root: Path = Path("."),
    env_file: Path = Path(".env"),
    expected_product_plan_hash: str = EXPECTED_PRODUCTIZATION_PLAN_HASH,
    expected_ppa_hash: str = EXPECTED_PPA_ACCEPTANCE_HASH,
    expected_pfe_hash: str = EXPECTED_PFE_001_HASH,
    expected_decision_hash: str = EXPECTED_PRD_DEC_001_HASH,
    expected_pdf_hash: str = EXPECTED_PRODUCT_DRY_RUN_HASH,
    runtime_snapshot_override: dict[str, Any] | None = None,
) -> FrozenProductPlanAcceptance:
    gate = evaluate_frozen_product_plan_acceptance(
        plan_path=plan_path,
        ppa_path=ppa_path,
        pfe_path=pfe_path,
        decision_path=decision_path,
        pdf_path=pdf_path,
        lock_path=lock_path,
        import_preview_path=import_preview_path,
        repository_root=repository_root,
        env_file=env_file,
        expected_product_plan_hash=expected_product_plan_hash,
        expected_ppa_hash=expected_ppa_hash,
        expected_pfe_hash=expected_pfe_hash,
        expected_decision_hash=expected_decision_hash,
        expected_pdf_hash=expected_pdf_hash,
        runtime_snapshot_override=runtime_snapshot_override,
    )
    artifact_dir = artifacts_dir / "ppg-001"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "PPG-001.json").write_bytes(canonical_bytes(gate.as_dict()) + b"\n")
    (artifact_dir / "PPG-001.md").write_text(_markdown(gate), encoding="utf-8")
    return gate


def _validations(
    *,
    plan: dict[str, Any],
    ppa: dict[str, Any],
    pfe: dict[str, Any],
    decision: dict[str, Any],
    pdf: dict[str, Any],
    lock: dict[str, Any],
    import_preview: dict[str, Any],
    runtime_snapshot: dict[str, Any],
    expected_product_plan_hash: str,
    expected_ppa_hash: str,
    expected_pfe_hash: str,
    expected_decision_hash: str,
    expected_pdf_hash: str,
) -> dict[str, str]:
    summary = dict(pdf.get("summary", {}))
    previews = tuple(dict(item) for item in import_preview.get("task_import_preview", []))
    task_ids = {str(item.get("task_id")) for item in previews}
    edges = tuple((str(edge[0]), str(edge[1])) for edge in lock.get("dependency_edges", []))
    gates = tuple(dict(item) for item in lock.get("human_gate_definitions", []))
    plan_dag = dict(plan.get("productization_dag", {}))
    return {
        "frozen_identity": _pass(
            pdf.get("product_plan_id") == f"PRODUCT-PLAN-{expected_product_plan_hash[:12]}"
            and pdf.get("plan_version") == "1"
            and pdf.get("frozen_plan_state") == "FROZEN"
            and lock.get("product_plan_id") == f"PRODUCT-PLAN-{expected_product_plan_hash[:12]}"
            and lock.get("plan_version") == "1"
            and lock.get("state") == "FROZEN"
        ),
        "accepted_lineage_hashes": _pass(
            plan.get("plan_hash") == expected_product_plan_hash
            and ppa.get("acceptance_hash") == expected_ppa_hash
            and pfe.get("product_feasibility_hash") == expected_pfe_hash
            and decision.get("decision_hash") == expected_decision_hash
            and pdf.get("product_dry_run_hash") == expected_pdf_hash
            and lock.get("product_dry_run_hash") == expected_pdf_hash
            and pdf.get("accepted_prd_plan_hash") == expected_product_plan_hash
            and pdf.get("ppa_acceptance_hash") == expected_ppa_hash
            and pdf.get("pfe_feasibility_hash") == expected_pfe_hash
            and pdf.get("prd_dec_001_hash") == expected_decision_hash
        ),
        "pdf_lineage_reconciliation": _pass(
            pdf.get("lineage_reconciliation", {}).get("result") == "PASS"
            and pdf.get("lineage_reconciliation", {}).get("material_difference_count") == 0
            and set(pdf.get("lineage_reconciliation", {}).get("classification", []))
            <= {"DECISION_BINDING", "PROVENANCE_ONLY", "TOOLING_ONLY"}
        ),
        "task_contracts_complete": _pass(len(previews) == 25 and all(_preview_complete(item) for item in previews)),
        "counts_match": _pass(
            summary.get("task_count") == 25
            and summary.get("dependency_edge_count") == 51
            and summary.get("wave_count") == 9
            and summary.get("human_gate_count") == 8
            and summary.get("theoretical_parallel_width") == 6
            and summary.get("effective_concurrency") == 1
        ),
        "risk_distribution": _pass(summary.get("risk_distribution") == {"HIGH": 8, "LOW": 2, "MEDIUM": 15}),
        "policy_distribution": _pass(
            summary.get("policy_distribution")
            == {"AUTO_ALLOWED": 2, "GUARDED_ALLOWED": 15, "HUMAN_APPROVAL_REQUIRED": 8}
        ),
        "execution_limits": _pass(
            summary.get("execution_limits")
            == {
                "effective_concurrency": 1,
                "max_failures_per_run": 1,
                "max_repairs_per_task": 1,
                "max_tasks_per_run": 1,
            }
        ),
        "dependency_graph": _pass(
            len(edges) == 51
            and not any(task == dependency for task, dependency in edges)
            and all(task in task_ids and dependency in task_ids for task, dependency in edges)
            and not _has_cycle(task_ids, edges)
        ),
        "waves_and_critical_path": _pass(
            len(plan_dag.get("waves", [])) == 9
            and plan_dag.get("critical_path")
            == [
                "PRD-TASK-005",
                "PRD-TASK-006",
                "PRD-TASK-007",
                "PRD-TASK-008",
                "PRD-TASK-010",
                "PRD-TASK-018",
                "PRD-TASK-023",
                "PRD-TASK-024",
                "PRD-TASK-025",
            ]
        ),
        "prd_dec_001_frozen": _pass(
            pdf.get("decision_boundaries", {}).get("PRD-DEC-001", {}).get("state") == "ACCEPTED"
            and decision.get("accepted_stack")
            == {
                "backend_api": "FastAPI + Pydantic",
                "frontend": "React + TypeScript + Vite",
                "realtime": "SSE-first",
            }
        ),
        "prd_dec_002_boundary": _pass(
            pdf.get("decision_boundaries", {}).get("PRD-DEC-002", {}).get("state")
            == "UNRESOLVED_MUST_RESOLVE_BEFORE_AFFECTED_TASK"
            and pdf.get("decision_boundaries", {}).get("PRD-DEC-002", {}).get("affected_tasks")
            == ["PRD-TASK-019", "PRD-TASK-023", "PRD-TASK-025"]
            and _batches_stop_for_decision(pdf, "PRD-DEC-002")
        ),
        "prd_dec_003_boundary": _pass(
            pdf.get("decision_boundaries", {}).get("PRD-DEC-003", {}).get("state")
            == "UNRESOLVED_MUST_RESOLVE_BEFORE_AFFECTED_TASK"
            and pdf.get("decision_boundaries", {}).get("PRD-DEC-003", {}).get("affected_tasks")
            == ["PRD-TASK-020", "PRD-TASK-023", "PRD-TASK-025"]
            and _batches_stop_for_decision(pdf, "PRD-DEC-003")
        ),
        "human_gates_pending": _pass(
            len(gates) == 8
            and all(gate.get("status") == "PENDING_NOT_APPROVED" for gate in gates)
            and all(gate.get("approval_boundary") for gate in gates)
            and all(gate.get("expected_evidence") for gate in gates)
        ),
        "security_acceptance": _pass(_all_security_checks_pass(pdf)),
        "rbac_acceptance": _pass(_rbac_acceptance(pdf)["result"] == "PASS"),
        "external_project_runtime_generic": _pass(
            _external_project_runtime_acceptance(pdf, plan)["result"] == "PASS"
        ),
        "product_surface_coverage": _pass(
            len(plan.get("product_pages", [])) == 16
            and len(plan.get("api_domains", [])) == 15
            and len(plan.get("promoted_requirements", [])) == 12
            and pdf.get("ui_dry_run", {}).get("result") == "PASS"
            and pdf.get("api_dry_run", {}).get("result") == "PASS"
        ),
        "networking_policy": _pass(
            _networking_policy(pdf)
            == {
                "lan": "DECISION_GATED_BY_PRD-DEC-002",
                "localhost": "ALLOWED",
                "public_internet": "PROHIBITED_NOT_IN_CURRENT_PLAN",
            }
        ),
        "secrets_policy": _pass(
            _secrets_policy(pdf).get("production_backend") == "DECISION_GATED_BY_PRD-DEC-003"
            and _secrets_policy(pdf).get("secret_values_in_artifacts") == "DENIED"
        ),
        "docker_boundary": _pass(
            set(_docker_boundary(pdf).get("allowed_minimum", []))
            == {"build", "start", "health", "logs", "restart", "stop"}
            and _docker_boundary(pdf).get("unrestricted_daemon_control") == "DENIED"
        ),
        "recovery_coverage": _pass(
            {
                "after_claim",
                "after_worktree_creation",
                "during_executor",
                "after_executor",
                "after_verification",
                "after_commit",
                "before_db_completion",
                "at_human_gate",
                "at_unresolved_decision_boundary",
                "between_tasks",
            }
            <= set(pdf.get("recovery_points", []))
        ),
        "runtime_import_compatibility": _pass(
            _import_compatibility(import_preview, runtime_snapshot)["result"] == "PASS"
        ),
        "runtime_not_started": _pass(
            runtime_snapshot.get("product_tasks_imported") == 0
            and runtime_snapshot.get("product_executions") == 0
            and runtime_snapshot.get("product_gates_approved") == 0
            and runtime_snapshot.get("deployment_actions") == 0
        ),
    }


def _preview_complete(preview: dict[str, Any]) -> bool:
    required = (
        "task_id",
        "fingerprint",
        "depends_on",
        "task_type",
        "risk_level",
        "policy_decision",
        "agent_role",
        "model_profile",
        "executor",
        "verification_profile",
        "allowed_write_scope",
        "prohibited_paths",
        "acceptance_criteria",
        "required_decisions",
    )
    return all(key in preview and preview[key] not in ("", None) for key in required)


def _has_cycle(task_ids: set[str], edges: tuple[tuple[str, str], ...]) -> bool:
    graph: dict[str, list[str]] = {task_id: [] for task_id in task_ids}
    for task, dependency in edges:
        graph.setdefault(task, []).append(dependency)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> bool:
        if task_id in visiting:
            return True
        if task_id in visited:
            return False
        visiting.add(task_id)
        for dependency in graph.get(task_id, []):
            if visit(dependency):
                return True
        visiting.remove(task_id)
        visited.add(task_id)
        return False

    return any(visit(task_id) for task_id in sorted(task_ids))


def _batches_stop_for_decision(pdf: dict[str, Any], decision_id: str) -> bool:
    affected = set(
        pdf.get("decision_boundaries", {})
        .get(decision_id, {})
        .get("affected_tasks", [])
    )
    batches = pdf.get("execution_batches", [])
    return bool(affected) and all(
        any(
            batch.get("task_ids") == [task_id]
            and decision_id in batch.get("required_human_gates", [])
            and batch.get("resume_after") in batch.get("required_human_gates", [])
            for batch in batches
        )
        for task_id in affected
    )


def _bound_hashes(
    *,
    plan: dict[str, Any],
    ppa: dict[str, Any],
    pfe: dict[str, Any],
    decision: dict[str, Any],
    pdf: dict[str, Any],
    lock: dict[str, Any],
) -> dict[str, str]:
    return {
        "accepted_prd_hash": str(plan.get("plan_hash")),
        "ppa_acceptance_hash": str(ppa.get("acceptance_hash")),
        "pfe_feasibility_hash": str(pfe.get("product_feasibility_hash")),
        "prd_dec_001_hash": str(decision.get("decision_hash")),
        "pdf_dry_run_hash": str(pdf.get("product_dry_run_hash")),
        "frozen_plan_lock_hash": hashlib.sha256(canonical_bytes(lock)).hexdigest(),
        "task_contracts_hash": str(lock.get("task_contracts_hash")),
        "dependency_graph_hash": str(lock.get("dependency_graph_hash")),
        "security_policy_hash": str(lock.get("security_policy_hash")),
    }


def _counts(*, plan: dict[str, Any], pdf: dict[str, Any]) -> dict[str, int]:
    summary = dict(pdf.get("summary", {}))
    return {
        "tasks": int(summary.get("task_count", 0)),
        "dependency_edges": int(summary.get("dependency_edge_count", 0)),
        "waves": int(summary.get("wave_count", 0)),
        "human_gates": int(summary.get("human_gate_count", 0)),
        "theoretical_parallel_width": int(summary.get("theoretical_parallel_width", 0)),
        "effective_concurrency": int(summary.get("effective_concurrency", 0)),
        "ui_pages": len(plan.get("product_pages", [])),
        "api_domains": len(plan.get("api_domains", [])),
        "promoted_requirements": len(plan.get("promoted_requirements", [])),
        "dry_run_errors": int(summary.get("dry_run_errors", 0)),
        "dry_run_warnings": int(summary.get("dry_run_warnings", 0)),
    }


def _decision_states(pdf: dict[str, Any]) -> dict[str, Any]:
    return dict(pdf.get("decision_boundaries", {}))


def _security_acceptance(pdf: dict[str, Any]) -> dict[str, Any]:
    checks = dict(pdf.get("security_dry_run", {}).get("checks", {}))
    return {
        "result": "PASS" if checks and all(value == "PASS" for value in checks.values()) else "FAIL",
        "checks": checks,
    }


def _all_security_checks_pass(pdf: dict[str, Any]) -> bool:
    required = {
        "api_cannot_bypass_control_plane",
        "api_cannot_forge_verification_pass",
        "api_cannot_manipulate_leases",
        "api_cannot_mark_arbitrary_task_passed",
        "artifact_outside_project_rejected",
        "artifact_path_traversal_rejected",
        "generated_app_control_plane_secret_access_rejected",
        "public_network_ingress_not_enabled",
        "secret_paths_not_served",
        "sse_authority_commands_rejected",
        "ui_implicit_approval_rejected",
        "unauthenticated_api_access_rejected",
        "unauthorized_approval_rejected",
        "unauthorized_project_access_rejected",
        "unrestricted_docker_operation_denied",
    }
    checks = dict(pdf.get("security_dry_run", {}).get("checks", {}))
    return required <= set(checks) and all(checks[key] == "PASS" for key in required)


def _rbac_acceptance(pdf: dict[str, Any]) -> dict[str, Any]:
    rbac = dict(pdf.get("rbac_dry_run", {}))
    roles = set(dict(rbac.get("roles", {})))
    required = {"anonymous", "authenticated_user", "operator", "approver", "administrator"}
    return {
        "result": "PASS" if rbac.get("result") == "PASS" and required <= roles else "FAIL",
        "roles": sorted(roles),
        "boundaries": rbac.get("boundaries", {}),
    }


def _external_project_runtime_acceptance(pdf: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    runtime = dict(pdf.get("external_project_runtime_dry_run", {}))
    expected_lifecycle = [
        "CREATE",
        "INTAKE",
        "PLAN",
        "FREEZE",
        "APPROVE",
        "IMPORT",
        "EXECUTE",
        "VERIFY",
        "COMPLETE",
        "RUN",
        "ARCHIVE",
    ]
    plan_runtime = dict(plan.get("external_project_runtime", {}))
    return {
        "result": "PASS"
        if runtime.get("result") == "PASS"
        and set(runtime.get("lifecycle", [])) == set(expected_lifecycle)
        and runtime.get("hardcoded_e2e_identity_used") is False
        and set(plan_runtime.get("lifecycle", [])) == set(expected_lifecycle)
        else "FAIL",
        "lifecycle": runtime.get("lifecycle", []),
        "hardcoded_e2e_identity_used": runtime.get("hardcoded_e2e_identity_used"),
        "entities": plan_runtime.get("entities", []),
    }


def _surface_coverage(plan: dict[str, Any], pdf: dict[str, Any]) -> dict[str, Any]:
    return {
        "result": "PASS"
        if len(plan.get("product_pages", [])) == 16
        and len(plan.get("api_domains", [])) == 15
        and len(plan.get("promoted_requirements", [])) == 12
        and pdf.get("ui_dry_run", {}).get("result") == "PASS"
        and pdf.get("api_dry_run", {}).get("result") == "PASS"
        else "FAIL",
        "ui_pages": [item.get("page") for item in plan.get("product_pages", [])],
        "api_domains": [item.get("name") for item in plan.get("api_domains", [])],
        "promoted_requirements": len(plan.get("promoted_requirements", [])),
    }


def _networking_policy(pdf: dict[str, Any]) -> dict[str, Any]:
    return dict(pdf.get("docker_network_secrets_dry_run", {}).get("networking", {}))


def _secrets_policy(pdf: dict[str, Any]) -> dict[str, Any]:
    return dict(pdf.get("docker_network_secrets_dry_run", {}).get("secrets", {}))


def _docker_boundary(pdf: dict[str, Any]) -> dict[str, Any]:
    return dict(pdf.get("docker_network_secrets_dry_run", {}).get("docker_operations", {}))


def _recovery_compatibility(pdf: dict[str, Any]) -> dict[str, Any]:
    required = {
        "after_claim",
        "after_worktree_creation",
        "during_executor",
        "after_executor",
        "after_verification",
        "after_commit",
        "before_db_completion",
        "at_human_gate",
        "at_unresolved_decision_boundary",
        "between_tasks",
    }
    actual = set(pdf.get("recovery_points", []))
    return {
        "result": "PASS" if required <= actual else "FAIL",
        "covered": sorted(required & actual),
        "missing": sorted(required - actual),
    }


def _import_compatibility(
    import_preview: dict[str, Any],
    runtime_snapshot: dict[str, Any],
) -> dict[str, Any]:
    previews = tuple(dict(item) for item in import_preview.get("task_import_preview", []))
    return {
        "result": "PASS"
        if len(previews) == 25
        and all(_preview_complete(item) for item in previews)
        and runtime_snapshot.get("product_tasks_imported") == 0
        and runtime_snapshot.get("product_executions") == 0
        and runtime_snapshot.get("product_gates_approved") == 0
        else "FAIL",
        "preview_task_count": len(previews),
        "runtime_schema_fields": [
            "task_id",
            "fingerprint",
            "dependencies",
            "risk",
            "policy_decision",
            "agent_role",
            "model_profile",
            "executor",
            "verification_profile",
            "write_scope",
            "acceptance_criteria",
            "human_gates",
            "decision_dependencies",
        ],
        "coexists_with": ["PLAN-1a75a2e3c5a7 v1", "RESIDUAL-PLAN-a918c449cfe5 v1"],
        "idempotency_requirements": {
            "exact_repeat_import": "ALREADY_IMPORTED / IN_SYNC",
            "material_mismatch": "CONFLICT",
            "partial_failure": "rollback",
            "duplicates": "denied",
        },
    }


def _runtime_snapshot(
    *,
    env_file: Path,
    project_id: str,
    plan_id: str,
) -> dict[str, Any]:
    database = Database(load_database_settings(env_file))
    try:
        with database.session() as session:
            return _runtime_snapshot_from_session(session, project_id=project_id, plan_id=plan_id)
    finally:
        database.dispose()


def _runtime_snapshot_from_session(
    session: Session,
    *,
    project_id: str,
    plan_id: str,
) -> dict[str, Any]:
    product_tasks = (
        session.execute(
            select(func.count())
            .select_from(RuntimeTaskPlanBinding)
            .where(RuntimeTaskPlanBinding.plan_id == plan_id)
        ).scalar_one()
        or 0
    )
    product_task_ids = [
        row[0]
        for row in session.execute(
            select(RuntimeTaskPlanBinding.task_id).where(RuntimeTaskPlanBinding.plan_id == plan_id)
        ).all()
    ]
    product_executions = 0
    active_leases = 0
    if product_task_ids:
        product_executions = (
            session.execute(
                select(func.count()).select_from(Execution).where(Execution.task_id.in_(product_task_ids))
            ).scalar_one()
            or 0
        )
        active_leases = (
            session.execute(
                select(func.count())
                .select_from(TaskLease)
                .where(TaskLease.task_id.in_(product_task_ids), TaskLease.status == "active")
            ).scalar_one()
            or 0
        )
    product_gates_approved = (
        session.execute(
            select(func.count())
            .select_from(RuntimeHumanGate)
            .where(RuntimeHumanGate.plan_id == plan_id, RuntimeHumanGate.status == "approved")
        ).scalar_one()
        or 0
    )
    product_imports = (
        session.execute(
            select(func.count())
            .select_from(RuntimePlanImport)
            .where(RuntimePlanImport.plan_id == plan_id, RuntimePlanImport.plan_version == "1")
        ).scalar_one()
        or 0
    )
    prior_plan_completed = _passed_count(session, "PLAN-1a75a2e3c5a7") == 13
    residual_plan_completed = _passed_count(session, "RESIDUAL-PLAN-a918c449cfe5") == 7
    return {
        "postgresql_authority": "active",
        "project_id": project_id,
        "product_plan_imports": int(product_imports),
        "product_tasks_imported": int(product_tasks),
        "product_executions": int(product_executions),
        "product_gates_approved": int(product_gates_approved),
        "product_active_leases": int(active_leases),
        "deployment_actions": 0,
        "prior_plan_13_passed": prior_plan_completed,
        "residual_plan_7_passed": residual_plan_completed,
    }


def _passed_count(session: Session, plan_id: str) -> int:
    return int(
        session.execute(
            select(func.count())
            .select_from(Task)
            .join(RuntimeTaskPlanBinding, RuntimeTaskPlanBinding.task_id == Task.id)
            .where(RuntimeTaskPlanBinding.plan_id == plan_id, Task.status == "passed")
        ).scalar_one()
        or 0
    )


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _git_rev(cwd: Path, rev: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", rev],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"


def _pass(condition: bool) -> str:
    return "PASS" if condition else "FAIL"


def _markdown(gate: FrozenProductPlanAcceptance) -> str:
    lines = [
        "# PPG-001 Frozen Product Plan Acceptance Gate",
        "",
        f"Result: {gate.result}",
        f"Recommendation: {gate.recommendation}",
        f"Baseline commit: `{gate.baseline_commit}`",
        f"Frozen product plan: `{gate.product_plan_id}` / v{gate.plan_version} / {gate.frozen_state}",
        f"Acceptance hash: `{gate.acceptance_hash}`",
        "",
        "## Bound Hashes",
    ]
    lines.extend(f"- {key}: `{value}`" for key, value in gate.bound_hashes.items())
    lines.extend(["", "## Counts"])
    lines.extend(f"- {key}: {value}" for key, value in gate.counts.items())
    lines.extend(["", "## Decisions"])
    lines.extend(
        f"- {decision_id}: {state.get('state')}"
        for decision_id, state in sorted(gate.decision_states.items())
    )
    lines.extend(["", "## Validations"])
    lines.extend(f"- {key}: {value}" for key, value in gate.validations.items())
    if gate.limitations:
        lines.extend(["", "## Limitations"])
        lines.extend(f"- {item}" for item in gate.limitations)
    if gate.blockers:
        lines.extend(["", "## Blockers"])
        lines.extend(f"- {item}" for item in gate.blockers)
    lines.append("")
    return "\n".join(lines)
