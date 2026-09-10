from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ai_ent.project_manifest import GENERATED_MARKER, canonical_bytes

PRD_001_VERSION = "prd-001.1"
PRD_PLAN_ID = "PRODUCTIZATION-PLAN-001"

PRDResultValue = Literal["ACCEPTED", "ACCEPTED_WITH_LIMITATIONS", "REJECTED"]
RiskLevel = Literal["LOW", "MEDIUM", "HIGH"]
TaskType = Literal["ARCHITECTURE_TASK", "IMPLEMENTATION_TASK", "VERIFICATION_TASK"]
LimitationClass = Literal[
    "CURRENT_RELEASE_BLOCKER",
    "ACCEPTED_LIMITATION",
    "FUTURE_HARDENING",
    "DEFERRED_CAPABILITY",
]


@dataclass(frozen=True)
class ProductizationTask:
    task_id: str
    epic_id: str
    task_type: TaskType
    title: str
    objective: str
    dependencies: tuple[str, ...]
    risk: RiskLevel
    human_gate: str | None
    allowed_scope: tuple[str, ...]
    prohibited_scope: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_bytes(self.material_dict())).hexdigest()

    def material_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "epic_id": self.epic_id,
            "task_type": self.task_type,
            "title": self.title,
            "objective": self.objective,
            "dependencies": list(self.dependencies),
            "risk": self.risk,
            "human_gate": self.human_gate,
            "allowed_scope": list(self.allowed_scope),
            "prohibited_scope": list(self.prohibited_scope),
            "acceptance_criteria": list(self.acceptance_criteria),
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.material_dict(), "fingerprint": self.fingerprint}


@dataclass(frozen=True)
class ProductizationPlanResult:
    result: PRDResultValue
    recommendation: str
    baseline_head: str
    planner_version: str
    bound_evidence: dict[str, Any]
    product_objective: dict[str, Any]
    architecture: dict[str, Any]
    technology_decisions: tuple[dict[str, Any], ...]
    api_domains: tuple[dict[str, Any], ...]
    product_pages: tuple[dict[str, Any], ...]
    external_project_runtime: dict[str, Any]
    identity_rbac: dict[str, Any]
    approval_model: dict[str, Any]
    execution_monitoring: dict[str, Any]
    evidence_artifact_model: dict[str, Any]
    generated_app_lifecycle: dict[str, Any]
    network_architecture: dict[str, Any]
    deployment_portability: dict[str, Any]
    operations_model: dict[str, Any]
    threat_boundaries: tuple[dict[str, Any], ...]
    promoted_requirements: tuple[dict[str, Any], ...]
    tasks: tuple[ProductizationTask, ...]
    dependency_edges: tuple[tuple[str, str], ...]
    waves: tuple[dict[str, Any], ...]
    critical_path: tuple[str, ...]
    human_gates: tuple[dict[str, Any], ...]
    unresolved_human_decisions: tuple[dict[str, Any], ...]
    limitations: tuple[dict[str, str], ...]
    plan_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "phase": "PRD-001",
            "result": self.result,
            "recommendation": self.recommendation,
            "baseline_head": self.baseline_head,
            "planner_version": self.planner_version,
            "bound_evidence": self.bound_evidence,
            "product_objective": self.product_objective,
            "architecture": self.architecture,
            "technology_decisions": list(self.technology_decisions),
            "api_domains": list(self.api_domains),
            "product_pages": list(self.product_pages),
            "external_project_runtime": self.external_project_runtime,
            "identity_rbac": self.identity_rbac,
            "approval_model": self.approval_model,
            "execution_monitoring": self.execution_monitoring,
            "evidence_artifact_model": self.evidence_artifact_model,
            "generated_app_lifecycle": self.generated_app_lifecycle,
            "network_architecture": self.network_architecture,
            "deployment_portability": self.deployment_portability,
            "operations_model": self.operations_model,
            "threat_boundaries": list(self.threat_boundaries),
            "promoted_requirements": list(self.promoted_requirements),
            "productization_dag": {
                "plan_id": PRD_PLAN_ID,
                "tasks": [task.as_dict() for task in self.tasks],
                "dependency_edges": [list(edge) for edge in self.dependency_edges],
                "waves": list(self.waves),
                "critical_path": list(self.critical_path),
                "theoretical_parallel_width": max(len(wave["task_ids"]) for wave in self.waves),
                "human_gates": list(self.human_gates),
                "risk_distribution": _risk_distribution(self.tasks),
                "summary": {
                    "task_count": len(self.tasks),
                    "dependency_edges": len(self.dependency_edges),
                    "waves": len(self.waves),
                    "human_gates": len(self.human_gates),
                },
            },
            "unresolved_human_decisions": list(self.unresolved_human_decisions),
            "limitations": list(self.limitations),
            "plan_hash": self.plan_hash,
        }


