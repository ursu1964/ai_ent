from __future__ import annotations

import hashlib
import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ai_ent.project_manifest import (
    GENERATED_MARKER,
    EnvironmentProfile,
    FeasibilityFacet,
    FeasibilityPolicyProfile,
    PolicyDecision,
    canonical_bytes,
    current_environment_profile,
)
from ai_ent.residual_dag import (
    ResidualGeneratedTask,
    ResidualImplementationPlan,
    generate_residual_implementation_plan,
)

RFE_EVALUATOR_VERSION = "rfe-001.1"

ResidualFeasibilityStatus = Literal[
    "FEASIBLE",
    "FEASIBLE_WITH_CONDITIONS",
    "HUMAN_APPROVAL_REQUIRED",
    "BLOCKED",
    "DEFERRED",
]
ResidualPlanFeasibilityStatus = Literal["READY_FOR_RESIDUAL_DRY_RUN", "READY_WITH_HUMAN_GATES", "BLOCKED", "INVALID"]
ResidualFindingSeverity = Literal["ERROR", "WARNING", "INFO"]

EXPECTED_RESIDUAL_TASK_IDS = (
    "RES-C05-EVIDENCE",
    "RES-C06-EVIDENCE",
    "RES-C07-EVIDENCE",
    "RES-C08-CONTRACT",
    "RES-C08-SERVICE",
    "RES-C08-VERIFICATION",
    "RES-C09-EVIDENCE",
)
EVIDENCE_SOURCE_FILES = {
    "C05": ("src/ai_ent/manifest_compiler.py", "src/ai_ent/project_manifest.py", "tests/test_project_manifest.py"),
    "C06": ("src/ai_ent/generator_orchestrator.py", "src/ai_ent/deterministic_validator.py", "tests/test_generator_orchestrator.py"),
    "C07": ("src/ai_ent/runtime_kernel.py", "src/ai_ent/scheduler/bounded.py", "tests/test_runtime_kernel.py"),
    "C09": ("src/ai_ent/runtime_kernel.py", "src/ai_ent/execution_planner.py", "tests/test_execution_planner.py"),
}


@dataclass(frozen=True)
class ResidualTaskFeasibility:
    task_id: str
    task_type: str
    feasibility_status: ResidualFeasibilityStatus
    policy_decision: PolicyDecision
    risk: str
    blockers: tuple[str, ...]
    conditions: tuple[str, ...]
    human_gate_ids: tuple[str, ...]
    agent_feasibility: FeasibilityFacet
    model_feasibility: FeasibilityFacet
    executor_feasibility: FeasibilityFacet
    verification_feasibility: FeasibilityFacet
    write_scope_feasibility: FeasibilityFacet
    infrastructure_feasibility: FeasibilityFacet
    resource_feasibility: FeasibilityFacet
    dependency_feasibility: FeasibilityFacet
    evidence_feasibility: FeasibilityFacet

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "feasibility_status": self.feasibility_status,
            "policy_decision": self.policy_decision,
            "risk": self.risk,
            "blockers": list(self.blockers),
            "conditions": list(self.conditions),
            "human_gate_ids": list(self.human_gate_ids),
            "agent_feasibility": self.agent_feasibility.as_dict(),
            "model_feasibility": self.model_feasibility.as_dict(),
            "executor_feasibility": self.executor_feasibility.as_dict(),
            "verification_feasibility": self.verification_feasibility.as_dict(),
            "write_scope_feasibility": self.write_scope_feasibility.as_dict(),
            "infrastructure_feasibility": self.infrastructure_feasibility.as_dict(),
            "resource_feasibility": self.resource_feasibility.as_dict(),
            "dependency_feasibility": self.dependency_feasibility.as_dict(),
            "evidence_feasibility": self.evidence_feasibility.as_dict(),
        }


@dataclass(frozen=True)
class ResidualFeasibilityFinding:
    finding_id: str
    severity: ResidualFindingSeverity
    subject_id: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "finding_id": self.finding_id,
            "severity": self.severity,
            "subject_id": self.subject_id,
            "message": self.message,
        }


