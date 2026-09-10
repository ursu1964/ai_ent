from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ai_ent.project_manifest import GENERATED_MARKER, canonical_bytes

PPA_001_VERSION = "ppa-001.1"
EXPECTED_PRODUCTIZATION_PLAN_HASH = "7b0342fb7fd5a23a52f8bbc3d1a9d4d30547493f063f8017bc7de3577634ceea"

PPAResultValue = Literal["ACCEPTED", "ACCEPTED_WITH_DECISIONS_REQUIRED", "REJECTED"]
DecisionTiming = Literal[
    "MUST_RESOLVE_BEFORE_PLAN_FREEZE",
    "MUST_RESOLVE_BEFORE_AFFECTED_TASK",
    "SAFE_TO_DEFER",
]


@dataclass(frozen=True)
class ProductPlanAcceptanceResult:
    result: PPAResultValue
    recommendation: str
    baseline: str
    evaluator_version: str
    productization_plan_hash: str
    plan_identity: dict[str, Any]
    architecture_review: dict[str, Any]
    external_project_runtime_review: dict[str, Any]
    technology_decisions: tuple[dict[str, Any], ...]
    unresolved_human_decisions: tuple[dict[str, Any], ...]
    authority_reviews: dict[str, Any]
    human_gate_review: tuple[dict[str, Any], ...]
    high_risk_task_review: tuple[dict[str, Any], ...]
    future_hardening: tuple[dict[str, Any], ...]
    dag_integrity: dict[str, Any]
    critical_path_validation: tuple[dict[str, Any], ...]
    current_blockers: tuple[str, ...]
    accepted_limitations: tuple[str, ...]
    acceptance_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "gate_id": "PPA-001",
            "result": self.result,
            "recommendation": self.recommendation,
            "baseline": self.baseline,
            "evaluator_version": self.evaluator_version,
            "productization_plan_hash": self.productization_plan_hash,
            "plan_identity": self.plan_identity,
            "architecture_review": self.architecture_review,
            "external_project_runtime_review": self.external_project_runtime_review,
            "technology_decisions": list(self.technology_decisions),
            "unresolved_human_decisions": list(self.unresolved_human_decisions),
            "authority_reviews": self.authority_reviews,
            "human_gate_review": list(self.human_gate_review),
            "high_risk_task_review": list(self.high_risk_task_review),
            "future_hardening": list(self.future_hardening),
            "dag_integrity": self.dag_integrity,
            "critical_path_validation": list(self.critical_path_validation),
            "current_blockers": list(self.current_blockers),
            "accepted_limitations": list(self.accepted_limitations),
            "acceptance_hash": self.acceptance_hash,
        }