def evaluate_productization_plan(
    *,
    artifacts_dir: Path = Path("artifacts"),
    repository_root: Path = Path("."),
) -> ProductizationPlanResult:
    baseline_head = _git_rev(repository_root, "HEAD")
    bound_evidence = _bound_evidence(artifacts_dir)
    tasks = _productization_tasks()
    edges = tuple(
        sorted((task.task_id, dependency) for task in tasks for dependency in task.dependencies)
    )
    waves = _waves(tasks)
    critical_path = _critical_path(tasks)
    human_gates = _human_gates(tasks)
    promoted = _promoted_requirements(bound_evidence)
    limitations = (
        {
            "classification": "FUTURE_HARDENING",
            "description": "PRD-001 is an architecture and planning artifact only; productization tasks are not imported or executed.",
        },
    )
    evidence_ok = _accepted(bound_evidence.get("saag_001", {})) and _accepted(
        bound_evidence.get("e2e_001", {})
    )
    result: PRDResultValue = "ACCEPTED_WITH_LIMITATIONS" if evidence_ok else "REJECTED"
    recommendation = "READY_FOR_PRODUCT_PLAN_ACCEPTANCE" if evidence_ok else "REMEDIATION_REQUIRED"
    payload = {
        "baseline_head": baseline_head,
        "planner_version": PRD_001_VERSION,
        "bound_evidence": bound_evidence,
        "product_objective": _product_objective(),
        "architecture": _architecture(),
        "technology_decisions": _technology_decisions(),
        "api_domains": _api_domains(),
        "product_pages": _product_pages(),
        "external_project_runtime": _external_project_runtime(),
        "identity_rbac": _identity_rbac(),
        "approval_model": _approval_model(),
        "execution_monitoring": _execution_monitoring(),
        "evidence_artifact_model": _evidence_artifact_model(),
        "generated_app_lifecycle": _generated_app_lifecycle(),
        "network_architecture": _network_architecture(),
        "deployment_portability": _deployment_portability(),
        "operations_model": _operations_model(),
        "threat_boundaries": _threat_boundaries(),
        "promoted_requirements": promoted,
        "tasks": [task.as_dict() for task in tasks],
        "dependency_edges": [list(edge) for edge in edges],
        "waves": waves,
        "critical_path": critical_path,
        "human_gates": human_gates,
        "unresolved_human_decisions": _unresolved_human_decisions(),
        "limitations": limitations,
        "result": result,
    }
    plan_hash = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    return ProductizationPlanResult(
        result=result,
        recommendation=recommendation,
        baseline_head=baseline_head,
        planner_version=PRD_001_VERSION,
        bound_evidence=bound_evidence,
        product_objective=_product_objective(),
        architecture=_architecture(),
        technology_decisions=_technology_decisions(),
        api_domains=_api_domains(),
        product_pages=_product_pages(),
        external_project_runtime=_external_project_runtime(),
        identity_rbac=_identity_rbac(),
        approval_model=_approval_model(),
        execution_monitoring=_execution_monitoring(),
        evidence_artifact_model=_evidence_artifact_model(),
        generated_app_lifecycle=_generated_app_lifecycle(),
        network_architecture=_network_architecture(),
        deployment_portability=_deployment_portability(),
        operations_model=_operations_model(),
        threat_boundaries=_threat_boundaries(),
        promoted_requirements=promoted,
        tasks=tasks,
        dependency_edges=edges,
        waves=waves,
        critical_path=critical_path,
        human_gates=human_gates,
        unresolved_human_decisions=_unresolved_human_decisions(),
        limitations=limitations,
        plan_hash=plan_hash,
    )


def write_productization_plan(
    *,
    artifacts_dir: Path = Path("artifacts"),
    output_dir: Path = Path(".build/compiled"),
    repository_root: Path = Path("."),
) -> ProductizationPlanResult:
    result = evaluate_productization_plan(
        artifacts_dir=artifacts_dir,
        repository_root=repository_root,
    )
    artifact_dir = artifacts_dir / "prd-001"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    _write_json(artifact_dir / "PRD-001.json", result.as_dict())
    (artifact_dir / "PRD-001.md").write_text(_markdown(result), encoding="utf-8")
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "productization-plan.json", result.as_dict())
    return result


