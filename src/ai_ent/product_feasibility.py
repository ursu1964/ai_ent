from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ai_ent.product_plan_acceptance import EXPECTED_PRODUCTIZATION_PLAN_HASH
from ai_ent.project_manifest import GENERATED_MARKER, canonical_bytes

PFE_001_VERSION = "pfe-001.1"

FeasibilityStatus = Literal[
    "FEASIBLE",
    "FEASIBLE_WITH_CONDITIONS",
    "HUMAN_APPROVAL_REQUIRED",
    "BLOCKED",
    "DEFERRED",
]
PolicyDecision = Literal["AUTO_ALLOWED", "GUARDED_ALLOWED", "HUMAN_APPROVAL_REQUIRED", "PROHIBITED"]
PlanStatus = Literal[
    "READY_FOR_PRODUCT_DRY_RUN", "READY_WITH_DECISIONS_REQUIRED", "BLOCKED", "INVALID"
]
Availability = Literal["AVAILABLE", "CONFIGURABLE", "MISSING", "DEFERRED"]


@dataclass(frozen=True)
class ProductTaskContract:
    task_id: str
    objective: str
    dependencies: tuple[str, ...]
    risk: str
    human_gate: str | None
    agent_role: str
    model_profile: str
    executor: str
    verification_profile: str
    allowed_write_scope: tuple[str, ...]
    prohibited_write_scope: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    required_tools: tuple[str, ...]
    required_infrastructure: tuple[str, ...]
    required_decisions: tuple[str, ...]
    policy_decision: PolicyDecision
    feasibility_status: FeasibilityStatus
    blockers: tuple[str, ...]
    conditions: tuple[str, ...]
    source_task_fingerprint: str

    @property
    def contract_fingerprint(self) -> str:
        return hashlib.sha256(canonical_bytes(self.material_dict())).hexdigest()

    def material_dict(self) -> dict[str, Any]:
        return {
            "acceptance_criteria": list(self.acceptance_criteria),
            "agent_role": self.agent_role,
            "allowed_write_scope": list(self.allowed_write_scope),
            "blockers": list(self.blockers),
            "conditions": list(self.conditions),
            "dependencies": list(self.dependencies),
            "executor": self.executor,
            "feasibility_status": self.feasibility_status,
            "human_gate": self.human_gate,
            "model_profile": self.model_profile,
            "objective": self.objective,
            "policy_decision": self.policy_decision,
            "prohibited_write_scope": list(self.prohibited_write_scope),
            "required_decisions": list(self.required_decisions),
            "required_infrastructure": list(self.required_infrastructure),
            "required_tools": list(self.required_tools),
            "risk": self.risk,
            "source_task_fingerprint": self.source_task_fingerprint,
            "task_id": self.task_id,
            "verification_profile": self.verification_profile,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.material_dict(), "contract_fingerprint": self.contract_fingerprint}


@dataclass(frozen=True)
class ProductFeasibilityResult:
    result: PlanStatus
    recommendation: str
    baseline: str
    evaluator_version: str
    productization_plan_hash: str
    ppa_acceptance_hash: str
    product_feasibility_hash: str
    task_contracts: tuple[ProductTaskContract, ...]
    environment_profile: dict[str, Any]
    policy_profile: dict[str, Any]
    decision_plan: dict[str, Any]
    security_plan: dict[str, Any]
    human_gate_plan: tuple[dict[str, Any], ...]
    concurrency_plan: dict[str, Any]
    findings: tuple[dict[str, Any], ...]

    @property
    def ok(self) -> bool:
        return self.result in {"READY_FOR_PRODUCT_DRY_RUN", "READY_WITH_DECISIONS_REQUIRED"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "phase": "PFE-001",
            "result": self.result,
            "recommendation": self.recommendation,
            "baseline": self.baseline,
            "evaluator_version": self.evaluator_version,
            "productization_plan_hash": self.productization_plan_hash,
            "ppa_acceptance_hash": self.ppa_acceptance_hash,
            "product_feasibility_hash": self.product_feasibility_hash,
            "summary": _summary(self.task_contracts, self.human_gate_plan, self.findings),
            "environment_profile": self.environment_profile,
            "policy_profile": self.policy_profile,
            "decision_plan": self.decision_plan,
            "security_plan": self.security_plan,
            "human_gate_plan": list(self.human_gate_plan),
            "concurrency_plan": self.concurrency_plan,
            "task_contracts": [contract.as_dict() for contract in self.task_contracts],
            "findings": list(self.findings),
        }


