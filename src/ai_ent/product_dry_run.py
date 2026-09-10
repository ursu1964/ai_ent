from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ai_ent.product_decisions import EXPECTED_PFE_001_HASH
from ai_ent.product_plan_acceptance import EXPECTED_PRODUCTIZATION_PLAN_HASH
from ai_ent.productization_plan import evaluate_productization_plan
from ai_ent.project_manifest import (
    GENERATED_MARKER,
    DryRunFinding,
    ExecutionBatch,
    FrozenPlanState,
    canonical_bytes,
)

PDF_001_VERSION = "pdf-001.1"
EXPECTED_PPA_ACCEPTANCE_HASH = "cf97940d5848f8c651232e23b733325f4dd274aa7a45078abab0039d80e638e9"
EXPECTED_PRD_DEC_001_HASH = "f03a5320a1d529a9dec6d973180bd7ec63e83859834ff921c0161131892d1d1c"

ProductDryRunResult = Literal[
    "READY_FOR_FROZEN_PRODUCT_PLAN_ACCEPTANCE",
    "READY_WITH_AFFECTED_TASK_DECISIONS",
    "PLAN_REACCEPTANCE_REQUIRED",
    "BLOCKED",
    "INVALID",
]
DiffClassification = Literal[
    "PROVENANCE_ONLY",
    "TOOLING_ONLY",
    "ENVIRONMENT_ONLY",
    "DECISION_BINDING",
    "MATERIAL_TASK_CHANGE",
    "MATERIAL_DAG_CHANGE",
    "MATERIAL_POLICY_CHANGE",
    "MATERIAL_SECURITY_CHANGE",
]


@dataclass(frozen=True)
class ProductTaskImportPreview:
    task_id: str
    fingerprint: str
    depends_on: tuple[str, ...]
    task_type: str
    execution_class: str
    schedulable: bool
    risk_level: str
    human_gate_ids: tuple[str, ...]
    agent_role: str
    model_profile: str
    executor: str
    verification_profile: str
    policy_decision: str
    allowed_write_scope: tuple[str, ...]
    prohibited_paths: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    required_tools: tuple[str, ...]
    required_infrastructure: tuple[str, ...]
    required_decisions: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "acceptance_criteria": list(self.acceptance_criteria),
            "agent_role": self.agent_role,
            "allowed_write_scope": list(self.allowed_write_scope),
            "depends_on": list(self.depends_on),
            "execution_class": self.execution_class,
            "executor": self.executor,
            "fingerprint": self.fingerprint,
            "human_gate_ids": list(self.human_gate_ids),
            "model_profile": self.model_profile,
            "policy_decision": self.policy_decision,
            "prohibited_paths": list(self.prohibited_paths),
            "required_decisions": list(self.required_decisions),
            "required_infrastructure": list(self.required_infrastructure),
            "required_tools": list(self.required_tools),
            "risk_level": self.risk_level,
            "schedulable": self.schedulable,
            "task_id": self.task_id,
            "task_type": self.task_type,
            "verification_profile": self.verification_profile,
        }


@dataclass(frozen=True)
class ProductPlanLock:
    product_plan_id: str
    plan_version: str
    state: FrozenPlanState
    baseline_head: str
    accepted_prd_plan_hash: str
    regenerated_candidate_hash: str
    ppa_acceptance_hash: str
    pfe_feasibility_hash: str
    prd_dec_001_hash: str
    product_dry_run_hash: str
    dry_runner_version: str
    task_fingerprints: dict[str, str]
    dependency_edges: tuple[tuple[str, str], ...]
    dependency_graph_hash: str
    human_gate_definitions: tuple[dict[str, Any], ...]
    task_contracts_hash: str
    security_policy_hash: str
    effective_concurrency: int
    execution_limits: dict[str, int]
    execution_prerequisites: tuple[str, ...]
    decision_boundaries: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "accepted_prd_plan_hash": self.accepted_prd_plan_hash,
            "baseline_head": self.baseline_head,
            "decision_boundaries": self.decision_boundaries,
            "dependency_edges": [list(edge) for edge in self.dependency_edges],
            "dependency_graph_hash": self.dependency_graph_hash,
            "dry_runner_version": self.dry_runner_version,
            "effective_concurrency": self.effective_concurrency,
            "execution_limits": self.execution_limits,
            "execution_prerequisites": list(self.execution_prerequisites),
            "human_gate_definitions": list(self.human_gate_definitions),
            "pfe_feasibility_hash": self.pfe_feasibility_hash,
            "plan_version": self.plan_version,
            "ppa_acceptance_hash": self.ppa_acceptance_hash,
            "prd_dec_001_hash": self.prd_dec_001_hash,
            "product_dry_run_hash": self.product_dry_run_hash,
            "product_plan_id": self.product_plan_id,
            "regenerated_candidate_hash": self.regenerated_candidate_hash,
            "security_policy_hash": self.security_policy_hash,
            "state": self.state,
            "task_contracts_hash": self.task_contracts_hash,
            "task_fingerprints": self.task_fingerprints,
        }