@dataclass(frozen=True)
class ResidualFeasibilityEvaluation:
    evaluator_version: str
    residual_plan_hash: str
    head: str
    post_compiled_hash: str
    post_capability_hash: str
    post_trace_hash: str
    pir_residual_gap_hash: str
    residual_feasibility_hash: str
    plan_status: ResidualPlanFeasibilityStatus
    environment_profile: EnvironmentProfile
    policy_profile: FeasibilityPolicyProfile
    task_results: tuple[ResidualTaskFeasibility, ...]
    human_gates: tuple[dict[str, Any], ...]
    feasible_parallel_width: int
    technical_blockers: tuple[str, ...]
    evidence_task_findings: tuple[ResidualFeasibilityFinding, ...]
    c08_findings: tuple[ResidualFeasibilityFinding, ...]
    findings: tuple[ResidualFeasibilityFinding, ...]

    @property
    def ok(self) -> bool:
        return self.plan_status in {"READY_FOR_RESIDUAL_DRY_RUN", "READY_WITH_HUMAN_GATES"}

    def as_dict(self) -> dict[str, Any]:
        status_counts = {
            status: len([result for result in self.task_results if result.feasibility_status == status])
            for status in ("FEASIBLE", "FEASIBLE_WITH_CONDITIONS", "HUMAN_APPROVAL_REQUIRED", "BLOCKED", "DEFERRED")
        }
        policy_counts = {
            decision: len([result for result in self.task_results if result.policy_decision == decision])
            for decision in ("AUTO_ALLOWED", "GUARDED_ALLOWED", "HUMAN_APPROVAL_REQUIRED", "PROHIBITED")
        }
        return {
            "generated": GENERATED_MARKER,
            "evaluator_version": self.evaluator_version,
            "residual_plan_hash": self.residual_plan_hash,
            "head": self.head,
            "post_compiled_hash": self.post_compiled_hash,
            "post_capability_hash": self.post_capability_hash,
            "post_trace_hash": self.post_trace_hash,
            "pir_residual_gap_hash": self.pir_residual_gap_hash,
            "residual_feasibility_hash": self.residual_feasibility_hash,
            "plan_status": self.plan_status,
            "environment_profile": self.environment_profile.as_dict(),
            "policy_profile": self.policy_profile.as_dict(),
            "task_results": [result.as_dict() for result in self.task_results],
            "human_gates": list(self.human_gates),
            "feasible_parallel_width": self.feasible_parallel_width,
            "technical_blockers": list(self.technical_blockers),
            "evidence_task_findings": [finding.as_dict() for finding in self.evidence_task_findings],
            "c08_findings": [finding.as_dict() for finding in self.c08_findings],
            "findings": [finding.as_dict() for finding in self.findings],
            "summary": {
                "total_tasks": len(self.task_results),
                "feasible": status_counts["FEASIBLE"],
                "feasible_with_conditions": status_counts["FEASIBLE_WITH_CONDITIONS"],
                "human_approval_required": status_counts["HUMAN_APPROVAL_REQUIRED"],
                "blocked": status_counts["BLOCKED"],
                "deferred": status_counts["DEFERRED"],
                "auto_allowed": policy_counts["AUTO_ALLOWED"],
                "guarded_allowed": policy_counts["GUARDED_ALLOWED"],
                "prohibited": policy_counts["PROHIBITED"],
                "technical_blockers": len(self.technical_blockers),
                "human_gates": len(self.human_gates),
                "feasible_parallel_width": self.feasible_parallel_width,
            },
        }