def _bound_evidence(artifacts_dir: Path) -> dict[str, Any]:
    saag = _read_json(artifacts_dir / "saag-001" / "SAAG-001.json")
    e2e = _read_json(artifacts_dir / "e2e-001" / "E2E-001.json")
    pir = _read_json(artifacts_dir / "pir-002" / "PIR-002.json")
    return {
        "saag_001": {
            "result": saag.get("result"),
            "recommendation": saag.get("recommendation"),
            "head": saag.get("head"),
            "acceptance_hash": saag.get("acceptance_hash"),
            "proof_count": len(saag.get("proofs", [])),
            "negative_test_count": len(saag.get("negative_tests", [])),
            "limitations": saag.get("limitations", []),
        },
        "e2e_001": {
            "result": e2e.get("result"),
            "recommendation": e2e.get("recommendation"),
            "head": e2e.get("head"),
            "target_project_id": e2e.get("target_project_id"),
            "target_workspace": e2e.get("target_workspace"),
            "application_commit": e2e.get("application_commit"),
            "acceptance_hash": e2e.get("hashes", {}).get("acceptance_hash"),
            "proof_count": len(e2e.get("proofs", [])),
            "limitations": e2e.get("limitations", []),
        },
        "pir_002": {
            "result": pir.get("result"),
            "recommendation": pir.get("recommendation"),
            "residual_gap_count": len(pir.get("residual_gaps", [])),
            "hashes": pir.get("hashes", {}),
        },
    }


def _accepted(evidence: dict[str, Any]) -> bool:
    return evidence.get("result") in {"ACCEPTED", "ACCEPTED_WITH_LIMITATIONS"}


def _product_objective() -> dict[str, Any]:
    return {
        "objective": "Turn the accepted AI-Enterprise control plane into an authenticated operator-facing product.",
        "operator_workflow": [
            "open AI-Enterprise",
            "create a new application project",
            "describe the desired application",
            "review interpreted requirements",
            "review architecture and capability coverage",
            "review and accept generated implementation plans",
            "approve human gates explicitly",
            "start guarded implementation",
            "monitor executions and repairs",
            "inspect evidence and artifacts",
            "run or archive generated applications",
        ],
        "non_goals": [
            "replace the existing control-plane authority",
            "public internet exposure",
            "cloud-specific infrastructure",
            "implicit approval through UI navigation",
        ],
    }


def _architecture() -> dict[str, Any]:
    return {
        "layers": [
            "Browser",
            "Operator Web UI",
            "Product API",
            "Application Services",
            "Existing AI-Enterprise Control Plane",
            "PostgreSQL / Git / Worktrees / Docker / Codex",
        ],
        "authority_rule": "UI and API may request control-plane actions but cannot bypass gates, verification, scopes, execution limits, or commit boundaries.",
        "service_boundaries": [
            {"id": "PRODUCT-API", "responsibility": "Versioned product HTTP API for operators and future clients."},
            {"id": "IDENTITY", "responsibility": "Operator authentication, authorization, sessions, and audit identity."},
            {"id": "EXTERNAL-PROJECT-RUNTIME", "responsibility": "Reusable lifecycle for generated application workspaces and runtime bindings."},
            {"id": "APPROVALS", "responsibility": "Explicit scoped human-gate review and approval records."},
            {"id": "MONITORING", "responsibility": "Runtime status, task/execution state, repairs, leases, and verifier output."},
            {"id": "EVIDENCE-ARTIFACTS", "responsibility": "Non-authoritative provenance browsing and generated artifact access."},
            {"id": "GENERATED-APP-OPS", "responsibility": "Build/test/start/stop/log/archive generated applications."},
            {"id": "NETWORK-OPS", "responsibility": "Local/LAN binding, reverse proxy, TLS strategy, and future public gate."},
        ],
    }


def _technology_decisions() -> tuple[dict[str, Any], ...]:
    return (
        {
            "domain": "backend_api",
            "decision": "FastAPI with Pydantic schemas",
            "justification": "Provides typed OpenAPI boundaries for browser and future clients while aligning with existing Python/Pydantic service code.",
            "implementation_note": "Add only when product API implementation begins.",
        },
        {
            "domain": "frontend",
            "decision": "React + TypeScript + Vite",
            "justification": "Supports the operator console, DAG/evidence visualization, realtime views, and maintainable product UI.",
        },
        {
            "domain": "realtime",
            "decision": "Server-Sent Events first; WebSocket only if interactive control requires it",
            "justification": "Execution monitoring is primarily append/status streaming and should remain operationally simple.",
        },
        {
            "domain": "authentication",
            "decision": "Server-owned sessions backed by PostgreSQL with password hashing and RBAC",
            "justification": "Avoids exposing control-plane authority to browser-held secrets and supports auditable approvals.",
        },
        {
            "domain": "reverse_proxy",
            "decision": "Caddy or nginx in local/LAN deployment profile",
            "justification": "Terminates TLS and separates browser access from Python worker processes.",
        },
        {
            "domain": "artifact_serving",
            "decision": "API-mediated artifact metadata and scoped file download",
            "justification": "Prevents raw filesystem exposure and preserves secret/path filtering.",
        },
    )