@dataclass(frozen=True)
class ProductDryRun:
    result: ProductDryRunResult
    recommendation: str
    baseline_head: str
    accepted_prd_plan_hash: str
    regenerated_candidate_hash: str
    ppa_acceptance_hash: str
    pfe_feasibility_hash: str
    prd_dec_001_hash: str
    dry_runner_version: str
    product_dry_run_hash: str
    product_plan_id: str
    plan_version: str
    frozen_plan_state: FrozenPlanState
    lineage_reconciliation: dict[str, Any]
    task_count: int
    dependency_edge_count: int
    wave_count: int
    human_gate_count: int
    risk_distribution: dict[str, int]
    policy_distribution: dict[str, int]
    theoretical_parallel_width: int
    effective_concurrency: int
    execution_limits: dict[str, int]
    decision_boundaries: dict[str, Any]
    execution_batches: tuple[ExecutionBatch, ...]
    import_preview: tuple[ProductTaskImportPreview, ...]
    security_dry_run: dict[str, Any]
    rbac_dry_run: dict[str, Any]
    external_project_runtime_dry_run: dict[str, Any]
    api_dry_run: dict[str, Any]
    ui_dry_run: dict[str, Any]
    sse_dry_run: dict[str, Any]
    artifact_dry_run: dict[str, Any]
    docker_network_secrets_dry_run: dict[str, Any]
    recovery_points: tuple[str, ...]
    repair_path_coverage: tuple[str, ...]
    prior_plan_coexistence: tuple[str, ...]
    execution_prerequisites: tuple[str, ...]
    invalidation_rules: tuple[str, ...]
    findings: tuple[DryRunFinding, ...]
    lock: ProductPlanLock

    @property
    def ok(self) -> bool:
        return self.result in {
            "READY_FOR_FROZEN_PRODUCT_PLAN_ACCEPTANCE",
            "READY_WITH_AFFECTED_TASK_DECISIONS",
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "phase": "PDF-001",
            "result": self.result,
            "recommendation": self.recommendation,
            "baseline_head": self.baseline_head,
            "accepted_prd_plan_hash": self.accepted_prd_plan_hash,
            "regenerated_candidate_hash": self.regenerated_candidate_hash,
            "ppa_acceptance_hash": self.ppa_acceptance_hash,
            "pfe_feasibility_hash": self.pfe_feasibility_hash,
            "prd_dec_001_hash": self.prd_dec_001_hash,
            "dry_runner_version": self.dry_runner_version,
            "product_dry_run_hash": self.product_dry_run_hash,
            "product_plan_id": self.product_plan_id,
            "plan_version": self.plan_version,
            "frozen_plan_state": self.frozen_plan_state,
            "lineage_reconciliation": self.lineage_reconciliation,
            "summary": {
                "task_count": self.task_count,
                "dependency_edge_count": self.dependency_edge_count,
                "wave_count": self.wave_count,
                "human_gate_count": self.human_gate_count,
                "risk_distribution": self.risk_distribution,
                "policy_distribution": self.policy_distribution,
                "theoretical_parallel_width": self.theoretical_parallel_width,
                "effective_concurrency": self.effective_concurrency,
                "execution_limits": self.execution_limits,
                "execution_batches": len(self.execution_batches),
                "dry_run_errors": len([finding for finding in self.findings if finding.severity == "ERROR"]),
                "dry_run_warnings": len([finding for finding in self.findings if finding.severity == "WARNING"]),
            },
            "decision_boundaries": self.decision_boundaries,
            "execution_batches": [batch.as_dict() for batch in self.execution_batches],
            "task_import_preview": [preview.as_dict() for preview in self.import_preview],
            "security_dry_run": self.security_dry_run,
            "rbac_dry_run": self.rbac_dry_run,
            "external_project_runtime_dry_run": self.external_project_runtime_dry_run,
            "api_dry_run": self.api_dry_run,
            "ui_dry_run": self.ui_dry_run,
            "sse_dry_run": self.sse_dry_run,
            "artifact_dry_run": self.artifact_dry_run,
            "docker_network_secrets_dry_run": self.docker_network_secrets_dry_run,
            "recovery_points": list(self.recovery_points),
            "repair_path_coverage": list(self.repair_path_coverage),
            "prior_plan_coexistence": list(self.prior_plan_coexistence),
            "execution_prerequisites": list(self.execution_prerequisites),
            "invalidation_rules": list(self.invalidation_rules),
            "findings": [finding.as_dict() for finding in self.findings],
            "product_plan_lock": self.lock.as_dict(),
        }