def evaluate_residual_feasibility(
    *,
    plan: ResidualImplementationPlan | None = None,
    environment: EnvironmentProfile | None = None,
    policy: FeasibilityPolicyProfile | None = None,
    repository_root: Path = Path("."),
    manifest_root: Path = Path("manifest/project/ai-ent"),
    pir_artifact: Path = Path("artifacts/pir-001/PIR-001.json"),
) -> ResidualFeasibilityEvaluation:
    residual_plan = plan or generate_residual_implementation_plan(
        manifest_root=manifest_root,
        pir_artifact=pir_artifact,
        repository_root=repository_root,
    )
    environment_profile = environment or current_environment_profile(repository_root)
    policy_profile = policy or default_residual_feasibility_policy(residual_plan)
    return evaluate_residual_plan_feasibility(
        residual_plan,
        environment_profile,
        policy_profile,
        repository_root=repository_root,
    )


def evaluate_residual_plan_feasibility(
    plan: ResidualImplementationPlan,
    environment: EnvironmentProfile,
    policy: FeasibilityPolicyProfile,
    *,
    repository_root: Path = Path("."),
) -> ResidualFeasibilityEvaluation:
    preliminary = tuple(_task_feasibility(task, plan, environment, policy, repository_root) for task in plan.tasks)
    blocked_tasks = {result.task_id for result in preliminary if result.feasibility_status == "BLOCKED"}
    task_by_id = {task.id: task for task in plan.tasks}
    propagated: list[ResidualTaskFeasibility] = []
    for result in preliminary:
        blocked_dependencies = tuple(dependency for dependency in task_by_id[result.task_id].depends_on if dependency in blocked_tasks)
        if blocked_dependencies and result.feasibility_status != "BLOCKED":
            propagated.append(_block_by_dependency(result, blocked_dependencies))
        else:
            propagated.append(result)
    task_results = tuple(sorted(propagated, key=lambda item: item.task_id))
    technical_blockers = tuple(sorted({blocker for result in task_results for blocker in result.blockers}))
    if any(finding.severity == "ERROR" for finding in plan.findings) or tuple(task.id for task in plan.tasks) != EXPECTED_RESIDUAL_TASK_IDS:
        plan_status: ResidualPlanFeasibilityStatus = "INVALID"
    elif any(result.feasibility_status == "BLOCKED" for result in task_results):
        plan_status = "BLOCKED"
    elif any(result.feasibility_status == "HUMAN_APPROVAL_REQUIRED" for result in task_results):
        plan_status = "READY_WITH_HUMAN_GATES"
    else:
        plan_status = "READY_FOR_RESIDUAL_DRY_RUN"
    evidence_findings = tuple(
        sorted(
            (
                _evidence_finding(result)
                for result in task_results
                if result.task_type == "EVIDENCE_CLOSURE_TASK"
            ),
            key=lambda item: item.finding_id,
        )
    )
    c08_findings = tuple(
        sorted(
            (
                ResidualFeasibilityFinding(
                    f"RFE-{result.task_id}-C08",
                    "INFO",
                    result.task_id,
                    "C08 task is bounded by existing runtime, verifier, gate, and Git authority separation.",
                )
                for result in task_results
                if "C08" in _task_capabilities(task_by_id[result.task_id])
            ),
            key=lambda item: item.finding_id,
        )
    )
    findings = tuple(sorted((*evidence_findings, *c08_findings, *_blocker_findings(task_results)), key=lambda item: item.finding_id))
    feasible_parallel_width = _feasible_parallel_width(plan, task_results, policy)
    payload = {
        "evaluator_version": RFE_EVALUATOR_VERSION,
        "residual_plan_hash": plan.residual_plan_hash,
        "head": plan.head,
        "post_compiled_hash": plan.post_compiled_hash,
        "post_capability_hash": plan.post_capability_hash,
        "post_trace_hash": plan.post_trace_hash,
        "pir_residual_gap_hash": plan.pir_residual_gap_hash,
        "environment_profile": environment.as_dict(),
        "policy_profile": policy.as_dict(),
        "task_results": [result.as_dict() for result in task_results],
        "human_gates": list(plan.human_gates),
        "feasible_parallel_width": feasible_parallel_width,
        "technical_blockers": list(technical_blockers),
        "evidence_task_findings": [finding.as_dict() for finding in evidence_findings],
        "c08_findings": [finding.as_dict() for finding in c08_findings],
        "findings": [finding.as_dict() for finding in findings],
        "plan_status": plan_status,
    }
    feasibility_hash = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    return ResidualFeasibilityEvaluation(
        evaluator_version=RFE_EVALUATOR_VERSION,
        residual_plan_hash=plan.residual_plan_hash,
        head=plan.head,
        post_compiled_hash=plan.post_compiled_hash,
        post_capability_hash=plan.post_capability_hash,
        post_trace_hash=plan.post_trace_hash,
        pir_residual_gap_hash=plan.pir_residual_gap_hash,
        residual_feasibility_hash=feasibility_hash,
        plan_status=plan_status,
        environment_profile=environment,
        policy_profile=policy,
        task_results=task_results,
        human_gates=plan.human_gates,
        feasible_parallel_width=feasible_parallel_width,
        technical_blockers=technical_blockers,
        evidence_task_findings=evidence_findings,
        c08_findings=c08_findings,
        findings=findings,
    )