def _api_domains() -> tuple[dict[str, Any], ...]:
    names = [
        "auth",
        "projects",
        "intake",
        "requirements",
        "architecture",
        "capabilities",
        "plans",
        "tasks",
        "executions",
        "gates",
        "evidence",
        "artifacts",
        "generated-applications",
        "runtime-status",
        "system-health",
    ]
    return tuple(
        {"name": name, "version": "v1", "boundary": "product API facade over existing services"}
        for name in names
    )


def _product_pages() -> tuple[dict[str, Any], ...]:
    pages = [
        ("Login", "Authenticate operator sessions."),
        ("Dashboard", "Summarize projects, gated work, failures, and system health."),
        ("Projects", "List and filter generated application projects."),
        ("New Project", "Submit application requests through intake."),
        ("Project Overview", "Show lifecycle, plan state, readiness, and generated app status."),
        ("Requirements", "Review interpreted and accepted requirements."),
        ("Architecture", "Inspect components, interfaces, and deployment boundaries."),
        ("Capability Matrix", "Show selected/deferred capability coverage."),
        ("Implementation Plan / DAG", "Visualize tasks, dependencies, waves, and gates."),
        ("Tasks", "Inspect runtime task state and scope contracts."),
        ("Executions", "Monitor attempts, repairs, verification, and commits."),
        ("Human Approvals", "Review and explicitly approve/reject gates."),
        ("Evidence", "Browse provenance paths without granting authority."),
        ("Artifacts", "Inspect generated files, docs, logs, and packages."),
        ("Generated Application", "Start, stop, test, and link to local app URLs."),
        ("System / Runtime Status", "Inspect PostgreSQL, Docker, Codex, workers, and recovery."),
    ]
    return tuple({"page": page, "purpose": purpose} for page, purpose in pages)


def _external_project_runtime() -> dict[str, Any]:
    return {
        "entities": [
            "ExternalProject",
            "ProjectWorkspace",
            "ProjectRepository",
            "ProjectPlan",
            "ProjectRuntimeBinding",
            "GeneratedArtifact",
        ],
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
        "genericity_rule": "No E2E-TEAM-WORK-TRACKER-specific code path may be required for future generated applications.",
        "isolation_rule": "Generated application source, Git repository, Docker services, and runtime config stay outside src/ai_ent/**.",
    }


def _identity_rbac() -> dict[str, Any]:
    return {
        "roles": ["authenticated_user", "project_operator", "approver", "administrator"],
        "controls": [
            "session management",
            "project authorization",
            "approval authorization",
            "administrative-action authorization",
            "audit trail for identity-bearing decisions",
        ],
        "separation": "Approver authority is distinct from ordinary project operation and administration.",
    }


def _approval_model() -> dict[str, Any]:
    return {
        "first_class_objects": True,
        "approval_requires": [
            "gate ID",
            "plan ID/version",
            "task ID",
            "risk",
            "reason",
            "write scope",
            "prohibited scope",
            "dependency evidence",
            "approval conditions",
            "operator identity",
            "timestamp",
        ],
        "prohibited": [
            "implicit approval by navigation",
            "agent self-approval",
            "approval of unrelated gates",
            "weakening frozen task contracts",
        ],
    }


def _execution_monitoring() -> dict[str, Any]:
    return {
        "states": [
            "READY",
            "WAITING",
            "GATED",
            "CLAIMED",
            "EXECUTING",
            "VERIFYING",
            "REPAIRING",
            "PASSED",
            "FAILED",
            "BLOCKED",
        ],
        "visible_fields": [
            "task",
            "execution",
            "attempt",
            "agent/model profile",
            "worktree",
            "changed files",
            "verification result",
            "repair history",
            "commit",
            "evidence",
        ],
        "redaction_rule": "Secret values and unsafe raw environment data are never exposed.",
    }


def _evidence_artifact_model() -> dict[str, Any]:
    return {
        "path": [
            "user request",
            "requirement",
            "capability",
            "architecture",
            "task",
            "execution",
            "candidate",
            "verification",
            "commit",
            "artifact",
        ],
        "authority": "Artifact Evidence Graph is NON_AUTHORITATIVE provenance/index.",
        "immutability": "Evidence records are append-oriented or immutably auditable.",
    }


def _generated_app_lifecycle() -> dict[str, Any]:
    return {
        "managed_operations": [
            "workspace location",
            "Git repository",
            "generated application status",
            "build",
            "tests",
            "Docker services",
            "local URL",
            "logs",
            "restart",
            "stop",
            "archive",
        ],
        "isolation": "Generated apps are isolated from the AI-Enterprise source tree and runtime secrets.",
    }