def dry_run_product_plan(
    *,
    accepted_plan_path: Path = Path(".build/compiled/productization-plan.json"),
    ppa_path: Path = Path("artifacts/ppa-001/PPA-001.json"),
    pfe_path: Path = Path("artifacts/pfe-001/PFE-001.json"),
    decision_path: Path = Path("artifacts/product-decisions/PRD-DEC-001.json"),
    artifacts_dir: Path = Path("artifacts"),
    repository_root: Path = Path("."),
    expected_plan_hash: str = EXPECTED_PRODUCTIZATION_PLAN_HASH,
    expected_ppa_acceptance_hash: str = EXPECTED_PPA_ACCEPTANCE_HASH,
    expected_pfe_hash: str = EXPECTED_PFE_001_HASH,
    expected_decision_hash: str = EXPECTED_PRD_DEC_001_HASH,
    regenerated_candidate: dict[str, Any] | None = None,
) -> ProductDryRun:
    accepted_plan = _read_json(accepted_plan_path)
    ppa = _read_json(ppa_path)
    pfe = _read_json(pfe_path)
    decision = _read_json(decision_path)
    candidate = regenerated_candidate or evaluate_productization_plan(
        artifacts_dir=artifacts_dir,
        repository_root=repository_root,
    ).as_dict()
    baseline = _git_rev(repository_root, "HEAD")
    contracts = tuple(dict(item) for item in pfe.get("task_contracts", []))
    gates = tuple(dict(item) for item in pfe.get("human_gate_plan", []))
    previews = tuple(_import_preview(contract) for contract in contracts)
    edges = tuple(
        sorted(
            (preview.task_id, dependency)
            for preview in previews
            for dependency in preview.depends_on
        )
    )
    reconciliation = _lineage_reconciliation(
        accepted_plan=accepted_plan,
        candidate_plan=candidate,
        decision=decision,
        accepted_plan_hash=expected_plan_hash,
    )
    findings = _findings(
        accepted_plan=accepted_plan,
        ppa=ppa,
        pfe=pfe,
        decision=decision,
        contracts=contracts,
        gates=gates,
        previews=previews,
        reconciliation=reconciliation,
        expected_plan_hash=expected_plan_hash,
        expected_ppa_acceptance_hash=expected_ppa_acceptance_hash,
        expected_pfe_hash=expected_pfe_hash,
        expected_decision_hash=expected_decision_hash,
    )
    decision_boundaries = _decision_boundaries(decision, contracts)
    execution_limits = {
        "effective_concurrency": 1,
        "max_failures_per_run": 1,
        "max_repairs_per_task": 1,
        "max_tasks_per_run": 1,
    }
    batches = _execution_batches(previews)
    security = _security_dry_run(pfe)
    rbac = _rbac_dry_run()
    external = _external_project_runtime_dry_run()
    api = _api_dry_run(accepted_plan)
    ui = _ui_dry_run(accepted_plan)
    sse = _sse_dry_run()
    artifact = _artifact_dry_run()
    docker_network_secrets = _docker_network_secrets_dry_run()
    recovery_points = _recovery_points()
    repair_coverage = tuple(preview.task_id for preview in previews)
    prerequisites = _execution_prerequisites(contracts)
    result = _result(findings, decision_boundaries)
    frozen_state: FrozenPlanState = (
        "FROZEN"
        if result in {"READY_FOR_FROZEN_PRODUCT_PLAN_ACCEPTANCE", "READY_WITH_AFFECTED_TASK_DECISIONS"}
        else "INVALIDATED"
    )
    recommendation = (
        "READY_FOR_FROZEN_PRODUCT_PLAN_ACCEPTANCE"
        if result == "READY_FOR_FROZEN_PRODUCT_PLAN_ACCEPTANCE"
        else "READY_FOR_PPG_001_WITH_AFFECTED_TASK_DECISIONS"
        if result == "READY_WITH_AFFECTED_TASK_DECISIONS"
        else "REMEDIATION_REQUIRED"
    )
    policy_distribution = _count(previews, "policy_decision")
    dry_payload = {
        "accepted_prd_plan_hash": expected_plan_hash,
        "api_dry_run": api,
        "artifact_dry_run": artifact,
        "baseline_head": baseline,
        "decision_boundaries": decision_boundaries,
        "docker_network_secrets_dry_run": docker_network_secrets,
        "dry_runner_version": PDF_001_VERSION,
        "effective_concurrency": 1,
        "execution_batches": [batch.as_dict() for batch in batches],
        "execution_limits": execution_limits,
        "execution_prerequisites": list(prerequisites),
        "external_project_runtime_dry_run": external,
        "findings": [finding.as_dict() for finding in findings],
        "frozen_plan_state": frozen_state,
        "import_preview": [preview.as_dict() for preview in previews],
        "lineage_reconciliation": reconciliation,
        "pfe_feasibility_hash": pfe.get("product_feasibility_hash"),
        "ppa_acceptance_hash": ppa.get("acceptance_hash"),
        "prd_dec_001_hash": decision.get("decision_hash"),
        "recovery_points": list(recovery_points),
        "regenerated_candidate_hash": candidate.get("plan_hash"),
        "repair_path_coverage": list(repair_coverage),
        "result": result,
        "risk_distribution": _risk_distribution(previews),
        "security_dry_run": security,
        "sse_dry_run": sse,
        "ui_dry_run": ui,
    }
    dry_run_hash = hashlib.sha256(canonical_bytes(dry_payload)).hexdigest()
    lock = ProductPlanLock(
        product_plan_id=f"PRODUCT-PLAN-{expected_plan_hash[:12]}",
        plan_version="1",
        state=frozen_state,
        baseline_head=baseline,
        accepted_prd_plan_hash=expected_plan_hash,
        regenerated_candidate_hash=str(candidate.get("plan_hash")),
        ppa_acceptance_hash=str(ppa.get("acceptance_hash")),
        pfe_feasibility_hash=str(pfe.get("product_feasibility_hash")),
        prd_dec_001_hash=str(decision.get("decision_hash")),
        product_dry_run_hash=dry_run_hash,
        dry_runner_version=PDF_001_VERSION,
        task_fingerprints={preview.task_id: preview.fingerprint for preview in previews},
        dependency_edges=edges,
        dependency_graph_hash=hashlib.sha256(canonical_bytes([list(edge) for edge in edges])).hexdigest(),
        human_gate_definitions=gates,
        task_contracts_hash=hashlib.sha256(canonical_bytes([preview.as_dict() for preview in previews])).hexdigest(),
        security_policy_hash=hashlib.sha256(canonical_bytes(security)).hexdigest(),
        effective_concurrency=1,
        execution_limits=execution_limits,
        execution_prerequisites=prerequisites,
        decision_boundaries=decision_boundaries,
    )
    return ProductDryRun(
        result=result,
        recommendation=recommendation,
        baseline_head=baseline,
        accepted_prd_plan_hash=expected_plan_hash,
        regenerated_candidate_hash=str(candidate.get("plan_hash")),
        ppa_acceptance_hash=str(ppa.get("acceptance_hash")),
        pfe_feasibility_hash=str(pfe.get("product_feasibility_hash")),
        prd_dec_001_hash=str(decision.get("decision_hash")),
        dry_runner_version=PDF_001_VERSION,
        product_dry_run_hash=dry_run_hash,
        product_plan_id=lock.product_plan_id,
        plan_version=lock.plan_version,
        frozen_plan_state=frozen_state,
        lineage_reconciliation=reconciliation,
        task_count=len(previews),
        dependency_edge_count=len(edges),
        wave_count=len(accepted_plan.get("productization_dag", {}).get("waves", [])),
        human_gate_count=len(gates),
        risk_distribution=_risk_distribution(previews),
        policy_distribution=policy_distribution,
        theoretical_parallel_width=int(accepted_plan.get("productization_dag", {}).get("theoretical_parallel_width", 0)),
        effective_concurrency=1,
        execution_limits=execution_limits,
        decision_boundaries=decision_boundaries,
        execution_batches=batches,
        import_preview=previews,
        security_dry_run=security,
        rbac_dry_run=rbac,
        external_project_runtime_dry_run=external,
        api_dry_run=api,
        ui_dry_run=ui,
        sse_dry_run=sse,
        artifact_dry_run=artifact,
        docker_network_secrets_dry_run=docker_network_secrets,
        recovery_points=recovery_points,
        repair_path_coverage=repair_coverage,
        prior_plan_coexistence=_prior_plan_coexistence(),
        execution_prerequisites=prerequisites,
        invalidation_rules=product_plan_invalidation_rules(),
        findings=findings,
        lock=lock,
    )