def evaluate_product_feasibility(
    *,
    plan_path: Path = Path(".build/compiled/productization-plan.json"),
    ppa_path: Path = Path("artifacts/ppa-001/PPA-001.json"),
    repository_root: Path = Path("."),
    expected_plan_hash: str = EXPECTED_PRODUCTIZATION_PLAN_HASH,
    environment_override: dict[str, Any] | None = None,
    policy_override: dict[str, Any] | None = None,
) -> ProductFeasibilityResult:
    plan = _read_json(plan_path)
    ppa = _read_json(ppa_path)
    baseline = _git_rev(repository_root, "HEAD")
    environment = _environment_profile(repository_root, environment_override or {})
    policy = _policy_profile(policy_override or {})
    decision_plan = _decision_plan(ppa)
    security_plan = _security_plan(policy)
    contracts = _contracts(plan, environment, policy, decision_plan)
    gates = _human_gate_plan(plan)
    concurrency = _concurrency_plan(plan, contracts, policy)
    findings = _findings(
        plan,
        ppa,
        contracts,
        environment,
        decision_plan,
        expected_plan_hash=expected_plan_hash,
    )
    result = _plan_result(findings, decision_plan)
    recommendation = (
        "READY_FOR_PRODUCT_DRY_RUN"
        if result == "READY_FOR_PRODUCT_DRY_RUN"
        else "READY_FOR_PRODUCT_DRY_RUN_AFTER_DECISION_PRD_DEC_001"
        if result == "READY_WITH_DECISIONS_REQUIRED"
        else "REMEDIATION_REQUIRED"
    )
    payload = {
        "baseline": baseline,
        "concurrency_plan": concurrency,
        "decision_plan": decision_plan,
        "environment_profile": environment,
        "evaluator_version": PFE_001_VERSION,
        "findings": findings,
        "human_gate_plan": gates,
        "policy_profile": policy,
        "ppa_acceptance_hash": ppa.get("acceptance_hash"),
        "productization_plan_hash": plan.get("plan_hash"),
        "result": result,
        "security_plan": security_plan,
        "task_contracts": [contract.as_dict() for contract in contracts],
    }
    feasibility_hash = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    return ProductFeasibilityResult(
        result=result,
        recommendation=recommendation,
        baseline=baseline,
        evaluator_version=PFE_001_VERSION,
        productization_plan_hash=str(plan.get("plan_hash")),
        ppa_acceptance_hash=str(ppa.get("acceptance_hash")),
        product_feasibility_hash=feasibility_hash,
        task_contracts=contracts,
        environment_profile=environment,
        policy_profile=policy,
        decision_plan=decision_plan,
        security_plan=security_plan,
        human_gate_plan=gates,
        concurrency_plan=concurrency,
        findings=findings,
    )


def write_product_feasibility(
    *,
    plan_path: Path = Path(".build/compiled/productization-plan.json"),
    ppa_path: Path = Path("artifacts/ppa-001/PPA-001.json"),
    artifacts_dir: Path = Path("artifacts"),
    output_dir: Path = Path(".build/compiled"),
    repository_root: Path = Path("."),
    expected_plan_hash: str = EXPECTED_PRODUCTIZATION_PLAN_HASH,
) -> ProductFeasibilityResult:
    result = evaluate_product_feasibility(
        plan_path=plan_path,
        ppa_path=ppa_path,
        repository_root=repository_root,
        expected_plan_hash=expected_plan_hash,
    )
    artifact_dir = artifacts_dir / "pfe-001"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(artifact_dir / "PFE-001.json", result.as_dict())
    (artifact_dir / "PFE-001.md").write_text(_markdown(result), encoding="utf-8")
    _write_json(output_dir / "product-feasibility-report.json", result.as_dict())
    _write_json(
        output_dir / "product-task-contracts.json",
        [item.as_dict() for item in result.task_contracts],
    )
    _write_json(
        output_dir / "product-policy-decisions.json", _policy_decisions(result.task_contracts)
    )
    _write_json(output_dir / "product-human-gate-plan.json", result.human_gate_plan)
    _write_json(output_dir / "product-decision-plan.json", result.decision_plan)
    _write_json(output_dir / "product-security-plan.json", result.security_plan)
    _write_json(output_dir / "product-concurrency-plan.json", result.concurrency_plan)
    return result