def _network_architecture() -> dict[str, Any]:
    return {
        "initial_target": "one workstation/server with Docker, PostgreSQL, and LAN browser clients",
        "binding": "operator-configured host/port; default loopback until LAN explicitly enabled",
        "reverse_proxy": "Caddy/nginx profile with TLS strategy before LAN broad use",
        "public_internet": "separate future security gate, not authorized by PRD-001",
    }


def _deployment_portability() -> dict[str, Any]:
    return {
        "migration_path": ["laptop", "Docker host", "multi-server", "AWS/Azure/GCP"],
        "portable_boundaries": [
            "PostgreSQL",
            "object/artifact storage",
            "Git/workspaces",
            "execution workers",
            "model providers",
            "secrets",
            "networking",
            "observability",
        ],
        "cloud_specific_work": "deferred until explicit deployment portability proof.",
    }


def _operations_model() -> dict[str, Any]:
    return {
        "operations": [
            "health/readiness",
            "structured logs",
            "metrics",
            "audit events",
            "backups",
            "migrations",
            "graceful shutdown",
            "recovery",
            "retention",
            "cleanup",
        ],
        "authority": "Operational controls call existing services and repositories; they do not mutate frozen plans directly.",
    }


def _threat_boundaries() -> tuple[dict[str, Any], ...]:
    return tuple(
        {"boundary": boundary, "controls": controls}
        for boundary, controls in [
            ("browser", ["CSRF/session protection", "RBAC", "no raw secrets"]),
            ("product API", ["schema validation", "authorization", "audit logging"]),
            ("PostgreSQL", ["transactional writes", "migration discipline", "least privilege"]),
            ("Codex", ["scoped execution packages", "no commit/approval authority"]),
            ("Docker", ["local/LAN profiles", "no public exposure by default"]),
            ("Git/worktrees", ["isolated worktrees", "verified commit boundary"]),
            ("generated applications", ["workspace isolation", "separate runtime config"]),
            ("LAN clients", ["authentication", "TLS/reverse proxy", "firewall assumptions"]),
            ("future internet clients", ["separate explicit security gate"]),
        ]
    )