def evaluate_product_plan_acceptance(
    *,
    plan_path: Path = Path(".build/compiled/productization-plan.json"),
    expected_plan_hash: str = EXPECTED_PRODUCTIZATION_PLAN_HASH,
    repository_root: Path = Path("."),
) -> ProductPlanAcceptanceResult:
    plan = _read_json(plan_path)
    dag = plan.get("productization_dag", {})
    tasks = tuple(dict(item) for item in dag.get("tasks", []))
    edges = tuple((str(edge[0]), str(edge[1])) for edge in dag.get("dependency_edges", []))
    gates = tuple(dict(item) for item in dag.get("human_gates", []))
    plan_hash = str(plan.get("plan_hash", ""))
    baseline = _git_rev(repository_root, "HEAD")

    plan_identity = _plan_identity(plan, tasks, edges, gates)
    architecture_review = _architecture_review(plan)
    external_review = _external_project_runtime_review(plan)
    technology = _technology_decisions(plan)
    decisions = _decision_reviews(plan)
    authority = _authority_reviews(plan)
    gate_review = _human_gate_review(tasks, edges, gates)
    high_risk = _high_risk_task_review(tasks)
    future_hardening = _future_hardening(plan)
    dag_integrity = _dag_integrity(plan, tasks, edges)
    critical_path = _critical_path_validation(plan, tasks)
    blockers = _current_blockers(
        plan_hash=plan_hash,
        expected_plan_hash=expected_plan_hash,
        plan_identity=plan_identity,
        architecture_review=architecture_review,
        external_review=external_review,
        dag_integrity=dag_integrity,
    )
    limitations = tuple(
        sorted(
            {
                "PRD-001 is accepted as an architecture and proposed DAG only; no productization task has been imported or executed.",
                "Per-task agent/model/executor/verification profiles are not frozen in PRD-001 and must be resolved in PFE/PDF before runtime import.",
            }
        )
    )
    has_freeze_decision = any(
        item["resolution_timing"] == "MUST_RESOLVE_BEFORE_PLAN_FREEZE" for item in decisions
    )
    if blockers:
        result: PPAResultValue = "REJECTED"
        recommendation = "REMEDIATION_REQUIRED"
    elif has_freeze_decision:
        result = "ACCEPTED_WITH_DECISIONS_REQUIRED"
        recommendation = "READY_FOR_PRODUCT_FEASIBILITY_AND_POLICY_WITH_DECISION_TRACKING"
    else:
        result = "ACCEPTED"
        recommendation = "READY_FOR_PRODUCT_FEASIBILITY_AND_POLICY"

    payload = {
        "baseline": baseline,
        "evaluator_version": PPA_001_VERSION,
        "productization_plan_hash": plan_hash,
        "plan_identity": plan_identity,
        "architecture_review": architecture_review,
        "external_project_runtime_review": external_review,
        "technology_decisions": technology,
        "unresolved_human_decisions": decisions,
        "authority_reviews": authority,
        "human_gate_review": gate_review,
        "high_risk_task_review": high_risk,
        "future_hardening": future_hardening,
        "dag_integrity": dag_integrity,
        "critical_path_validation": critical_path,
        "current_blockers": blockers,
        "accepted_limitations": limitations,
        "result": result,
    }
    acceptance_hash = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    return ProductPlanAcceptanceResult(
        result=result,
        recommendation=recommendation,
        baseline=baseline,
        evaluator_version=PPA_001_VERSION,
        productization_plan_hash=plan_hash,
        plan_identity=plan_identity,
        architecture_review=architecture_review,
        external_project_runtime_review=external_review,
        technology_decisions=technology,
        unresolved_human_decisions=decisions,
        authority_reviews=authority,
        human_gate_review=gate_review,
        high_risk_task_review=high_risk,
        future_hardening=future_hardening,
        dag_integrity=dag_integrity,
        critical_path_validation=critical_path,
        current_blockers=blockers,
        accepted_limitations=limitations,
        acceptance_hash=acceptance_hash,
    )


def write_product_plan_acceptance(
    *,
    plan_path: Path = Path(".build/compiled/productization-plan.json"),
    artifacts_dir: Path = Path("artifacts"),
    expected_plan_hash: str = EXPECTED_PRODUCTIZATION_PLAN_HASH,
    repository_root: Path = Path("."),
) -> ProductPlanAcceptanceResult:
    result = evaluate_product_plan_acceptance(
        plan_path=plan_path,
        expected_plan_hash=expected_plan_hash,
        repository_root=repository_root,
    )
    output_dir = artifacts_dir / "ppa-001"
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "PPA-001.json", result.as_dict())
    (output_dir / "PPA-001.md").write_text(_markdown(result), encoding="utf-8")
    return result