def _environment_profile(root: Path, override: dict[str, Any]) -> dict[str, Any]:
    def executable(name: str) -> Availability:
        return "AVAILABLE" if shutil.which(name) else "MISSING"

    def module(name: str) -> Availability:
        import importlib.util

        return "AVAILABLE" if importlib.util.find_spec(name) is not None else "CONFIGURABLE"

    docker = executable("docker")
    profile: dict[str, Any] = {
        "aient_codex_command": {
            "configured": bool(os.environ.get("AIENT_CODEX_COMMAND")),
            "status": "AVAILABLE" if os.environ.get("AIENT_CODEX_COMMAND") else "CONFIGURABLE",
        },
        "alembic": {"status": module("alembic")},
        "codex": {"path": _redact_path(shutil.which("codex") or ""), "status": executable("codex")},
        "docker": {"status": docker, "version": _version(["docker", "--version"])},
        "docker_compose": {
            "status": "AVAILABLE"
            if docker == "AVAILABLE" and _run(["docker", "compose", "version"]).returncode == 0
            else "MISSING",
            "version": _version(["docker", "compose", "version"]) if docker == "AVAILABLE" else "",
        },
        "fastapi": {"condition": "add product API dependency", "status": module("fastapi")},
        "filesystem_workspace_root": {"path": str(root.resolve()), "status": "AVAILABLE"},
        "git_worktrees": {"status": executable("git")},
        "lan_networking": {
            "condition": "requires PRD-DEC-002 and GATE-PRD-LAN-ACCESS",
            "status": "CONFIGURABLE",
        },
        "localhost_networking": {"status": "AVAILABLE"},
        "node": {"status": executable("node"), "version": _version(["node", "--version"])},
        "npm": {"status": executable("npm"), "version": _version(["npm", "--version"])},
        "postgresql": {"status": executable("psql")},
        "pydantic": {"status": module("pydantic")},
        "python": {"executable": _redact_path(shutil.which("python") or ""), "status": "AVAILABLE"},
        "react_typescript_vite": {
            "condition": "create product UI package and install pinned npm dependencies",
            "status": "CONFIGURABLE",
        },
        "reverse_proxy": {
            "condition": "install/configure Caddy or nginx before PRD-TASK-019 if LAN is enabled",
            "status": "CONFIGURABLE"
            if not (shutil.which("caddy") or shutil.which("nginx"))
            else "AVAILABLE",
        },
        "uvicorn": {"condition": "add product API server dependency", "status": module("uvicorn")},
    }
    return _deep_merge(profile, override)


def _policy_profile(override: dict[str, Any]) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "auto_allowed_risks": ["LOW"],
        "available_agent_roles": [
            "AGT-002:Planner Worker",
            "AGT-003:Generator Worker",
            "AGT-003:Generator Worker/Security Reviewer",
            "AGT-003:Generator Worker/UI Specialist",
            "AGT-004:Verification Worker",
        ],
        "available_model_profiles": [
            "MDL-001:Codex executor model:high-risk-guarded",
            "MDL-001:Codex executor model:standard",
            "MDL-001:Codex executor model:verification",
        ],
        "available_verification_profiles": [
            "API_AUTHORIZATION_TESTS",
            "FRONTEND_BUILD_AND_TESTS",
            "FULL_REGRESSION_SECURITY",
            "PRODUCT_E2E_ACCEPTANCE",
            "STANDARD_REGRESSION",
        ],
        "docker_operations": [
            "compose config",
            "compose build",
            "compose up/down for scoped generated apps",
            "logs",
        ],
        "docker_prohibited": [
            "host daemon administration",
            "privileged containers",
            "unscoped volume mounts",
            "public ingress by default",
        ],
        "guarded_allowed_risks": ["MEDIUM"],
        "human_gate_required_risks": ["HIGH"],
        "max_feasible_width": 1,
        "prohibited_paths": [".bootstrap/**", ".build/**", ".env", ".env.*", "aient/**"],
        "sse_authority": "observation_only",
    }
    return _deep_merge(profile, override)