def _promoted_requirements(bound_evidence: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    requirements = [
        {
            "id": "PRD-FH-001",
            "source": "E2E-001",
            "requirement": "Promote the target-app E2E harness into a reusable external-project runtime importer and executor.",
            "classification": "PRODUCTIZATION_REQUIRED",
        },
        {
            "id": "PRD-FH-002",
            "source": "RCG-001/PIR-002",
            "requirement": "Create first-class runtime import receipt artifacts for external and residual plans.",
            "classification": "PRODUCTIZATION_REQUIRED",
        },
        {
            "id": "PRD-FH-003",
            "source": "RCG-001/PIR-002",
            "requirement": "Harden generated lock projections against accepted DB receipt drift.",
            "classification": "FUTURE_HARDENING",
        },
        {
            "id": "PRD-FH-004",
            "source": "RCG-001/PIR-002",
            "requirement": "Add rich approval-principal and approval-scope documents to the human-gate model.",
            "classification": "PRODUCTIZATION_REQUIRED",
        },
    ]
    for phase in ("saag_001", "e2e_001"):
        for index, limitation in enumerate(bound_evidence.get(phase, {}).get("limitations", []), start=1):
            requirements.append(
                {
                    "id": f"PRD-CARRY-{phase.upper()}-{index:03d}",
                    "source": phase.upper(),
                    "requirement": str(limitation.get("description", limitation)),
                    "classification": str(limitation.get("classification", "FUTURE_HARDENING")),
                }
            )
    return tuple(requirements)


def _unresolved_human_decisions() -> tuple[dict[str, Any], ...]:
    return (
        {
            "id": "PRD-DEC-001",
            "decision": "Choose FastAPI/React product stack at Product Plan Acceptance.",
            "default": "accept unless operator requires a different stack.",
        },
        {
            "id": "PRD-DEC-002",
            "decision": "Approve LAN exposure profile before enabling non-loopback bind.",
            "default": "loopback only.",
        },
        {
            "id": "PRD-DEC-003",
            "decision": "Define first production-grade secrets backend before internet exposure.",
            "default": "private local environment configuration only.",
        },
    )


def _productization_tasks() -> tuple[ProductizationTask, ...]:
    scope = ("src/ai_ent/**", "scripts/**", "tests/**", "manifest/project/ai-ent/**")
    ui_scope = ("src/ai_ent_product_ui/**", "tests/**", "pyproject.toml")
    return (
        _task("PRD-TASK-001", "EP-EXT", "ARCHITECTURE_TASK", "External project domain model", "Define generic ExternalProject, workspace, repository, plan, binding, and artifact model.", (), "MEDIUM", None, scope),
        _task("PRD-TASK-002", "EP-EXT", "IMPLEMENTATION_TASK", "Project workspace and repository manager", "Implement isolated generated-app workspace and repository services.", ("PRD-TASK-001",), "MEDIUM", None, scope),
        _task("PRD-TASK-003", "EP-EXT", "IMPLEMENTATION_TASK", "Reusable external-project runtime importer", "Promote the E2E harness into generic frozen-plan import and runtime binding support.", ("PRD-TASK-002",), "HIGH", "GATE-PRD-EXTERNAL-RUNTIME", scope),
        _task("PRD-TASK-004", "EP-EXT", "IMPLEMENTATION_TASK", "Generated artifact registry", "Persist generated application artifacts, logs, docs, and workspace indexes without raw filesystem exposure.", ("PRD-TASK-002",), "MEDIUM", None, scope),
        _task("PRD-TASK-005", "EP-API", "ARCHITECTURE_TASK", "Product API shell", "Introduce versioned API boundary, schema conventions, errors, and service dependency wiring.", (), "MEDIUM", None, ("src/ai_ent_product_api/**", "tests/**", "pyproject.toml")),
        _task("PRD-TASK-006", "EP-SEC", "IMPLEMENTATION_TASK", "Operator authentication, sessions, and RBAC", "Implement product identity model and authorization checks for projects, approvals, and admin actions.", ("PRD-TASK-005",), "HIGH", "GATE-PRD-IDENTITY-RBAC", scope),
        _task("PRD-TASK-007", "EP-API", "IMPLEMENTATION_TASK", "Project intake and review APIs", "Expose bounded APIs for projects, intake, requirements, architecture, and capability review.", ("PRD-TASK-005", "PRD-TASK-006"), "MEDIUM", None, ("src/ai_ent_product_api/**", "tests/**")),
        _task("PRD-TASK-008", "EP-API", "IMPLEMENTATION_TASK", "Plan, task, and execution APIs", "Expose generated plan, DAG, task, execution, repair, and runtime state without granting control authority.", ("PRD-TASK-003", "PRD-TASK-007"), "HIGH", "GATE-PRD-RUNTIME-CONTROL", ("src/ai_ent_product_api/**", "src/ai_ent/**", "tests/**")),
        _task("PRD-TASK-009", "EP-HITL", "IMPLEMENTATION_TASK", "Human approval API", "Expose explicit gate review, approval, rejection, evidence, and scoped decision APIs.", ("PRD-TASK-006", "PRD-TASK-008"), "HIGH", "GATE-PRD-HITL-API", ("src/ai_ent_product_api/**", "src/ai_ent/**", "tests/**")),
        _task("PRD-TASK-010", "EP-EVID", "IMPLEMENTATION_TASK", "Evidence and artifact APIs", "Expose provenance queries and artifact metadata through redacted, non-authoritative API responses.", ("PRD-TASK-004", "PRD-TASK-008"), "MEDIUM", None, ("src/ai_ent_product_api/**", "src/ai_ent/**", "tests/**")),
        _task("PRD-TASK-011", "EP-OPS", "IMPLEMENTATION_TASK", "Runtime status and health APIs", "Expose system health, readiness, recovery, Docker, Codex, and PostgreSQL status safely.", ("PRD-TASK-005",), "LOW", None, ("src/ai_ent_product_api/**", "tests/**")),
        _task("PRD-TASK-012", "EP-UI", "ARCHITECTURE_TASK", "Operator UI shell, login, and dashboard", "Create product UI shell and first authenticated dashboard views.", ("PRD-TASK-005", "PRD-TASK-006"), "MEDIUM", None, ui_scope),
        _task("PRD-TASK-013", "EP-UI", "IMPLEMENTATION_TASK", "Project creation and overview UI", "Implement project list, new project, and overview workflows backed by intake APIs.", ("PRD-TASK-007", "PRD-TASK-012"), "MEDIUM", None, ui_scope),
        _task("PRD-TASK-014", "EP-UI", "IMPLEMENTATION_TASK", "Requirements, architecture, and capability review UI", "Implement review pages for requirements, architecture, capability matrix, and unresolved decisions.", ("PRD-TASK-007", "PRD-TASK-012"), "MEDIUM", None, ui_scope),
        _task("PRD-TASK-015", "EP-UI", "IMPLEMENTATION_TASK", "Implementation DAG and execution monitoring UI", "Implement task DAG, task details, execution attempts, repair history, verifier output, and commits.", ("PRD-TASK-008", "PRD-TASK-013"), "MEDIUM", None, ui_scope),
        _task("PRD-TASK-016", "EP-HITL", "IMPLEMENTATION_TASK", "Human approval console", "Implement explicit operator approval/rejection UI with gate-scoped conditions and evidence.", ("PRD-TASK-009", "PRD-TASK-015"), "HIGH", "GATE-PRD-HITL-UI", ui_scope),
        _task("PRD-TASK-017", "EP-EVID", "IMPLEMENTATION_TASK", "Evidence and artifact browser UI", "Implement provenance path and artifact browsing views while preserving non-authoritative evidence status.", ("PRD-TASK-010", "PRD-TASK-015"), "MEDIUM", None, ui_scope),
        _task("PRD-TASK-018", "EP-GENAPP", "IMPLEMENTATION_TASK", "Generated application operations", "Implement build/test/start/stop/restart/log/archive controls for isolated generated apps.", ("PRD-TASK-003", "PRD-TASK-008", "PRD-TASK-010"), "HIGH", "GATE-PRD-GENERATED-APP-OPS", scope),
        _task("PRD-TASK-019", "EP-NET", "IMPLEMENTATION_TASK", "Local and LAN access profile", "Implement explicit bind-address, port, reverse-proxy, TLS, and firewall-assumption configuration.", ("PRD-TASK-006", "PRD-TASK-011"), "HIGH", "GATE-PRD-LAN-ACCESS", ("src/ai_ent_product_api/**", "deployment/**", "tests/**", "manifest/project/ai-ent/**")),
        _task("PRD-TASK-020", "EP-CONFIG", "IMPLEMENTATION_TASK", "Configuration and secret boundary", "Implement structured non-secret configuration views and redacted provider/model/admin settings.", ("PRD-TASK-006", "PRD-TASK-011"), "HIGH", "GATE-PRD-CONFIG-SECRETS", scope),
        _task("PRD-TASK-021", "EP-OPS", "IMPLEMENTATION_TASK", "Audit logs, metrics, and operational events", "Implement product audit events, structured logs, metrics surfaces, and retention classifications.", ("PRD-TASK-009", "PRD-TASK-010", "PRD-TASK-011"), "MEDIUM", None, scope),
        _task("PRD-TASK-022", "EP-OPS", "IMPLEMENTATION_TASK", "Retention, cleanup, migrations, and backups", "Implement retention policies, cleanup jobs, migration checks, backup guidance, and graceful shutdown handling.", ("PRD-TASK-004", "PRD-TASK-021"), "MEDIUM", None, scope),
        _task("PRD-TASK-023", "EP-PORT", "ARCHITECTURE_TASK", "Deployment portability packaging", "Package local Docker-host deployment while preserving migration boundaries for future cloud targets.", ("PRD-TASK-018", "PRD-TASK-019", "PRD-TASK-020"), "MEDIUM", None, ("deployment/**", "scripts/**", "tests/**", "manifest/project/ai-ent/**")),
        _task("PRD-TASK-024", "EP-VERIFY", "VERIFICATION_TASK", "Integrated product E2E acceptance", "Verify create-project through guarded generated-app lifecycle via UI/API with provenance and recovery evidence.", ("PRD-TASK-013", "PRD-TASK-014", "PRD-TASK-015", "PRD-TASK-016", "PRD-TASK-017", "PRD-TASK-018", "PRD-TASK-019", "PRD-TASK-021", "PRD-TASK-023"), "MEDIUM", None, ("tests/**", "artifacts/**")),
        _task("PRD-TASK-025", "EP-VERIFY", "VERIFICATION_TASK", "Product acceptance gate", "Run product-level acceptance, security boundary checks, LAN readiness, and deployment portability review.", ("PRD-TASK-022", "PRD-TASK-024"), "LOW", None, ("tests/**", "artifacts/**")),
    )


def _task(
    task_id: str,
    epic_id: str,
    task_type: TaskType,
    title: str,
    objective: str,
    dependencies: tuple[str, ...],
    risk: RiskLevel,
    human_gate: str | None,
    allowed_scope: tuple[str, ...],
) -> ProductizationTask:
    return ProductizationTask(
        task_id=task_id,
        epic_id=epic_id,
        task_type=task_type,
        title=title,
        objective=objective,
        dependencies=dependencies,
        risk=risk,
        human_gate=human_gate,
        allowed_scope=allowed_scope,
        prohibited_scope=(".bootstrap/**", ".build/**", ".env", ".env.*", "aient/**"),
        acceptance_criteria=(
            "existing control-plane authority preserved",
            "independent verification remains mandatory",
            "human gates remain explicit",
            "no secret values exposed",
            "deterministic tests added or updated",
        ),
    )


def _human_gates(tasks: tuple[ProductizationTask, ...]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "gate_id": task.human_gate,
            "task_id": task.task_id,
            "risk": task.risk,
            "reason": _gate_reason(task),
            "approval_boundary": "explicit operator approval before task execution",
            "expected_evidence": [
                "frozen task fingerprint",
                "write/prohibited scope",
                "authority impact",
                "security impact",
                "verification profile",
            ],
        }
        for task in tasks
        if task.human_gate is not None
    )