def write_residual_feasibility(
    *,
    output_dir: Path = Path(".build/compiled"),
    repository_root: Path = Path("."),
    manifest_root: Path = Path("manifest/project/ai-ent"),
    pir_artifact: Path = Path("artifacts/pir-001/PIR-001.json"),
) -> ResidualFeasibilityEvaluation:
    residual_plan = generate_residual_implementation_plan(
        manifest_root=manifest_root,
        pir_artifact=pir_artifact,
        repository_root=repository_root,
    )
    evaluation = evaluate_residual_feasibility(
        plan=residual_plan,
        repository_root=repository_root,
        manifest_root=manifest_root,
        pir_artifact=pir_artifact,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "residual-feasibility-report.json", evaluation.as_dict())
    _write_json(
        output_dir / "residual-task-feasibility.json",
        {
            "generated": GENERATED_MARKER,
            "residual_feasibility_hash": evaluation.residual_feasibility_hash,
            "task_results": [result.as_dict() for result in evaluation.task_results],
        },
    )
    _write_json(
        output_dir / "residual-policy-decisions.json",
        {
            "generated": GENERATED_MARKER,
            "residual_feasibility_hash": evaluation.residual_feasibility_hash,
            "policy_decisions": [
                {
                    "task_id": result.task_id,
                    "risk": result.risk,
                    "policy_decision": result.policy_decision,
                    "human_gate_ids": list(result.human_gate_ids),
                }
                for result in evaluation.task_results
            ],
        },
    )
    _write_json(
        output_dir / "residual-human-gate-plan.json",
        {
            "generated": GENERATED_MARKER,
            "residual_feasibility_hash": evaluation.residual_feasibility_hash,
            "human_gates": list(evaluation.human_gates),
        },
    )
    _write_json(
        output_dir / "residual-concurrency-plan.json",
        {
            "generated": GENERATED_MARKER,
            "residual_feasibility_hash": evaluation.residual_feasibility_hash,
            "theoretical_parallel_width": max((len(wave["task_ids"]) for wave in residual_plan.waves), default=0),
            "feasible_parallel_width": evaluation.feasible_parallel_width,
            "reason": "residual tasks share source, manifest, test, and verification scopes; concurrency is not enabled by RFE",
        },
    )
    return evaluation


def default_residual_feasibility_policy(plan: ResidualImplementationPlan) -> FeasibilityPolicyProfile:
    return FeasibilityPolicyProfile(
        profile_id="residual-guarded-policy",
        agent_roles=tuple(sorted({task.execution["agent_role"] for task in plan.tasks})),
        model_profiles=("ARCHITECTURE_REASONING", "CODING_HIGH", "CODING_STANDARD", "SECURITY_REVIEW"),
        executors=("codex",),
        verification_profiles=("FULL_REGRESSION", "STANDARD_REGRESSION"),
        auto_allowed_risks=("LOW",),
        guarded_allowed_risks=("LOW", "MEDIUM", "HIGH"),
        prohibited_path_patterns=(".env", ".env.*", "aient/**", ".bootstrap/**", ".build/**"),
        max_parallel_width=2,
    )