def _decision_plan(ppa: dict[str, Any]) -> dict[str, Any]:
    decisions = {item["decision_id"]: item for item in ppa.get("unresolved_human_decisions", [])}
    return {
        "PRD-DEC-001": {
            **decisions.get("PRD-DEC-001", {}),
            "blocks_plan_freeze": True,
            "decision_evidence_required": [
                "PFE environment confirms Python/Pydantic and Node/npm availability",
                "FastAPI/uvicorn are configurable product dependencies rather than platform blockers",
                "API/UI separation preserves control-plane authority",
                "SSE remains observation-only",
            ],
            "pfe_recommendation": "ACCEPT",
            "recommended_stack": {
                "backend_api": "FastAPI + Pydantic",
                "frontend": "React + TypeScript + Vite",
                "realtime": "SSE-first",
            },
            "state": "UNRESOLVED",
        },
        "PRD-DEC-002": {
            **decisions.get("PRD-DEC-002", {}),
            "blocks_plan_freeze": False,
            "current_position": "localhost operation must work independently; LAN/public ingress remains closed until gate.",
            "first_required_task": "PRD-TASK-019",
            "state": "TRACKED_UNRESOLVED",
        },
        "PRD-DEC-003": {
            **decisions.get("PRD-DEC-003", {}),
            "blocks_plan_freeze": False,
            "current_position": "local private environment config is enough for localhost productization; internet/cloud secrets backend is later.",
            "first_required_task": "PRD-TASK-020",
            "state": "TRACKED_UNRESOLVED",
        },
    }


def _contracts(
    plan: dict[str, Any],
    environment: dict[str, Any],
    policy: dict[str, Any],
    decision_plan: dict[str, Any],
) -> tuple[ProductTaskContract, ...]:
    tasks = tuple(dict(item) for item in plan.get("productization_dag", {}).get("tasks", []))
    initial = [
        _contract(task, environment=environment, policy=policy, decision_plan=decision_plan)
        for task in tasks
    ]
    return _propagate_dependency_blocking(tuple(sorted(initial, key=lambda item: item.task_id)))


def _contract(
    task: dict[str, Any],
    *,
    environment: dict[str, Any],
    policy: dict[str, Any],
    decision_plan: dict[str, Any],
) -> ProductTaskContract:
    task_id = str(task["task_id"])
    required_decisions = _required_decisions(task_id, decision_plan)
    human_gate = task.get("human_gate")
    risk = str(task["risk"])
    role = _agent_role(task)
    model = _model_profile(task)
    executor = _executor(task)
    verifier = _verification_profile(task)
    tools = _required_tools(task)
    infra = _required_infrastructure(task)
    policy_decision = _policy_decision(risk, human_gate, policy)
    blockers = _contract_blockers(task, environment, policy, role, model, executor, verifier)
    conditions = _contract_conditions(task, environment, required_decisions)
    status = _feasibility_status(
        blockers=blockers,
        conditions=conditions,
        policy_decision=policy_decision,
    )
    return ProductTaskContract(
        task_id=task_id,
        objective=str(task["objective"]),
        dependencies=tuple(str(item) for item in task.get("dependencies", [])),
        risk=risk,
        human_gate=str(human_gate) if human_gate else None,
        agent_role=role,
        model_profile=model,
        executor=executor,
        verification_profile=verifier,
        allowed_write_scope=tuple(str(item) for item in task.get("allowed_scope", [])),
        prohibited_write_scope=tuple(str(item) for item in task.get("prohibited_scope", [])),
        acceptance_criteria=tuple(str(item) for item in task.get("acceptance_criteria", [])),
        required_tools=tools,
        required_infrastructure=infra,
        required_decisions=required_decisions,
        policy_decision=policy_decision,
        feasibility_status=status,
        blockers=blockers,
        conditions=conditions,
        source_task_fingerprint=str(task["fingerprint"]),
    )


def _required_decisions(task_id: str, decision_plan: dict[str, Any]) -> tuple[str, ...]:
    required: list[str] = []
    for decision_id, decision in decision_plan.items():
        if task_id in decision.get("affected_tasks", []):
            required.append(f"DECISION_REQUIRED:{decision_id}")
    return tuple(sorted(required))


def _agent_role(task: dict[str, Any]) -> str:
    task_id = str(task["task_id"])
    title = str(task["title"]).lower()
    if task_id in {"PRD-TASK-024", "PRD-TASK-025"} or task["task_type"] == "VERIFICATION_TASK":
        return "AGT-004:Verification Worker"
    if "ui" in title or "frontend" in title:
        return "AGT-003:Generator Worker/UI Specialist"
    if "authentication" in title or "approval" in title or "secret" in title or "lan" in title:
        return "AGT-003:Generator Worker/Security Reviewer"
    if task["task_type"] == "ARCHITECTURE_TASK":
        return "AGT-002:Planner Worker"
    return "AGT-003:Generator Worker"