def _gate_reason(task: ProductizationTask) -> str:
    if "approval" in task.title.lower() or "Human approval" in task.title:
        return "human-gate authority changes require explicit review"
    if "authentication" in task.title.lower():
        return "identity and authorization boundary changes require explicit review"
    if "runtime" in task.title.lower() or "execution" in task.title.lower():
        return "runtime control-plane access requires explicit review"
    if "LAN" in task.title or "access" in task.title.lower():
        return "network exposure changes require explicit review"
    if "secret" in task.title.lower() or "configuration" in task.title.lower():
        return "configuration and secret-boundary changes require explicit review"
    return "high-risk productization task requires explicit review"


def _waves(tasks: tuple[ProductizationTask, ...]) -> tuple[dict[str, Any], ...]:
    remaining = {task.task_id: set(task.dependencies) for task in tasks}
    completed: set[str] = set()
    waves: list[dict[str, Any]] = []
    while remaining:
        ready = sorted(task_id for task_id, dependencies in remaining.items() if dependencies <= completed)
        if not ready:
            raise ValueError("productization DAG contains a cycle or missing dependency")
        waves.append({"id": f"PRD-WAVE-{len(waves) + 1:03d}", "task_ids": ready})
        completed.update(ready)
        for task_id in ready:
            del remaining[task_id]
    return tuple(waves)