def _task_feasibility(
    task: ResidualGeneratedTask,
    plan: ResidualImplementationPlan,
    environment: EnvironmentProfile,
    policy: FeasibilityPolicyProfile,
    repository_root: Path,
) -> ResidualTaskFeasibility:
    blockers: list[str] = []
    conditions: list[str] = []
    agent = _facet(task.execution["agent_role"] in policy.agent_roles, f"agent role {task.execution['agent_role']} is available")
    model = _facet(task.execution["model_profile"] in policy.model_profiles, f"model profile {task.execution['model_profile']} is available")
    executor = _executor_feasibility(task.execution["executor"], environment, policy)
    verification = _facet(
        str(task.verification["profile"]) in policy.verification_profiles,
        f"verification profile {task.verification['profile']} is available",
    )
    write_scope = _write_scope_feasibility(task, policy, repository_root)
    infrastructure = _infrastructure_feasibility(environment)
    resources = _resource_feasibility(task, environment)
    dependency = _dependency_feasibility(task, plan)
    evidence = _evidence_feasibility(task, repository_root)
    for facet in (agent, model, executor, verification, write_scope, infrastructure, resources, dependency, evidence):
        if facet.status == "MISSING":
            blockers.extend(facet.reasons)
        if facet.status == "CONDITIONAL":
            conditions.extend(facet.reasons)
    gates = tuple(sorted(str(gate["id"]) for gate in plan.human_gates if gate["task_id"] == task.id))
    decision = _policy_decision(task, policy)
    if decision == "PROHIBITED":
        blockers.append("policy prohibits residual task execution")
    if blockers:
        status: ResidualFeasibilityStatus = "BLOCKED"
    elif gates:
        status = "HUMAN_APPROVAL_REQUIRED"
    elif conditions:
        status = "FEASIBLE_WITH_CONDITIONS"
    else:
        status = "FEASIBLE"
    return ResidualTaskFeasibility(
        task_id=task.id,
        task_type=task.task_type,
        feasibility_status=status,
        policy_decision=decision,
        risk=task.risk["level"],
        blockers=tuple(sorted(set(blockers))),
        conditions=tuple(sorted(set(conditions))),
        human_gate_ids=gates,
        agent_feasibility=agent,
        model_feasibility=model,
        executor_feasibility=executor,
        verification_feasibility=verification,
        write_scope_feasibility=write_scope,
        infrastructure_feasibility=infrastructure,
        resource_feasibility=resources,
        dependency_feasibility=dependency,
        evidence_feasibility=evidence,
    )


def _facet(ok: bool, ok_reason: str) -> FeasibilityFacet:
    if ok:
        return FeasibilityFacet("AVAILABLE", (ok_reason,))
    return FeasibilityFacet("MISSING", (ok_reason.replace(" is available", " is missing"),))


def _executor_feasibility(
    executor_name: str,
    environment: EnvironmentProfile,
    policy: FeasibilityPolicyProfile,
) -> FeasibilityFacet:
    if executor_name not in policy.executors:
        return FeasibilityFacet("MISSING", (f"executor {executor_name} is not allowed by policy",))
    if executor_name != "codex":
        return FeasibilityFacet("AVAILABLE", (f"executor {executor_name} is available",))
    command = os.environ.get("AIENT_CODEX_COMMAND", "").strip()
    executable = shlex.split(command)[0] if command else "codex"
    executable_available = shutil.which(executable) is not None or environment.codex_executable_available
    if environment.codex_command_configured and executable_available:
        return FeasibilityFacet("AVAILABLE", ("AIENT_CODEX_COMMAND is configured and Codex executable is available",))
    if executable_available:
        return FeasibilityFacet("CONDITIONAL", ("set AIENT_CODEX_COMMAND before residual execution",))
    return FeasibilityFacet("MISSING", ("Codex executable is unavailable",))