def _model_profile(task: dict[str, Any]) -> str:
    if task["task_type"] == "VERIFICATION_TASK":
        return "MDL-001:Codex executor model:verification"
    if str(task["risk"]) == "HIGH":
        return "MDL-001:Codex executor model:high-risk-guarded"
    return "MDL-001:Codex executor model:standard"


def _executor(task: dict[str, Any]) -> str:
    if task["task_type"] == "VERIFICATION_TASK":
        return "IndependentVerifier"
    return "CodexExecutor via GuardedAutonomousRunner"


def _verification_profile(task: dict[str, Any]) -> str:
    task_id = str(task["task_id"])
    title = str(task["title"]).lower()
    if task_id in {"PRD-TASK-024", "PRD-TASK-025"}:
        return "PRODUCT_E2E_ACCEPTANCE"
    if str(task["risk"]) == "HIGH":
        return "FULL_REGRESSION_SECURITY"
    if "ui" in title or "frontend" in title:
        return "FRONTEND_BUILD_AND_TESTS"
    if "api" in title or "authentication" in title:
        return "API_AUTHORIZATION_TESTS"
    return "STANDARD_REGRESSION"


def _required_tools(task: dict[str, Any]) -> tuple[str, ...]:
    tools = {"git", "pytest", "ruff", "pyright"}
    text = f"{task['title']} {task['objective']}".lower()
    if any(keyword in text for keyword in ("api", "authentication", "session", "rbac")):
        tools.update({"fastapi", "pydantic", "postgresql"})
    if any(keyword in text for keyword in ("ui", "frontend", "dashboard", "react")):
        tools.update({"node", "npm", "typescript", "vite"})
    if any(keyword in text for keyword in ("docker", "generated application", "lan", "deployment")):
        tools.update({"docker", "docker_compose"})
    if any(keyword in text for keyword in ("migration", "backup", "postgresql")):
        tools.add("alembic")
    return tuple(sorted(tools))


def _required_infrastructure(task: dict[str, Any]) -> tuple[str, ...]:
    infra = {"filesystem_workspaces", "git_worktrees", "postgresql"}
    text = f"{task['title']} {task['objective']}".lower()
    if any(keyword in text for keyword in ("docker", "generated application", "deployment")):
        infra.add("docker")
    if any(keyword in text for keyword in ("lan", "reverse proxy")):
        infra.update({"lan_networking", "localhost_networking", "reverse_proxy"})
    if any(keyword in text for keyword in ("ui", "frontend")):
        infra.add("node_toolchain")
    return tuple(sorted(infra))


def _policy_decision(risk: str, human_gate: object, policy: dict[str, Any]) -> PolicyDecision:
    if risk in policy["human_gate_required_risks"] or human_gate:
        return "HUMAN_APPROVAL_REQUIRED"
    if risk in policy["auto_allowed_risks"]:
        return "AUTO_ALLOWED"
    if risk in policy["guarded_allowed_risks"]:
        return "GUARDED_ALLOWED"
    return "PROHIBITED"