def _critical_path(tasks: tuple[ProductizationTask, ...]) -> tuple[str, ...]:
    by_id = {task.task_id: task for task in tasks}
    memo: dict[str, tuple[str, ...]] = {}

    def path_to(task_id: str) -> tuple[str, ...]:
        if task_id in memo:
            return memo[task_id]
        task = by_id[task_id]
        if not task.dependencies:
            memo[task_id] = (task_id,)
            return memo[task_id]
        best = max((path_to(dependency) for dependency in task.dependencies), key=lambda path: (len(path), path))
        memo[task_id] = (*best, task_id)
        return memo[task_id]

    return max((path_to(task.task_id) for task in tasks), key=lambda path: (len(path), path))


def _risk_distribution(tasks: tuple[ProductizationTask, ...]) -> dict[str, int]:
    distribution: dict[str, int] = {}
    for task in tasks:
        distribution[task.risk] = distribution.get(task.risk, 0) + 1
    return dict(sorted(distribution.items()))


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"result": "MISSING", "path": str(path)}
    value = __import__("json").loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {"result": "INVALID", "path": str(path)}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(value) + b"\n")


def _git_rev(cwd: Path, rev: str) -> str:
    result = __import__("subprocess").run(
        ["git", "rev-parse", rev],
        cwd=cwd,
        text=True,
        stdout=__import__("subprocess").PIPE,
        stderr=__import__("subprocess").STDOUT,
        timeout=30,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"


def _markdown(result: ProductizationPlanResult) -> str:
    dag = result.as_dict()["productization_dag"]
    lines = [
        "# PRD-001 AI-Enterprise Productization Architecture & Plan",
        "",
        f"Result: {result.result}",
        f"Recommendation: {result.recommendation}",
        f"Baseline: {result.baseline_head}",
        f"Plan hash: {result.plan_hash}",
        "",
        "## Architecture",
        result.architecture["authority_rule"],
        "",
        "## Technology Decisions",
    ]
    lines.extend(
        f"- {decision['domain']}: {decision['decision']}" for decision in result.technology_decisions
    )
    lines.extend(
        [
            "",
            "## Productization DAG",
            f"- Tasks: {dag['summary']['task_count']}",
            f"- Dependency edges: {dag['summary']['dependency_edges']}",
            f"- Waves: {dag['summary']['waves']}",
            f"- Critical path: {' -> '.join(dag['critical_path'])}",
            f"- Theoretical parallel width: {dag['theoretical_parallel_width']}",
            f"- Human gates: {dag['summary']['human_gates']}",
            f"- Risk distribution: {dag['risk_distribution']}",
            "",
            "## Promoted Requirements",
        ]
    )
    lines.extend(f"- {item['id']}: {item['requirement']}" for item in result.promoted_requirements)
    lines.extend(
        [
            "",
            "## Limitations",
        ]
    )
    lines.extend(f"- {item['classification']}: {item['description']}" for item in result.limitations)
    lines.append("")
    return "\n".join(lines)