def _write_scope_feasibility(
    task: ResidualGeneratedTask,
    policy: FeasibilityPolicyProfile,
    repository_root: Path,
) -> FeasibilityFacet:
    blockers: list[str] = []
    for path in task.write_scope["allowed"]:
        if path.startswith(("/", "..")):
            blockers.append(f"write scope escapes repository: {path}")
        if not _scope_root_exists(repository_root, path):
            blockers.append(f"write scope root does not exist: {path}")
        for prohibited in (*task.write_scope["prohibited"], *policy.prohibited_path_patterns):
            if path == prohibited or path.startswith(prohibited.rstrip("*")):
                blockers.append(f"write scope overlaps prohibited path: {path}")
    if blockers:
        return FeasibilityFacet("MISSING", tuple(sorted(set(blockers))))
    return FeasibilityFacet("AVAILABLE", ("residual write scope is inside repository and outside prohibited paths",))


def _scope_root_exists(repository_root: Path, scope: str) -> bool:
    root = scope.split("**", 1)[0].rstrip("/")
    return bool(root) and (repository_root / root).exists()


def _infrastructure_feasibility(environment: EnvironmentProfile) -> FeasibilityFacet:
    missing = []
    if not environment.git_available:
        missing.append("git is unavailable")
    if not environment.postgresql_available:
        missing.append("PostgreSQL client is unavailable")
    if not environment.alembic_available:
        missing.append("Alembic is unavailable")
    if not environment.docker_available:
        missing.append("Docker is unavailable")
    if not environment.docker_compose_available:
        missing.append("Docker Compose is unavailable")
    if not environment.python_available:
        missing.append("project Python runtime is unavailable")
    if missing:
        return FeasibilityFacet("MISSING", tuple(sorted(missing)))
    return FeasibilityFacet("AVAILABLE", ("Git, PostgreSQL, Alembic, Docker, Compose, and Python are available",))


def _resource_feasibility(task: ResidualGeneratedTask, environment: EnvironmentProfile) -> FeasibilityFacet:
    if environment.ram_mb is not None and environment.ram_mb < 2048:
        return FeasibilityFacet("MISSING", ("available RAM is below 2048 MiB",))
    if task.execution["model_profile"] == "GPU_REQUIRED" and not environment.gpu_available:
        return FeasibilityFacet("MISSING", ("GPU resource is required but unavailable",))
    return FeasibilityFacet("AVAILABLE", ("declared residual task resources fit current environment profile",))


def _dependency_feasibility(task: ResidualGeneratedTask, plan: ResidualImplementationPlan) -> FeasibilityFacet:
    task_ids = {candidate.id for candidate in plan.tasks}
    missing = tuple(sorted(dependency for dependency in task.depends_on if dependency not in task_ids))
    if missing:
        return FeasibilityFacet("MISSING", tuple(f"missing dependency {dependency}" for dependency in missing))
    return FeasibilityFacet("AVAILABLE", ("residual dependency references are present",))


def _evidence_feasibility(task: ResidualGeneratedTask, repository_root: Path) -> FeasibilityFacet:
    if task.task_type != "EVIDENCE_CLOSURE_TASK":
        return FeasibilityFacet("AVAILABLE", ("not an evidence-only residual task",))
    capability = task.capabilities[0]
    source_files = EVIDENCE_SOURCE_FILES.get(capability, ())
    missing = tuple(path for path in source_files if not (repository_root / path).exists())
    if missing:
        return FeasibilityFacet(
            "MISSING",
            tuple(f"evidence-only residual lacks source proof file: {path}" for path in missing),
        )
    if "Do not implement new runtime behavior" not in task.objective:
        return FeasibilityFacet("MISSING", ("evidence-only residual objective does not prohibit new runtime behavior",))
    return FeasibilityFacet(
        "AVAILABLE",
        (
            "source implementation/evidence files exist and task is restricted to evidence closure",
            "verification can confirm evidence mapping without new runtime behavior",
        ),
    )