def _contract_blockers(
    task: dict[str, Any],
    environment: dict[str, Any],
    policy: dict[str, Any],
    role: str,
    model: str,
    executor: str,
    verifier: str,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if role not in policy["available_agent_roles"]:
        blockers.append("missing agent role")
    if model not in policy["available_model_profiles"]:
        blockers.append("missing model profile")
    if not executor:
        blockers.append("missing executor")
    if verifier not in policy["available_verification_profiles"]:
        blockers.append("missing verification profile")
    if str(task["risk"]) == "HIGH" and not task.get("human_gate"):
        blockers.append("high-risk task missing human gate")
    tools = _required_tools(task)
    if "node" in tools and environment["node"]["status"] == "MISSING":
        blockers.append("missing Node.js for product UI tasks")
    if "npm" in tools and environment["npm"]["status"] == "MISSING":
        blockers.append("missing npm for product UI tasks")
    if "docker" in tools and environment["docker"]["status"] == "MISSING":
        blockers.append("missing Docker for generated-app operations")
    if "docker_compose" in tools and environment["docker_compose"]["status"] == "MISSING":
        blockers.append("missing Docker Compose for generated-app operations")
    if "postgresql" in tools and environment["postgresql"]["status"] == "MISSING":
        blockers.append("missing PostgreSQL client/runtime")
    return tuple(sorted(set(blockers)))


def _contract_conditions(
    task: dict[str, Any],
    environment: dict[str, Any],
    required_decisions: tuple[str, ...],
) -> tuple[str, ...]:
    conditions = list(required_decisions)
    tools = _required_tools(task)
    if "fastapi" in tools and environment["fastapi"]["status"] == "CONFIGURABLE":
        conditions.append("CONFIGURE_DEPENDENCY:fastapi")
    if "fastapi" in tools and environment["uvicorn"]["status"] == "CONFIGURABLE":
        conditions.append("CONFIGURE_DEPENDENCY:uvicorn")
    if {"typescript", "vite"} & set(tools) and environment["react_typescript_vite"][
        "status"
    ] == "CONFIGURABLE":
        conditions.append("CONFIGURE_DEPENDENCY:react-typescript-vite")
    if (
        "reverse_proxy" in _required_infrastructure(task)
        and environment["reverse_proxy"]["status"] == "CONFIGURABLE"
    ):
        conditions.append("CONFIGURE_DEPENDENCY:reverse-proxy-before-lan")
    if environment["aient_codex_command"]["status"] == "CONFIGURABLE":
        conditions.append("EXECUTION_PREREQUISITE:AIENT_CODEX_COMMAND")
    return tuple(sorted(set(conditions)))


def _feasibility_status(
    *,
    blockers: tuple[str, ...],
    conditions: tuple[str, ...],
    policy_decision: PolicyDecision,
) -> FeasibilityStatus:
    if blockers or policy_decision == "PROHIBITED":
        return "BLOCKED"
    if policy_decision == "HUMAN_APPROVAL_REQUIRED":
        return "HUMAN_APPROVAL_REQUIRED"
    if conditions:
        return "FEASIBLE_WITH_CONDITIONS"
    return "FEASIBLE"


def _propagate_dependency_blocking(
    contracts: tuple[ProductTaskContract, ...],
) -> tuple[ProductTaskContract, ...]:
    by_id = {contract.task_id: contract for contract in contracts}
    changed = True
    while changed:
        changed = False
        for contract in tuple(by_id.values()):
            dependency_blockers = [
                f"BLOCKED_BY_DEPENDENCY:{dependency_id}"
                for dependency_id in contract.dependencies
                if by_id[dependency_id].feasibility_status == "BLOCKED"
            ]
            if dependency_blockers and not set(dependency_blockers).issubset(
                set(contract.blockers)
            ):
                by_id[contract.task_id] = ProductTaskContract(
                    **{
                        **contract.material_dict(),
                        "blockers": tuple(
                            sorted(set(contract.blockers + tuple(dependency_blockers)))
                        ),
                        "feasibility_status": "BLOCKED",
                    }
                )
                changed = True
    return tuple(sorted(by_id.values(), key=lambda item: item.task_id))


def _security_plan(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "api_security": {
            "authority": "facade over existing bounded application/control-plane services",
            "forbidden_direct_operations": [
                "mark tasks passed",
                "forge verification PASS",
                "manipulate leases",
                "bypass gates",
                "bypass task scope",
                "bypass repair limits",
                "commit or push generated work",
                "change immutable plan fingerprints",
                "invoke unrestricted Codex",
                "read arbitrary filesystem paths",
            ],
        },
        "artifact_serving": {
            "rejected_patterns": [
                "path traversal",
                "serving .env",
                "serving Git credentials",
                "serving runtime secrets",
            ],
            "requires": [
                "project-scoped identifiers",
                "server-side path resolution",
                "canonical-path containment checks",
                "allowlisted artifact roots",
                "authorization before access",
                "secret redaction",
                "no raw arbitrary filesystem path API",
            ],
        },
        "authentication_rbac": {
            "approval_authorization": "approver role distinct from ordinary project operator where required",
            "audit_events": "required for authentication, approval, admin, and execution-control actions",
            "project_authorization": "required for API, UI, SSE, artifacts, and generated-app operations",
            "roles": ["authenticated_user", "operator", "approver", "administrator"],
            "session_security": "server-owned sessions with browser write CSRF strategy",
        },
        "docker_boundary": {
            "control": "human-gated generated-app operation task plus scoped Docker policy",
            "minimum_operations": policy["docker_operations"],
            "prohibited": policy["docker_prohibited"],
            "risk": "HIGH",
        },
        "external_project_runtime": {
            "feasible": True,
            "isolation_rejections": [
                "generated apps write AI-Enterprise source",
                "generated apps read AI-Enterprise secrets",
                "generated apps mutate control-plane PostgreSQL authority",
                "generated apps access unrelated workspaces",
                "generated apps control Docker outside scoped boundary",
            ],
            "requires": [
                "workspace creation",
                "Git repository creation/binding",
                "project isolation",
                "plan binding",
                "runtime import",
                "Docker lifecycle",
                "logs",
                "health",
                "stop/restart",
                "archive",
            ],
        },
        "sse": {
            "authorization": "project-scoped authenticated connection",
            "authority": policy["sse_authority"],
            "commands": "prohibited",
            "ordering": "monotonic backend event sequence",
            "resume": "Last-Event-ID or equivalent cursor",
            "retention": "bounded event window",
        },
    }


def _human_gate_plan(plan: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    gates = plan.get("productization_dag", {}).get("human_gates", [])
    return tuple(
        {
            **gate,
            "resume_semantics": "explicit approval, then reevaluate readiness before claim",
            "status": "PENDING_NOT_APPROVED",
        }
        for gate in sorted(gates, key=lambda item: item["gate_id"])
    )


def _concurrency_plan(
    plan: dict[str, Any],
    contracts: tuple[ProductTaskContract, ...],
    policy: dict[str, Any],
) -> dict[str, Any]:
    return {
        "conflict_classes": [
            "write-scope overlap",
            "shared API contracts",
            "shared frontend contracts",
            "DB migrations",
            "authentication/security work",
            "Docker/generated-app lifecycle",
            "human-gated authority boundaries",
            "model/resource contention",
        ],
        "feasible_width": policy["max_feasible_width"],
        "not_enabled": True,
        "reason": "productization changes central API/UI/security/runtime boundaries; start sequentially even though the DAG has wider theoretical parallelism",
        "task_count": len(contracts),
        "theoretical_parallel_width": plan.get("productization_dag", {}).get(
            "theoretical_parallel_width"
        ),
    }


def _findings(
    plan: dict[str, Any],
    ppa: dict[str, Any],
    contracts: tuple[ProductTaskContract, ...],
    environment: dict[str, Any],
    decision_plan: dict[str, Any],
    *,
    expected_plan_hash: str,
) -> tuple[dict[str, Any], ...]:
    findings: list[dict[str, Any]] = []
    if plan.get("plan_hash") != expected_plan_hash:
        findings.append(
            {
                "id": "PFE-PLAN-HASH",
                "message": "accepted productization plan hash mismatch",
                "severity": "ERROR",
            }
        )
    if ppa.get("result") != "ACCEPTED_WITH_DECISIONS_REQUIRED":
        findings.append(
            {
                "id": "PFE-PPA-STATE",
                "message": "PPA-001 acceptance state is not usable",
                "severity": "ERROR",
            }
        )
    if decision_plan["PRD-DEC-001"]["state"] == "UNRESOLVED":
        findings.append(
            {
                "id": "PFE-PRD-DEC-001",
                "message": "FastAPI/Pydantic + React/TypeScript/Vite must be explicitly accepted before PDF freeze",
                "severity": "DECISION_REQUIRED",
            }
        )
    for tool in ("node", "npm", "docker", "docker_compose", "postgresql", "git_worktrees", "codex"):
        if environment[tool]["status"] == "MISSING":
            findings.append(
                {
                    "id": f"PFE-ENV-{tool.upper()}",
                    "message": f"{tool} is missing",
                    "severity": "ERROR",
                }
            )
    blocked = [
        contract.task_id for contract in contracts if contract.feasibility_status == "BLOCKED"
    ]
    if blocked:
        findings.append(
            {
                "id": "PFE-TASK-BLOCKED",
                "message": "one or more product tasks are blocked",
                "severity": "ERROR",
                "task_ids": blocked,
            }
        )
    return tuple(sorted(findings, key=lambda item: item["id"]))


def _plan_result(findings: tuple[dict[str, Any], ...], decision_plan: dict[str, Any]) -> PlanStatus:
    if any(item["severity"] == "ERROR" for item in findings):
        return "BLOCKED"
    if decision_plan["PRD-DEC-001"]["state"] == "UNRESOLVED":
        return "READY_WITH_DECISIONS_REQUIRED"
    return "READY_FOR_PRODUCT_DRY_RUN"


def _summary(
    contracts: tuple[ProductTaskContract, ...],
    human_gate_plan: tuple[dict[str, Any], ...],
    findings: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    feasibility_counts = _count_by(contracts, "feasibility_status")
    policy_counts = _count_by(contracts, "policy_decision")
    risk_counts = _count_by(contracts, "risk")
    return {
        "auto_allowed": policy_counts.get("AUTO_ALLOWED", 0),
        "blocked": feasibility_counts.get("BLOCKED", 0),
        "decision_blockers": len(
            [item for item in findings if item["severity"] == "DECISION_REQUIRED"]
        ),
        "deferred": feasibility_counts.get("DEFERRED", 0),
        "feasible": feasibility_counts.get("FEASIBLE", 0),
        "feasible_width": 1,
        "feasible_with_conditions": feasibility_counts.get("FEASIBLE_WITH_CONDITIONS", 0),
        "guarded_allowed": policy_counts.get("GUARDED_ALLOWED", 0),
        "human_approval_required": feasibility_counts.get("HUMAN_APPROVAL_REQUIRED", 0),
        "human_gates": len(human_gate_plan),
        "policy_human_approval_required": policy_counts.get("HUMAN_APPROVAL_REQUIRED", 0),
        "prohibited": policy_counts.get("PROHIBITED", 0),
        "risk_distribution": risk_counts,
        "technical_blockers": len([item for item in findings if item["severity"] == "ERROR"]),
        "theoretical_width": 6,
        "total_tasks": len(contracts),
    }


def _count_by(contracts: tuple[ProductTaskContract, ...], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for contract in contracts:
        value = str(getattr(contract, field))
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _policy_decisions(contracts: tuple[ProductTaskContract, ...]) -> list[dict[str, Any]]:
    return [
        {
            "human_gate": contract.human_gate,
            "policy_decision": contract.policy_decision,
            "risk": contract.risk,
            "task_id": contract.task_id,
        }
        for contract in contracts
    ]


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
    )


def _version(command: list[str]) -> str:
    if not shutil.which(command[0]):
        return ""
    result = _run(command)
    return (
        result.stdout.strip().splitlines()[0]
        if result.returncode == 0 and result.stdout.strip()
        else ""
    )


def _redact_path(value: str) -> str:
    return value.replace(str(Path.home()), "$HOME") if value else value


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


def _read_json(path: Path) -> dict[str, Any]:
    value = __import__("json").loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(value) + b"\n")


def _markdown(result: ProductFeasibilityResult) -> str:
    summary = result.as_dict()["summary"]
    lines = [
        "# PFE-001 Product Feasibility + Security/Policy Evaluation",
        "",
        f"Result: {result.result}",
        f"Recommendation: {result.recommendation}",
        f"Baseline: {result.baseline}",
        f"Productization plan hash: {result.productization_plan_hash}",
        f"Product feasibility hash: {result.product_feasibility_hash}",
        "",
        "## Summary",
        f"- Total tasks: {summary['total_tasks']}",
        f"- Feasible: {summary['feasible']}",
        f"- Feasible with conditions: {summary['feasible_with_conditions']}",
        f"- Human approval required: {summary['human_approval_required']}",
        f"- Blocked: {summary['blocked']}",
        f"- Deferred: {summary['deferred']}",
        f"- Human gates: {summary['human_gates']}",
        f"- Theoretical width: {summary['theoretical_width']}",
        f"- Feasible width: {summary['feasible_width']}",
        f"- Risk distribution: {summary['risk_distribution']}",
        "",
        "## Decisions",
    ]
    for decision_id, decision in sorted(result.decision_plan.items()):
        lines.append(
            f"- {decision_id}: {decision['state']} - {decision.get('pfe_recommendation', decision.get('current_position'))}"
        )
    lines.extend(["", "## Findings"])
    lines.extend(
        f"- {finding['severity']} {finding['id']}: {finding['message']}"
        for finding in result.findings
    )
    lines.append("")
    return "\n".join(lines)