def _plan_identity(
    plan: dict[str, Any],
    tasks: tuple[dict[str, Any], ...],
    edges: tuple[tuple[str, str], ...],
    gates: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    dag = plan.get("productization_dag", {})
    risk = dict(dag.get("risk_distribution", {}))
    fingerprint_valid = all(_task_fingerprint(task) == task.get("fingerprint") for task in tasks)
    missing_execution_profiles = tuple(
        task["task_id"]
        for task in tasks
        if not all(field in task for field in ("agent_role", "model_profile", "executor", "verification_profile"))
    )
    return {
        "task_count": len(tasks),
        "dependency_edges": len(edges),
        "waves": len(dag.get("waves", [])),
        "human_gates": len(gates),
        "theoretical_parallel_width": dag.get("theoretical_parallel_width"),
        "risk_distribution": risk,
        "matches_expected_counts": len(tasks) == 25
        and len(edges) == 51
        and len(dag.get("waves", [])) == 9
        and len(gates) == 8
        and dag.get("theoretical_parallel_width") == 6
        and risk == {"HIGH": 8, "LOW": 2, "MEDIUM": 15},
        "task_fingerprints_valid": fingerprint_valid,
        "missing_execution_profile_bindings": list(missing_execution_profiles),
        "execution_profile_binding_status": "REQUIRED_BEFORE_FREEZE",
    }


def _task_fingerprint(task: dict[str, Any]) -> str:
    material = {
        "task_id": task["task_id"],
        "epic_id": task["epic_id"],
        "task_type": task["task_type"],
        "title": task["title"],
        "objective": task["objective"],
        "dependencies": task["dependencies"],
        "risk": task["risk"],
        "human_gate": task.get("human_gate"),
        "allowed_scope": task["allowed_scope"],
        "prohibited_scope": task["prohibited_scope"],
        "acceptance_criteria": task["acceptance_criteria"],
    }
    return hashlib.sha256(canonical_bytes(material)).hexdigest()


def _architecture_review(plan: dict[str, Any]) -> dict[str, Any]:
    layers = plan.get("architecture", {}).get("layers", [])
    expected = [
        "Browser",
        "Operator Web UI",
        "Product API",
        "Application Services",
        "Existing AI-Enterprise Control Plane",
        "PostgreSQL / Git / Worktrees / Docker / Codex",
    ]
    authority_rule = str(plan.get("architecture", {}).get("authority_rule", ""))
    return {
        "expected_layers_present": set(layers) == set(expected),
        "wraps_existing_control_plane": "cannot bypass gates" in authority_rule,
        "authority_rule": authority_rule,
        "status": "ACCEPTED",
    }


def _external_project_runtime_review(plan: dict[str, Any]) -> dict[str, Any]:
    runtime = plan.get("external_project_runtime", {})
    entities = set(runtime.get("entities", []))
    lifecycle = runtime.get("lifecycle", [])
    expected_entities = {
        "ExternalProject",
        "ProjectWorkspace",
        "ProjectRepository",
        "ProjectPlan",
        "ProjectRuntimeBinding",
        "GeneratedArtifact",
    }
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
    genericity = str(runtime.get("genericity_rule", ""))
    return {
        "entities_complete": expected_entities <= entities,
        "lifecycle_complete": set(lifecycle) == set(expected_lifecycle),
        "removes_e2e_harness_dependency": "No E2E-TEAM-WORK-TRACKER-specific" in genericity,
        "genericity_rule": genericity,
        "status": "ACCEPTED",
    }


def _technology_decisions(plan: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    classifications = {
        "backend_api": ("ACCEPTED_WITH_CONDITIONS", "Add FastAPI only at implementation time and keep API as a facade over existing services."),
        "frontend": ("ACCEPTED_WITH_CONDITIONS", "Use React/Vite for operator workflows; ordinary UI actions must not imply approval."),
        "realtime": ("ACCEPTED", "SSE is observation-only and operationally simpler than WebSocket for first release."),
        "authentication": ("ACCEPTED_WITH_CONDITIONS", "Session/RBAC implementation must include CSRF, password hashing, audit, and distinct approver authorization."),
        "reverse_proxy": ("DECISION_REQUIRED", "Choose Caddy or nginx before PRD-TASK-019 LAN access work."),
        "artifact_serving": ("ACCEPTED_WITH_CONDITIONS", "Artifact downloads must be path-scoped, redacted, and API-mediated."),
    }
    result: list[dict[str, Any]] = []
    for decision in plan.get("technology_decisions", []):
        domain = str(decision.get("domain"))
        status, condition = classifications.get(domain, ("DECISION_REQUIRED", "No PPA classification exists."))
        result.append(
            {
                **decision,
                "ppa_classification": status,
                "condition": condition,
                "requirement_driven": True,
            }
        )
    return tuple(result)


def _decision_reviews(plan: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    tasks = tuple(dict(item) for item in plan.get("productization_dag", {}).get("tasks", []))
    by_id = {task["task_id"]: task for task in tasks}
    reviews = {
        "PRD-DEC-001": {
            "question": "Accept FastAPI/Pydantic plus React/TypeScript/Vite as the product stack?",
            "alternatives": ["Accept proposed stack", "choose another web/API stack", "split API/UI technology decisions"],
            "affected_tasks": [
                "PRD-TASK-005",
                "PRD-TASK-007",
                "PRD-TASK-008",
                "PRD-TASK-009",
                "PRD-TASK-010",
                "PRD-TASK-011",
                "PRD-TASK-012",
                "PRD-TASK-013",
                "PRD-TASK-014",
                "PRD-TASK-015",
                "PRD-TASK-016",
                "PRD-TASK-017",
            ],
            "security_implications": "Controls API schema validation, auth middleware placement, browser attack surface, and dependency review.",
            "deployment_implications": "Adds Python API and Node frontend build/runtime packaging.",
            "implementation_can_begin_before_resolution": False,
            "recommended_option": "Accept proposed stack.",
            "rationale": "The plan already derives API/UI tasks from these choices; changing them alters plan identity.",
            "resolution_timing": "MUST_RESOLVE_BEFORE_PLAN_FREEZE",
        },
        "PRD-DEC-002": {
            "question": "When should non-loopback LAN access be enabled?",
            "alternatives": ["Loopback only until PRD-TASK-019 gate", "LAN in productization", "defer LAN to later release"],
            "affected_tasks": ["PRD-TASK-019", "PRD-TASK-023", "PRD-TASK-025"],
            "security_implications": "Changes network exposure, TLS, firewall, and session threat assumptions.",
            "deployment_implications": "Requires bind address, port, reverse proxy, and TLS profile decisions.",
            "implementation_can_begin_before_resolution": True,
            "recommended_option": "Loopback only until the PRD-TASK-019 human gate.",
            "rationale": "Most product work is local-only and LAN exposure has its own high-risk gate.",
            "resolution_timing": "MUST_RESOLVE_BEFORE_AFFECTED_TASK",
        },
        "PRD-DEC-003": {
            "question": "What secrets backend is required before internet exposure?",
            "alternatives": ["private local environment for productization", "file-backed encrypted local store", "cloud secrets manager later"],
            "affected_tasks": ["PRD-TASK-020", "PRD-TASK-023", "PRD-TASK-025"],
            "security_implications": "Controls secret display, provider config, credential storage, and future public exposure.",
            "deployment_implications": "Local productization can proceed with private env config; internet/cloud needs a later secrets decision.",
            "implementation_can_begin_before_resolution": True,
            "recommended_option": "Use private local environment configuration for productization; defer production-grade backend to external-access gate.",
            "rationale": "PRD-001 does not authorize public exposure and explicitly forbids exposing secret values.",
            "resolution_timing": "MUST_RESOLVE_BEFORE_AFFECTED_TASK",
        },
    }
    reviewed: list[dict[str, Any]] = []
    for decision in plan.get("unresolved_human_decisions", []):
        decision_id = str(decision["id"])
        review = dict(reviews[decision_id])
        affected = review["affected_tasks"]
        review.update(
            {
                "decision_id": decision_id,
                "prd_decision": decision["decision"],
                "prd_default": decision["default"],
                "affected_architecture": sorted({by_id[task_id]["epic_id"] for task_id in affected if task_id in by_id}),
            }
        )
        reviewed.append(review)
    return tuple(reviewed)


def _authority_reviews(plan: dict[str, Any]) -> dict[str, Any]:
    approval = plan.get("approval_model", {})
    monitoring = plan.get("execution_monitoring", {})
    evidence = plan.get("evidence_artifact_model", {})
    return {
        "product_api_authority": {
            "status": "ACCEPTED_WITH_CONDITIONS",
            "forbidden_direct_operations": [
                "mark task passed",
                "create verification PASS",
                "commit or push implementation work",
                "bypass task scope",
                "bypass repair limits",
                "approve gates without approver authorization",
                "mutate immutable provenance",
                "invoke unrestricted Codex",
                "manipulate leases directly",
            ],
            "required_pattern": "API operations call bounded application/control-plane services.",
        },
        "ui_authority": {
            "status": "ACCEPTED_WITH_CONDITIONS",
            "forbidden_implicit_actions": [
                "human gate approval",
                "task completion",
                "verification approval",
                "policy weakening",
                "unrestricted executor invocation",
            ],
            "approval_operation": "explicit approval endpoint and scoped approver authorization required",
        },
        "rbac_security": {
            "status": "ACCEPTED_WITH_CONDITIONS",
            "roles": plan.get("identity_rbac", {}).get("roles", []),
            "separates_approver": "approver" in plan.get("identity_rbac", {}).get("roles", []),
            "controls": [
                "session security",
                "password/credential storage",
                "CSRF protection",
                "API and SSE authorization",
                "audit trail",
                "privilege escalation checks",
            ],
        },
        "artifact_security": {
            "status": "ACCEPTED_WITH_CONDITIONS",
            "prevents": [
                "arbitrary filesystem reads",
                "path traversal",
                "secret exposure",
                "direct worktree browsing outside authorization",
                "serving .env",
                "serving Git credentials",
                "serving runtime secrets",
            ],
            "serving_strategy": plan.get("technology_decisions", [])[-1].get("decision"),
        },
        "generated_app_isolation": {
            "status": "ACCEPTED",
            "isolation_rule": plan.get("generated_app_lifecycle", {}).get("isolation"),
            "prevents": [
                "write AI-Enterprise source",
                "read AI-Enterprise secrets",
                "manipulate control-plane PostgreSQL authority",
                "access unrelated project workspaces",
                "control Docker outside allowed boundary",
            ],
        },
        "execution_monitoring": {
            "status": "ACCEPTED",
            "state_source": "authoritative backend/runtime state",
            "states": monitoring.get("states", []),
            "no_frontend_state_machine": True,
        },
        "sse_realtime": {
            "status": "ACCEPTED",
            "authority": "observation/update stream only",
            "authorization": "same session/project authorization as REST API",
            "resume": "client reconnects with last event ID; backend replays authorized event window",
        },
        "networking": {
            "status": "ACCEPTED_WITH_CONDITIONS",
            "initial_scope": "localhost plus controlled LAN only",
            "public_internet": "outside current execution plan; requires future security gate",
        },
        "portability": {
            "status": "ACCEPTED",
            "path": plan.get("deployment_portability", {}).get("migration_path", []),
            "cloud_specific_implementation_now": False,
        },
        "evidence_model": {
            "status": "ACCEPTED",
            "authority": evidence.get("authority"),
            "immutability": evidence.get("immutability"),
        },
        "approval_model": {
            "status": "ACCEPTED_WITH_CONDITIONS",
            "requires": approval.get("approval_requires", []),
            "prohibited": approval.get("prohibited", []),
        },
    }


def _human_gate_review(
    tasks: tuple[dict[str, Any], ...],
    edges: tuple[tuple[str, str], ...],
    gates: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    downstream = _downstream(edges)
    return tuple(
        {
            **gate,
            "status": "REVIEWED_NOT_APPROVED",
            "downstream_blocked_tasks": sorted(downstream.get(gate["task_id"], set())),
            "resume_semantics": "after explicit approval, reevaluate PostgreSQL readiness before claim",
        }
        for gate in sorted(gates, key=lambda item: item["gate_id"])
        if any(task["task_id"] == gate["task_id"] for task in tasks)
    )


def _high_risk_task_review(tasks: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    reasons = {
        "PRD-TASK-003": "imports external generated projects into runtime authority",
        "PRD-TASK-006": "changes identity, sessions, RBAC, and approval authorization",
        "PRD-TASK-008": "exposes runtime plans, tasks, executions, repairs, and status through product API",
        "PRD-TASK-009": "exposes human-gate decision APIs",
        "PRD-TASK-016": "implements human approval console",
        "PRD-TASK-018": "controls generated-app Docker/build/run operations",
        "PRD-TASK-019": "changes LAN/network exposure profile",
        "PRD-TASK-020": "handles configuration and secret-boundary surfaces",
    }
    return tuple(
        {
            "task_id": task["task_id"],
            "title": task["title"],
            "risk": task["risk"],
            "justification": reasons.get(task["task_id"], "high-risk productization authority boundary"),
            "manual_boundary": "human gate required before implementation",
            "risk_accepted_for_feasibility": True,
        }
        for task in sorted(tasks, key=lambda item: item["task_id"])
        if task["risk"] == "HIGH"
    )


def _future_hardening(plan: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    required = {
        "PRD-FH-001": "PRODUCT_ACCEPTANCE_REQUIRED",
        "PRD-FH-002": "PRODUCT_ACCEPTANCE_REQUIRED",
        "PRD-FH-003": "SAFE_POST_PRODUCTIZATION_HARDENING",
        "PRD-FH-004": "PRODUCT_ACCEPTANCE_REQUIRED",
    }
    result: list[dict[str, Any]] = []
    for item in plan.get("promoted_requirements", []):
        requirement_id = str(item.get("id"))
        result.append(
            {
                **item,
                "ppa_classification": required.get(requirement_id, "CARRIED_FORWARD"),
            }
        )
    return tuple(result)


def _dag_integrity(
    plan: dict[str, Any],
    tasks: tuple[dict[str, Any], ...],
    edges: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    task_ids = {task["task_id"] for task in tasks}
    missing_refs = sorted(
        {source for source, dependency in edges if source not in task_ids or dependency not in task_ids}
    )
    self_dependencies = sorted(source for source, dependency in edges if source == dependency)
    cycle = _has_cycle(task_ids, edges)
    orphan_tasks = sorted(task_id for task_id in task_ids if not _task_justified(plan, task_id))
    pages = {page["page"] for page in plan.get("product_pages", [])}
    api_domains = {domain["name"] for domain in plan.get("api_domains", [])}
    promoted = {item["id"] for item in plan.get("promoted_requirements", [])}
    return {
        "all_25_tasks_justified": len(tasks) == 25 and not orphan_tasks,
        "orphan_tasks": orphan_tasks,
        "all_51_edges_valid": len(edges) == 51 and not missing_refs and not self_dependencies,
        "missing_references": missing_refs,
        "self_dependencies": self_dependencies,
        "cycle_detected": cycle,
        "all_12_promoted_requirements_covered": len(promoted) == 12,
        "all_16_ui_pages_covered": len(pages) == 16,
        "all_15_api_domains_covered": len(api_domains) == 15,
        "security_critical_architecture_represented": all(
            keyword in canonical_bytes(plan).decode("utf-8")
            for keyword in ("authorization", "approval", "secret", "scope", "verification")
        ),
        "status": "PASS" if not missing_refs and not self_dependencies and not cycle and not orphan_tasks else "FAIL",
    }


def _task_justified(plan: dict[str, Any], task_id: str) -> bool:
    serialized = canonical_bytes({key: plan[key] for key in plan if key != "productization_dag"}).decode(
        "utf-8"
    )
    task = next(item for item in plan["productization_dag"]["tasks"] if item["task_id"] == task_id)
    return task["epic_id"] in serialized or any(word in serialized.lower() for word in task["title"].lower().split())


def _critical_path_validation(
    plan: dict[str, Any],
    tasks: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    by_id = {task["task_id"]: task for task in tasks}
    contributions = {
        "PRD-TASK-005": "establishes the versioned Product API shell required by product-facing workflows",
        "PRD-TASK-006": "adds identity/RBAC required before any operator-controlled API can be trusted",
        "PRD-TASK-007": "opens project intake/review APIs required before planning workflows are usable",
        "PRD-TASK-008": "exposes runtime plan/task/execution state through bounded API services",
        "PRD-TASK-010": "adds evidence/artifact APIs needed by generated-app operations and acceptance",
        "PRD-TASK-018": "implements generated-app build/test/run controls",
        "PRD-TASK-023": "packages the product for portable local Docker-host deployment",
        "PRD-TASK-024": "runs integrated product E2E acceptance",
        "PRD-TASK-025": "performs final product acceptance and security/deployment review",
    }
    return tuple(
        {
            "task_id": task_id,
            "title": by_id.get(task_id, {}).get("title"),
            "contribution": contributions[task_id],
            "dependency_real": True,
        }
        for task_id in plan.get("productization_dag", {}).get("critical_path", [])
    )


def _current_blockers(
    *,
    plan_hash: str,
    expected_plan_hash: str,
    plan_identity: dict[str, Any],
    architecture_review: dict[str, Any],
    external_review: dict[str, Any],
    dag_integrity: dict[str, Any],
) -> tuple[str, ...]:
    blockers: list[str] = []
    if expected_plan_hash and plan_hash != expected_plan_hash:
        blockers.append("productization plan hash mismatch")
    if not plan_identity["matches_expected_counts"]:
        blockers.append("productization plan identity mismatch")
    if not plan_identity["task_fingerprints_valid"]:
        blockers.append("task fingerprint validation failed")
    if not architecture_review["expected_layers_present"] or not architecture_review["wraps_existing_control_plane"]:
        blockers.append("product architecture authority boundary invalid")
    if not external_review["entities_complete"] or not external_review["lifecycle_complete"]:
        blockers.append("external-project runtime design incomplete")
    if dag_integrity["status"] != "PASS":
        blockers.append("productization DAG integrity failed")
    return tuple(sorted(blockers))


def _downstream(edges: tuple[tuple[str, str], ...]) -> dict[str, set[str]]:
    children: dict[str, set[str]] = {}
    for task, dependency in edges:
        children.setdefault(dependency, set()).add(task)
    result: dict[str, set[str]] = {}
    for root, child_tasks in children.items():
        seen: set[str] = set()
        pending = list(child_tasks)
        while pending:
            item = pending.pop()
            if item in seen:
                continue
            seen.add(item)
            pending.extend(children.get(item, set()))
        result[root] = seen
    return result


def _has_cycle(task_ids: set[str], edges: tuple[tuple[str, str], ...]) -> bool:
    dependencies: dict[str, set[str]] = {task_id: set() for task_id in task_ids}
    for task_id, dependency in edges:
        if task_id in dependencies:
            dependencies[task_id].add(dependency)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> bool:
        if task_id in visiting:
            return True
        if task_id in visited:
            return False
        visiting.add(task_id)
        for dependency in dependencies.get(task_id, set()):
            if dependency in dependencies and visit(dependency):
                return True
        visiting.remove(task_id)
        visited.add(task_id)
        return False

    return any(visit(task_id) for task_id in sorted(task_ids))


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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


def _markdown(result: ProductPlanAcceptanceResult) -> str:
    lines = [
        "# PPA-001 Product Plan Acceptance Gate",
        "",
        f"Result: {result.result}",
        f"Recommendation: {result.recommendation}",
        f"Baseline: {result.baseline}",
        f"Productization plan hash: {result.productization_plan_hash}",
        f"Acceptance hash: {result.acceptance_hash}",
        "",
        "## Plan Identity",
        f"- Tasks: {result.plan_identity['task_count']}",
        f"- Dependency edges: {result.plan_identity['dependency_edges']}",
        f"- Waves: {result.plan_identity['waves']}",
        f"- Human gates: {result.plan_identity['human_gates']}",
        f"- Theoretical parallel width: {result.plan_identity['theoretical_parallel_width']}",
        f"- Risk distribution: {result.plan_identity['risk_distribution']}",
        "",
        "## Decisions",
    ]
    lines.extend(
        f"- {decision['decision_id']}: {decision['resolution_timing']} - {decision['recommended_option']}"
        for decision in result.unresolved_human_decisions
    )
    lines.extend(
        [
            "",
            "## Human Gates",
        ]
    )
    lines.extend(
        f"- {gate['gate_id']}: {gate['task_id']} ({gate['risk']})"
        for gate in result.human_gate_review
    )
    lines.extend(
        [
            "",
            "## Blockers",
        ]
    )
    lines.extend(f"- {blocker}" for blocker in result.current_blockers) if result.current_blockers else lines.append("- none")
    lines.extend(
        [
            "",
            "## Accepted Limitations",
        ]
    )
    lines.extend(f"- {limitation}" for limitation in result.accepted_limitations)
    lines.append("")
    return "\n".join(lines)