def _policy_decision(task: ResidualGeneratedTask, policy: FeasibilityPolicyProfile) -> PolicyDecision:
    risk = task.risk["level"]
    if risk not in policy.guarded_allowed_risks:
        return "PROHIBITED"
    if risk in policy.auto_allowed_risks:
        return "AUTO_ALLOWED"
    if risk == "HIGH":
        return "HUMAN_APPROVAL_REQUIRED"
    return "GUARDED_ALLOWED"


def _block_by_dependency(
    result: ResidualTaskFeasibility,
    blocked_dependencies: tuple[str, ...],
) -> ResidualTaskFeasibility:
    return ResidualTaskFeasibility(
        task_id=result.task_id,
        task_type=result.task_type,
        feasibility_status="BLOCKED",
        policy_decision=result.policy_decision,
        risk=result.risk,
        blockers=(*result.blockers, *(f"BLOCKED_BY_DEPENDENCY:{dependency}" for dependency in blocked_dependencies)),
        conditions=result.conditions,
        human_gate_ids=result.human_gate_ids,
        agent_feasibility=result.agent_feasibility,
        model_feasibility=result.model_feasibility,
        executor_feasibility=result.executor_feasibility,
        verification_feasibility=result.verification_feasibility,
        write_scope_feasibility=result.write_scope_feasibility,
        infrastructure_feasibility=result.infrastructure_feasibility,
        resource_feasibility=result.resource_feasibility,
        dependency_feasibility=FeasibilityFacet(
            "MISSING",
            tuple(f"dependency {dependency} is blocked" for dependency in blocked_dependencies),
        ),
        evidence_feasibility=result.evidence_feasibility,
    )


def _evidence_finding(result: ResidualTaskFeasibility) -> ResidualFeasibilityFinding:
    severity: ResidualFindingSeverity = "ERROR" if result.evidence_feasibility.status == "MISSING" else "INFO"
    return ResidualFeasibilityFinding(
        f"RFE-{result.task_id}-EVIDENCE",
        severity,
        result.task_id,
        "; ".join(result.evidence_feasibility.reasons),
    )


def _blocker_findings(task_results: tuple[ResidualTaskFeasibility, ...]) -> tuple[ResidualFeasibilityFinding, ...]:
    findings: list[ResidualFeasibilityFinding] = []
    for result in task_results:
        for blocker in result.blockers:
            findings.append(
                ResidualFeasibilityFinding(
                    f"RFE-{result.task_id}-{hashlib.sha256(blocker.encode()).hexdigest()[:8]}",
                    "ERROR",
                    result.task_id,
                    blocker,
                )
            )
        for condition in result.conditions:
            findings.append(
                ResidualFeasibilityFinding(
                    f"RFE-{result.task_id}-{hashlib.sha256(condition.encode()).hexdigest()[:8]}",
                    "WARNING",
                    result.task_id,
                    condition,
                )
            )
    return tuple(findings)


def _feasible_parallel_width(
    plan: ResidualImplementationPlan,
    task_results: tuple[ResidualTaskFeasibility, ...],
    policy: FeasibilityPolicyProfile,
) -> int:
    non_blocked = {result.task_id for result in task_results if result.feasibility_status not in {"BLOCKED", "DEFERRED"}}
    by_id = {task.id: task for task in plan.tasks}
    widths: list[int] = []
    for wave in plan.waves:
        compatible: list[str] = []
        used_scopes: set[str] = set()
        for task_id in wave["task_ids"]:
            if task_id not in non_blocked:
                continue
            scopes = set(by_id[task_id].write_scope["allowed"])
            if not scopes.intersection(used_scopes):
                compatible.append(task_id)
                used_scopes.update(scopes)
        widths.append(len(compatible))
    return min(max(widths, default=0), policy.max_parallel_width)


def _task_capabilities(task: ResidualGeneratedTask) -> tuple[str, ...]:
    return task.capabilities


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(canonical_bytes(value) + b"\n")