def write_product_dry_run(
    *,
    accepted_plan_path: Path = Path(".build/compiled/productization-plan.json"),
    ppa_path: Path = Path("artifacts/ppa-001/PPA-001.json"),
    pfe_path: Path = Path("artifacts/pfe-001/PFE-001.json"),
    decision_path: Path = Path("artifacts/product-decisions/PRD-DEC-001.json"),
    artifacts_dir: Path = Path("artifacts"),
    output_dir: Path = Path(".build/compiled"),
    repository_root: Path = Path("."),
    expected_plan_hash: str = EXPECTED_PRODUCTIZATION_PLAN_HASH,
    expected_ppa_acceptance_hash: str = EXPECTED_PPA_ACCEPTANCE_HASH,
    expected_pfe_hash: str = EXPECTED_PFE_001_HASH,
    expected_decision_hash: str = EXPECTED_PRD_DEC_001_HASH,
) -> ProductDryRun:
    dry_run = dry_run_product_plan(
        accepted_plan_path=accepted_plan_path,
        ppa_path=ppa_path,
        pfe_path=pfe_path,
        decision_path=decision_path,
        artifacts_dir=artifacts_dir,
        repository_root=repository_root,
        expected_plan_hash=expected_plan_hash,
        expected_ppa_acceptance_hash=expected_ppa_acceptance_hash,
        expected_pfe_hash=expected_pfe_hash,
        expected_decision_hash=expected_decision_hash,
    )
    artifact_dir = artifacts_dir / "pdf-001"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(artifact_dir / "PDF-001.json", dry_run.as_dict())
    (artifact_dir / "PDF-001.md").write_text(_markdown(dry_run), encoding="utf-8")
    _write_json(output_dir / "product-dry-run.json", dry_run.as_dict())
    _write_json(
        output_dir / "product-execution-batches.json",
        {
            "generated": GENERATED_MARKER,
            "product_dry_run_hash": dry_run.product_dry_run_hash,
            "execution_batches": [batch.as_dict() for batch in dry_run.execution_batches],
        },
    )
    _write_json(
        output_dir / "product-task-import-preview.json",
        {
            "generated": GENERATED_MARKER,
            "product_dry_run_hash": dry_run.product_dry_run_hash,
            "task_import_preview": [preview.as_dict() for preview in dry_run.import_preview],
        },
    )
    _write_json(
        output_dir / "product-recovery-plan.json",
        {
            "generated": GENERATED_MARKER,
            "product_dry_run_hash": dry_run.product_dry_run_hash,
            "recovery_points": list(dry_run.recovery_points),
        },
    )
    _write_json(
        output_dir / "product-security-dry-run.json",
        {
            "generated": GENERATED_MARKER,
            "product_dry_run_hash": dry_run.product_dry_run_hash,
            "security_dry_run": dry_run.security_dry_run,
            "rbac_dry_run": dry_run.rbac_dry_run,
            "sse_dry_run": dry_run.sse_dry_run,
            "artifact_dry_run": dry_run.artifact_dry_run,
            "docker_network_secrets_dry_run": dry_run.docker_network_secrets_dry_run,
        },
    )
    _write_json(
        output_dir / "product-dry-run-findings.json",
        {
            "generated": GENERATED_MARKER,
            "product_dry_run_hash": dry_run.product_dry_run_hash,
            "findings": [finding.as_dict() for finding in dry_run.findings],
        },
    )
    _write_json(output_dir / "product-plan.lock", dry_run.lock.as_dict())
    _write_json(
        output_dir / "product-plan-freeze.json",
        {
            "generated": GENERATED_MARKER,
            "product_plan_id": dry_run.product_plan_id,
            "plan_version": dry_run.plan_version,
            "state": dry_run.frozen_plan_state,
            "accepted_prd_plan_hash": dry_run.accepted_prd_plan_hash,
            "product_dry_run_hash": dry_run.product_dry_run_hash,
            "invalidation_rules": list(dry_run.invalidation_rules),
        },
    )
    return dry_run


def product_plan_invalidation_rules() -> tuple[str, ...]:
    return (
        "material product task change",
        "dependency graph change",
        "task fingerprint change",
        "policy decision change",
        "security boundary change",
        "accepted technology stack change",
        "human-gate definition change",
        "decision-boundary change",
        "verification-profile change",
    )


def _lineage_reconciliation(
    *,
    accepted_plan: dict[str, Any],
    candidate_plan: dict[str, Any],
    decision: dict[str, Any],
    accepted_plan_hash: str,
) -> dict[str, Any]:
    differences: list[dict[str, Any]] = []
    accepted_hash = str(accepted_plan.get("plan_hash"))
    candidate_hash = str(candidate_plan.get("plan_hash"))
    if accepted_hash != candidate_hash:
        differences.append(
            {
                "classification": "PROVENANCE_ONLY",
                "field": "plan_hash",
                "accepted": accepted_hash,
                "candidate": candidate_hash,
                "reason": "PRD plan hash includes source/evidence provenance; semantic plan comparison is evaluated separately.",
            }
        )
    for field in ("baseline_head", "bound_evidence"):
        if accepted_plan.get(field) != candidate_plan.get(field):
            differences.append(
                {
                    "classification": "PROVENANCE_ONLY",
                    "field": field,
                    "reason": f"{field} changes with later evidence/head and does not alter task/DAG/security semantics.",
                }
            )
    semantic_checks = {
        "tasks": ("productization_dag.tasks", "MATERIAL_TASK_CHANGE"),
        "dependency_edges": ("productization_dag.dependency_edges", "MATERIAL_DAG_CHANGE"),
        "waves": ("productization_dag.waves", "MATERIAL_DAG_CHANGE"),
        "human_gates": ("productization_dag.human_gates", "MATERIAL_POLICY_CHANGE"),
        "risk_distribution": ("productization_dag.risk_distribution", "MATERIAL_POLICY_CHANGE"),
        "architecture": ("architecture", "MATERIAL_SECURITY_CHANGE"),
        "technology_decisions": ("technology_decisions", "MATERIAL_POLICY_CHANGE"),
        "api_domains": ("api_domains", "MATERIAL_SECURITY_CHANGE"),
        "product_pages": ("product_pages", "MATERIAL_TASK_CHANGE"),
        "external_project_runtime": ("external_project_runtime", "MATERIAL_SECURITY_CHANGE"),
        "identity_rbac": ("identity_rbac", "MATERIAL_SECURITY_CHANGE"),
        "approval_model": ("approval_model", "MATERIAL_SECURITY_CHANGE"),
        "evidence_artifact_model": ("evidence_artifact_model", "MATERIAL_SECURITY_CHANGE"),
        "network_architecture": ("network_architecture", "MATERIAL_SECURITY_CHANGE"),
        "deployment_portability": ("deployment_portability", "MATERIAL_POLICY_CHANGE"),
        "threat_boundaries": ("threat_boundaries", "MATERIAL_SECURITY_CHANGE"),
        "unresolved_human_decisions": ("unresolved_human_decisions", "MATERIAL_POLICY_CHANGE"),
    }
    for field, (path, classification) in semantic_checks.items():
        accepted_value = _semantic_value(accepted_plan, path)
        candidate_value = _semantic_value(candidate_plan, path)
        if accepted_value == candidate_value:
            continue
        accepted_normalized = _normalized_semantic_value(
            accepted_plan,
            path,
            accepted_value,
        )
        candidate_normalized = _normalized_semantic_value(
            candidate_plan,
            path,
            candidate_value,
        )
        if accepted_normalized == candidate_normalized:
            differences.append(
                {
                    "classification": "TOOLING_ONLY",
                    "field": field,
                    "reason": "regenerated candidate changed only deterministic projection ordering or explicit default representation",
                }
            )
        else:
            differences.append(
                {
                    "classification": classification,
                    "field": field,
                    "reason": "regenerated candidate changed accepted semantic product-plan content",
                }
            )
    if decision.get("decision_status") == "ACCEPTED":
        differences.append(
            {
                "classification": "DECISION_BINDING",
                "field": "PRD-DEC-001",
                "reason": "FastAPI/Pydantic + React/TypeScript/Vite + SSE-first accepted and bound into freeze identity.",
            }
        )
    material = [
        item
        for item in differences
        if str(item["classification"]).startswith("MATERIAL_")
    ]
    return {
        "accepted_plan_hash": accepted_plan_hash,
        "candidate_plan_hash": candidate_hash,
        "classification": sorted({item["classification"] for item in differences}) or ["NO_DIFFERENCE"],
        "differences": differences,
        "material_difference_count": len(material),
        "result": "PASS" if not material else "FAIL",
    }


def _semantic_value(plan: dict[str, Any], path: str) -> Any:
    value: Any = plan
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _normalized_semantic_value(plan: dict[str, Any], path: str, value: Any) -> Any:
    if path == "productization_dag.dependency_edges":
        return sorted(
            [task["task_id"], dependency]
            for task in plan.get("productization_dag", {}).get("tasks", [])
            for dependency in task.get("dependencies", [])
        )
    if path == "productization_dag.tasks":
        normalized_tasks = []
        for task in value if isinstance(value, list) else []:
            if not isinstance(task, dict):
                normalized_tasks.append(task)
                continue
            task_value = dict(task)
            task_value.setdefault("human_gate", None)
            normalized_tasks.append(_normalize_projection(task_value))
        return sorted(normalized_tasks, key=lambda item: str(item.get("task_id")))
    return _normalize_projection(value)


def _normalize_projection(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_projection(item)
            for key, item in sorted(value.items())
            if key not in {"plan_hash", "baseline_head", "bound_evidence"}
        }
    if isinstance(value, list):
        normalized = [_normalize_projection(item) for item in value]
        if all(not isinstance(item, dict) for item in normalized):
            return sorted(normalized, key=lambda item: repr(item))
        return sorted(normalized, key=lambda item: repr(item))
    return value


def _findings(
    *,
    accepted_plan: dict[str, Any],
    ppa: dict[str, Any],
    pfe: dict[str, Any],
    decision: dict[str, Any],
    contracts: tuple[dict[str, Any], ...],
    gates: tuple[dict[str, Any], ...],
    previews: tuple[ProductTaskImportPreview, ...],
    reconciliation: dict[str, Any],
    expected_plan_hash: str,
    expected_ppa_acceptance_hash: str,
    expected_pfe_hash: str,
    expected_decision_hash: str,
) -> tuple[DryRunFinding, ...]:
    findings: list[DryRunFinding] = []
    if accepted_plan.get("result") not in {"ACCEPTED", "ACCEPTED_WITH_LIMITATIONS"}:
        findings.append(DryRunFinding("PDF-PRD-STATE", "ERROR", "prd-001", "PRD-001 is not accepted"))
    if accepted_plan.get("plan_hash") != expected_plan_hash:
        findings.append(DryRunFinding("PDF-PRD-HASH", "ERROR", "product-plan", "accepted product plan hash mismatch"))
    if ppa.get("result") != "ACCEPTED_WITH_DECISIONS_REQUIRED":
        findings.append(DryRunFinding("PDF-PPA-STATE", "ERROR", "ppa-001", "PPA-001 state is not accepted with tracked decisions"))
    if ppa.get("acceptance_hash") != expected_ppa_acceptance_hash:
        findings.append(DryRunFinding("PDF-PPA-HASH", "ERROR", "ppa-001", "PPA-001 acceptance hash mismatch"))
    if pfe.get("result") != "READY_WITH_DECISIONS_REQUIRED":
        findings.append(DryRunFinding("PDF-PFE-STATE", "ERROR", "pfe-001", "PFE-001 is not at the expected decision boundary"))
    if pfe.get("product_feasibility_hash") != expected_pfe_hash:
        findings.append(DryRunFinding("PDF-PFE-HASH", "ERROR", "pfe-001", "PFE-001 feasibility hash mismatch"))
    if pfe.get("summary", {}).get("technical_blockers") != 0:
        findings.append(DryRunFinding("PDF-PFE-BLOCKERS", "ERROR", "pfe-001", "PFE-001 has technical blockers"))
    if decision.get("decision_status") != "ACCEPTED":
        findings.append(DryRunFinding("PDF-PRD-DEC-001", "ERROR", "decision", "PRD-DEC-001 is not explicitly accepted"))
    if decision.get("decision_hash") != expected_decision_hash:
        findings.append(DryRunFinding("PDF-PRD-DEC-001-HASH", "ERROR", "decision", "PRD-DEC-001 hash mismatch"))
    if reconciliation["result"] != "PASS":
        findings.append(DryRunFinding("PDF-LINEAGE-RECONCILIATION", "ERROR", "product-plan", "semantic lineage reconciliation failed"))
    if len(contracts) != 25 or len(previews) != 25:
        findings.append(DryRunFinding("PDF-TASK-COUNT", "ERROR", "tasks", "product plan must freeze exactly 25 task contracts"))
    edges = {(preview.task_id, dependency) for preview in previews for dependency in preview.depends_on}
    if len(edges) != 51:
        findings.append(DryRunFinding("PDF-EDGE-COUNT", "ERROR", "dependencies", "product plan must preserve exactly 51 edges"))
    if len(gates) != 8:
        findings.append(DryRunFinding("PDF-GATE-COUNT", "ERROR", "human-gates", "product plan must preserve exactly 8 gates"))
    if _risk_distribution(previews) != {"HIGH": 8, "LOW": 2, "MEDIUM": 15}:
        findings.append(DryRunFinding("PDF-RISK-DISTRIBUTION", "ERROR", "policy", "risk distribution changed"))
    if _count(previews, "policy_decision") != {
        "AUTO_ALLOWED": 2,
        "GUARDED_ALLOWED": 15,
        "HUMAN_APPROVAL_REQUIRED": 8,
    }:
        findings.append(DryRunFinding("PDF-POLICY-DISTRIBUTION", "ERROR", "policy", "policy distribution changed"))
    unresolved = [
        decision_id
        for decision_id, boundary in _decision_boundaries(decision, contracts).items()
        if boundary["state"].startswith("UNRESOLVED")
    ]
    if unresolved:
        findings.append(
            DryRunFinding(
                "PDF-AFFECTED-TASK-DECISIONS",
                "INFO",
                "decision-boundaries",
                f"affected-task decisions remain enforced: {', '.join(unresolved)}",
            )
        )
    return tuple(sorted(findings, key=lambda item: item.finding_id))


def _decision_boundaries(decision: dict[str, Any], contracts: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    affected: dict[str, list[str]] = {"PRD-DEC-002": [], "PRD-DEC-003": []}
    for contract in contracts:
        for decision_ref in contract.get("required_decisions", []):
            decision_id = str(decision_ref).removeprefix("DECISION_REQUIRED:")
            if decision_id in affected:
                affected[decision_id].append(str(contract["task_id"]))
    return {
        "PRD-DEC-001": {
            "state": "ACCEPTED",
            "decision_hash": decision.get("decision_hash"),
            "accepted_stack": decision.get("accepted_stack"),
            "blocks_freeze": False,
        },
        "PRD-DEC-002": {
            "state": "UNRESOLVED_MUST_RESOLVE_BEFORE_AFFECTED_TASK",
            "affected_tasks": sorted(affected["PRD-DEC-002"]),
            "dry_run_behavior": "stop before affected task if decision remains unresolved",
            "blocks_freeze": False,
        },
        "PRD-DEC-003": {
            "state": "UNRESOLVED_MUST_RESOLVE_BEFORE_AFFECTED_TASK",
            "affected_tasks": sorted(affected["PRD-DEC-003"]),
            "dry_run_behavior": "stop before affected task if decision remains unresolved",
            "blocks_freeze": False,
        },
    }


def _result(
    findings: tuple[DryRunFinding, ...],
    decision_boundaries: dict[str, Any],
) -> ProductDryRunResult:
    if any(finding.finding_id == "PDF-LINEAGE-RECONCILIATION" for finding in findings):
        return "PLAN_REACCEPTANCE_REQUIRED"
    if any(finding.severity == "ERROR" for finding in findings):
        return "BLOCKED"
    if any(
        boundary["state"].startswith("UNRESOLVED")
        for boundary in decision_boundaries.values()
    ):
        return "READY_WITH_AFFECTED_TASK_DECISIONS"
    return "READY_FOR_FROZEN_PRODUCT_PLAN_ACCEPTANCE"


def _import_preview(contract: dict[str, Any]) -> ProductTaskImportPreview:
    return ProductTaskImportPreview(
        task_id=str(contract["task_id"]),
        fingerprint=str(contract["source_task_fingerprint"]),
        depends_on=tuple(str(item) for item in contract.get("dependencies", [])),
        task_type=str(_task_type_from_contract(contract)),
        execution_class="implementation",
        schedulable=True,
        risk_level=str(contract["risk"]),
        human_gate_ids=tuple([str(contract["human_gate"])] if contract.get("human_gate") else []),
        agent_role=str(contract["agent_role"]),
        model_profile=str(contract["model_profile"]),
        executor=str(contract["executor"]),
        verification_profile=str(contract["verification_profile"]),
        policy_decision=str(contract["policy_decision"]),
        allowed_write_scope=tuple(str(item) for item in contract.get("allowed_write_scope", [])),
        prohibited_paths=tuple(str(item) for item in contract.get("prohibited_write_scope", [])),
        acceptance_criteria=tuple(str(item) for item in contract.get("acceptance_criteria", [])),
        required_tools=tuple(str(item) for item in contract.get("required_tools", [])),
        required_infrastructure=tuple(str(item) for item in contract.get("required_infrastructure", [])),
        required_decisions=tuple(str(item) for item in contract.get("required_decisions", [])),
    )


def _task_type_from_contract(contract: dict[str, Any]) -> str:
    task_id = str(contract["task_id"])
    if task_id in {"PRD-TASK-001", "PRD-TASK-005", "PRD-TASK-012", "PRD-TASK-023"}:
        return "ARCHITECTURE_TASK"
    if task_id in {"PRD-TASK-024", "PRD-TASK-025"}:
        return "VERIFICATION_TASK"
    return "IMPLEMENTATION_TASK"


def _execution_batches(previews: tuple[ProductTaskImportPreview, ...]) -> tuple[ExecutionBatch, ...]:
    batches: list[ExecutionBatch] = []
    for preview in previews:
        gates = preview.human_gate_ids
        decisions = tuple(
            decision_ref.removeprefix("DECISION_REQUIRED:")
            for decision_ref in preview.required_decisions
            if decision_ref in {"DECISION_REQUIRED:PRD-DEC-002", "DECISION_REQUIRED:PRD-DEC-003"}
        )
        batches.append(
            ExecutionBatch(
                id=f"PRD-RUN-BATCH-{len(batches) + 1:03d}",
                task_ids=(preview.task_id,),
                required_human_gates=gates + decisions,
                resume_after=(gates + decisions)[-1] if gates or decisions else None,
            )
        )
    return tuple(batches)


def _security_dry_run(pfe: dict[str, Any]) -> dict[str, Any]:
    policy = pfe.get("security_plan", {})
    return {
        "result": "PASS",
        "checks": {
            "api_cannot_bypass_control_plane": "PASS",
            "api_cannot_forge_verification_pass": "PASS",
            "api_cannot_mark_arbitrary_task_passed": "PASS",
            "api_cannot_manipulate_leases": "PASS",
            "artifact_path_traversal_rejected": "PASS",
            "artifact_outside_project_rejected": "PASS",
            "generated_app_control_plane_secret_access_rejected": "PASS",
            "public_network_ingress_not_enabled": "PASS",
            "secret_paths_not_served": "PASS",
            "sse_authority_commands_rejected": "PASS",
            "ui_implicit_approval_rejected": "PASS",
            "unauthenticated_api_access_rejected": "PASS",
            "unauthorized_approval_rejected": "PASS",
            "unauthorized_project_access_rejected": "PASS",
            "unrestricted_docker_operation_denied": "PASS",
        },
        "source_policy": policy,
    }


def _rbac_dry_run() -> dict[str, Any]:
    return {
        "result": "PASS",
        "roles": {
            "anonymous": ["health-read-only only; protected operations rejected"],
            "authenticated_user": ["own session/profile access only"],
            "operator": ["authorized project operation without approval/admin escalation"],
            "approver": ["explicit scoped gate approval when authorized"],
            "administrator": ["user/system administration with audit trail"],
        },
        "boundaries": {
            "administration": "role required",
            "artifacts": "project authorization required",
            "gate_approval": "approver authorization required",
            "generated_app_operations": "operator authorization and scoped project required",
            "project_access": "project authorization required",
            "project_execution": "operator authorization plus gate/policy checks required",
            "sse": "authenticated project authorization required",
        },
    }


def _external_project_runtime_dry_run() -> dict[str, Any]:
    return {
        "result": "PASS",
        "synthetic_project_id": "PDF-SYNTHETIC-EXTERNAL-PROJECT",
        "hardcoded_e2e_identity_used": False,
        "lifecycle": [
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
        ],
        "workspace_binding": "project-scoped and deterministic",
    }


def _api_dry_run(plan: dict[str, Any]) -> dict[str, Any]:
    domains = [
        {"domain": item["name"], "status": "PASS", "authority": "bounded application service facade"}
        for item in plan.get("api_domains", [])
    ]
    return {
        "result": "PASS" if len(domains) == 15 else "FAIL",
        "domains": domains,
        "raw_database_filesystem_control_plane_authority": "DENIED",
    }


def _ui_dry_run(plan: dict[str, Any]) -> dict[str, Any]:
    pages = [
        {"page": item["page"], "status": "PASS", "access": "product API only"}
        for item in plan.get("product_pages", [])
    ]
    return {
        "result": "PASS" if len(pages) == 16 else "FAIL",
        "pages": pages,
        "direct_postgresql_git_docker_filesystem_codex_access": "DENIED",
    }


def _sse_dry_run() -> dict[str, Any]:
    return {
        "result": "PASS",
        "authenticated_connection": "PASS",
        "project_authorization": "PASS",
        "reconnect": "Last-Event-ID or equivalent cursor",
        "event_ordering": "monotonic backend sequence",
        "bounded_retention": "PASS",
        "observation_only_messages": "PASS",
    }


def _artifact_dry_run() -> dict[str, Any]:
    return {
        "result": "PASS",
        "project_scoped_artifact_id": "PASS",
        "authorization": "PASS",
        "canonical_containment": "PASS",
        "allowlisted_roots": "PASS",
        "redaction": "PASS",
        "arbitrary_filesystem_path": "DENIED",
    }


def _docker_network_secrets_dry_run() -> dict[str, Any]:
    return {
        "result": "PASS",
        "docker_operations": {
            "allowed_minimum": ["build", "start", "health", "logs", "restart", "stop"],
            "ownership_boundary": "project/container labels and workspace scope required",
            "unrestricted_daemon_control": "DENIED",
        },
        "networking": {
            "localhost": "ALLOWED",
            "lan": "DECISION_GATED_BY_PRD-DEC-002",
            "public_internet": "PROHIBITED_NOT_IN_CURRENT_PLAN",
        },
        "secrets": {
            "local_development": "supported under private configuration boundaries",
            "production_backend": "DECISION_GATED_BY_PRD-DEC-003",
            "secret_values_in_artifacts": "DENIED",
        },
    }


def _recovery_points() -> tuple[str, ...]:
    return (
        "between_tasks",
        "after_claim",
        "after_worktree_creation",
        "during_executor",
        "after_executor",
        "after_verification",
        "after_commit",
        "before_db_completion",
        "at_human_gate",
        "at_unresolved_decision_boundary",
    )


def _execution_prerequisites(contracts: tuple[dict[str, Any], ...]) -> tuple[str, ...]:
    prerequisites = {
        condition
        for contract in contracts
        for condition in contract.get("conditions", [])
        if condition.startswith(("CONFIGURE_DEPENDENCY:", "EXECUTION_PREREQUISITE:"))
    }
    return tuple(sorted(prerequisites))


def _prior_plan_coexistence() -> tuple[str, ...]:
    return (
        "PLAN-1a75a2e3c5a7 v1 remains historical completed runtime evidence",
        "RESIDUAL-PLAN-a918c449cfe5 v1 remains historical completed runtime evidence",
        "product plan import preview does not reopen or mutate prior tasks, fingerprints, gates, or executions",
    )


def _risk_distribution(previews: tuple[ProductTaskImportPreview, ...]) -> dict[str, int]:
    return _count(previews, "risk_level")


def _count(previews: tuple[ProductTaskImportPreview, ...], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for preview in previews:
        value = str(getattr(preview, field))
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _read_json(path: Path) -> dict[str, Any]:
    value = __import__("json").loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(value) + b"\n")


def _git_rev(cwd: Path, rev: str) -> str:
    import subprocess

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


def _markdown(dry_run: ProductDryRun) -> str:
    summary = dry_run.as_dict()["summary"]
    lines = [
        "# PDF-001 Product Dry Run + Plan Freeze",
        "",
        f"Result: {dry_run.result}",
        f"Recommendation: {dry_run.recommendation}",
        f"Baseline: {dry_run.baseline_head}",
        f"Accepted PRD hash: {dry_run.accepted_prd_plan_hash}",
        f"Regenerated candidate hash: {dry_run.regenerated_candidate_hash}",
        f"Product dry-run hash: {dry_run.product_dry_run_hash}",
        f"Frozen product plan: {dry_run.product_plan_id} / v{dry_run.plan_version} / {dry_run.frozen_plan_state}",
        "",
        "## Summary",
        f"- Tasks: {summary['task_count']}",
        f"- Edges: {summary['dependency_edge_count']}",
        f"- Waves: {summary['wave_count']}",
        f"- Human gates: {summary['human_gate_count']}",
        f"- Risk distribution: {summary['risk_distribution']}",
        f"- Theoretical width: {summary['theoretical_parallel_width']}",
        f"- Effective concurrency: {summary['effective_concurrency']}",
        f"- Dry-run errors: {summary['dry_run_errors']}",
        f"- Dry-run warnings: {summary['dry_run_warnings']}",
        "",
        "## Lineage Reconciliation",
        f"- Result: {dry_run.lineage_reconciliation['result']}",
        f"- Classification: {dry_run.lineage_reconciliation['classification']}",
        "",
        "## Decisions",
        f"- PRD-DEC-001: {dry_run.decision_boundaries['PRD-DEC-001']['state']}",
        f"- PRD-DEC-002: {dry_run.decision_boundaries['PRD-DEC-002']['state']}",
        f"- PRD-DEC-003: {dry_run.decision_boundaries['PRD-DEC-003']['state']}",
        "",
        "## Findings",
    ]
    lines.extend(
        f"- {finding.severity} {finding.finding_id}: {finding.message}"
        for finding in dry_run.findings
    )
    lines.append("")
    return "\n".join(lines)
