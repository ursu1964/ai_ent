from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ai_ent.post_implementation import ResidualGap
from ai_ent.project_manifest import (
    GENERATED_MARKER,
    PROJECT_MANIFEST_ROOT,
    canonical_bytes,
    compile_project_manifest,
)

RESIDUAL_DAG_GENERATOR_VERSION = "rdg-001.1"

ResidualTaskType = Literal["IMPLEMENTATION_TASK", "EVIDENCE_CLOSURE_TASK", "VERIFICATION_TASK"]
ResidualFindingSeverity = Literal["ERROR", "WARNING", "INFO"]


@dataclass(frozen=True)
class ResidualGeneratedTask:
    id: str
    title: str
    objective: str
    task_type: ResidualTaskType
    gap_ids: tuple[str, ...]
    capabilities: tuple[str, ...]
    requirements: tuple[str, ...]
    components: tuple[str, ...]
    interfaces: tuple[str, ...]
    depends_on: tuple[str, ...]
    write_scope: dict[str, tuple[str, ...]]
    risk: dict[str, str]
    execution: dict[str, str]
    verification: dict[str, Any]
    provenance: dict[str, str]
    fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "objective": self.objective,
            "task_type": self.task_type,
            "gap_ids": list(self.gap_ids),
            "implements": {
                "requirements": list(self.requirements),
                "capabilities": list(self.capabilities),
                "components": list(self.components),
                "interfaces": list(self.interfaces),
            },
            "depends_on": list(self.depends_on),
            "write_scope": {
                "allowed": list(self.write_scope["allowed"]),
                "prohibited": list(self.write_scope["prohibited"]),
            },
            "risk": self.risk,
            "execution": self.execution,
            "verification": self.verification,
            "provenance": self.provenance,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class ResidualPlanFinding:
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
class ResidualImplementationPlan:
    generator_version: str
    head: str
    post_compiled_hash: str
    post_capability_hash: str
    post_trace_hash: str
    pir_residual_gap_hash: str
    residual_plan_hash: str
    tasks: tuple[ResidualGeneratedTask, ...]
    dependency_edges: tuple[tuple[str, str], ...]
    waves: tuple[dict[str, Any], ...]
    critical_path: tuple[str, ...]
    human_gates: tuple[dict[str, Any], ...]
    findings: tuple[ResidualPlanFinding, ...]

    @property
    def ok(self) -> bool:
        return not any(finding.severity == "ERROR" for finding in self.findings)

    def as_dict(self) -> dict[str, Any]:
        risk_distribution: dict[str, int] = {}
        tasks_per_gap: dict[str, int] = {}
        type_counts: dict[str, int] = {
            "IMPLEMENTATION_TASK": 0,
            "EVIDENCE_CLOSURE_TASK": 0,
            "VERIFICATION_TASK": 0,
        }
        for task in self.tasks:
            risk = task.risk["level"]
            risk_distribution[risk] = risk_distribution.get(risk, 0) + 1
            type_counts[task.task_type] += 1
            for gap_id in task.gap_ids:
                tasks_per_gap[gap_id] = tasks_per_gap.get(gap_id, 0) + 1
        return {
            "generated": GENERATED_MARKER,
            "generator_version": self.generator_version,
            "head": self.head,
            "post_compiled_hash": self.post_compiled_hash,
            "post_capability_hash": self.post_capability_hash,
            "post_trace_hash": self.post_trace_hash,
            "pir_residual_gap_hash": self.pir_residual_gap_hash,
            "residual_plan_hash": self.residual_plan_hash,
            "tasks": [task.as_dict() for task in self.tasks],
            "dependency_edges": [list(edge) for edge in self.dependency_edges],
            "waves": list(self.waves),
            "critical_path": list(self.critical_path),
            "human_gates": list(self.human_gates),
            "findings": [finding.as_dict() for finding in self.findings],
            "summary": {
                "total_residual_tasks": len(self.tasks),
                "implementation_tasks": type_counts["IMPLEMENTATION_TASK"],
                "evidence_closure_tasks": type_counts["EVIDENCE_CLOSURE_TASK"],
                "verification_tasks": type_counts["VERIFICATION_TASK"],
                "dependency_edges": len(self.dependency_edges),
                "waves": len(self.waves),
                "critical_path_length": len(self.critical_path),
                "maximum_parallel_width": max((len(wave["task_ids"]) for wave in self.waves), default=0),
                "human_gates": len(self.human_gates),
                "risk_distribution": dict(sorted(risk_distribution.items())),
                "tasks_per_residual_gap": dict(sorted(tasks_per_gap.items())),
                "error_findings": len([finding for finding in self.findings if finding.severity == "ERROR"]),
                "warning_findings": len([finding for finding in self.findings if finding.severity == "WARNING"]),
            },
        }


def generate_residual_implementation_plan(
    *,
    manifest_root: Path = PROJECT_MANIFEST_ROOT,
    pir_artifact: Path = Path("artifacts/pir-001/PIR-001.json"),
    repository_root: Path = Path("."),
) -> ResidualImplementationPlan:
    pir = _load_pir(pir_artifact)
    compilation = compile_project_manifest(manifest_root)
    residual_gaps = tuple(ResidualGap(**gap) for gap in pir["residual_gaps"])
    source_head = str(pir.get("head") or _git_rev(repository_root, "HEAD"))
    tasks = _residual_tasks(
        residual_gaps,
        pir,
        compilation.compiled.as_dict(),
        head=source_head,
    )
    dependency_edges = _dependency_edges(tasks)
    tasks = tuple(_replace_dependencies(task, tuple(sorted(dep for task_id, dep in dependency_edges if task_id == task.id))) for task in tasks)
    findings = _validate_residual_plan(tasks, dependency_edges, tuple(gap.gap_id for gap in residual_gaps), pir)
    waves = _waves(tasks, dependency_edges) if not any(finding.finding_id == "RDG-CYCLE" for finding in findings) else ()
    critical_path = _critical_path(tasks, dependency_edges) if waves else ()
    human_gates = tuple(_human_gates(tasks))
    payload = {
        "generator_version": RESIDUAL_DAG_GENERATOR_VERSION,
        "head": source_head,
        "post_compiled_hash": pir["hashes"]["post_compiled_hash"],
        "post_capability_hash": pir["hashes"]["post_capability_resolution_hash"],
        "post_trace_hash": pir["hashes"]["post_trace_validation_hash"],
        "pir_residual_gap_hash": pir["hashes"]["residual_gap_hash"],
        "tasks": [task.as_dict() for task in tasks],
        "dependency_edges": [list(edge) for edge in dependency_edges],
        "waves": list(waves),
        "critical_path": list(critical_path),
        "human_gates": list(human_gates),
        "findings": [finding.as_dict() for finding in findings],
    }
    residual_plan_hash = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    return ResidualImplementationPlan(
        generator_version=RESIDUAL_DAG_GENERATOR_VERSION,
        head=source_head,
        post_compiled_hash=str(pir["hashes"]["post_compiled_hash"]),
        post_capability_hash=str(pir["hashes"]["post_capability_resolution_hash"]),
        post_trace_hash=str(pir["hashes"]["post_trace_validation_hash"]),
        pir_residual_gap_hash=str(pir["hashes"]["residual_gap_hash"]),
        residual_plan_hash=residual_plan_hash,
        tasks=tasks,
        dependency_edges=dependency_edges,
        waves=waves,
        critical_path=critical_path,
        human_gates=human_gates,
        findings=findings,
    )


def write_residual_implementation_plan(
    *,
    manifest_root: Path = PROJECT_MANIFEST_ROOT,
    pir_artifact: Path = Path("artifacts/pir-001/PIR-001.json"),
    output_dir: Path = Path(".build/compiled"),
    repository_root: Path = Path("."),
) -> ResidualImplementationPlan:
    plan = generate_residual_implementation_plan(
        manifest_root=manifest_root,
        pir_artifact=pir_artifact,
        repository_root=repository_root,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    task_dir = output_dir / "residual-generated-tasks"
    task_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "residual-implementation-plan.json", plan.as_dict())
    _write_json(
        output_dir / "residual-dag.json",
        {
            "generated": GENERATED_MARKER,
            "residual_plan_hash": plan.residual_plan_hash,
            "tasks": [task.id for task in plan.tasks],
            "dependency_edges": [list(edge) for edge in plan.dependency_edges],
        },
    )
    _write_json(
        output_dir / "residual-waves.json",
        {
            "generated": GENERATED_MARKER,
            "residual_plan_hash": plan.residual_plan_hash,
            "waves": list(plan.waves),
        },
    )
    _write_json(
        output_dir / "residual-human-gates.json",
        {
            "generated": GENERATED_MARKER,
            "residual_plan_hash": plan.residual_plan_hash,
            "human_gates": list(plan.human_gates),
        },
    )
    _write_json(
        output_dir / "residual-task-findings.json",
        {
            "generated": GENERATED_MARKER,
            "residual_plan_hash": plan.residual_plan_hash,
            "findings": [finding.as_dict() for finding in plan.findings],
        },
    )
    for task in plan.tasks:
        _write_json(task_dir / f"{task.id}.json", task.as_dict())
    return plan


def _residual_tasks(
    residual_gaps: tuple[ResidualGap, ...],
    pir: dict[str, Any],
    compiled: dict[str, Any],
    *,
    head: str,
) -> tuple[ResidualGeneratedTask, ...]:
    capability_names = {str(capability["id"]): str(capability["name"]) for capability in compiled["capabilities"]}
    tasks: list[ResidualGeneratedTask] = []
    for gap in sorted(residual_gaps, key=lambda item: item.gap_id):
        capability_id = gap.capability_ids[0]
        capability_name = capability_names.get(capability_id, capability_id)
        if gap.gap_type == "IMPLEMENTATION_GAP" and capability_id == "C08":
            tasks.extend(_c08_tasks(gap, capability_name, pir, head))
        elif gap.gap_type == "EVIDENCE_GAP":
            tasks.append(_evidence_closure_task(gap, capability_name, pir, head))
        else:
            tasks.append(_verification_task(gap, capability_name, pir, head))
    return tuple(sorted(tasks, key=lambda item: item.id))


def _c08_tasks(gap: ResidualGap, capability_name: str, pir: dict[str, Any], head: str) -> tuple[ResidualGeneratedTask, ...]:
    contract = _make_task(
        task_id="RES-C08-CONTRACT",
        title="Define C08 governance evolution contract",
        objective=(
            "Define the bounded governance evolution and intelligence contract required by C08, "
            "without granting autonomous policy weakening or runtime execution authority."
        ),
        task_type="IMPLEMENTATION_TASK",
        gap=gap,
        capability_name=capability_name,
        pir=pir,
        head=head,
        risk_level="HIGH",
        risk_reason="C08 governs future policy/evolution behavior and requires explicit bounded authority.",
        agent_role="AGT-004",
        model_profile="ARCHITECTURE_REASONING",
        verification_profile="FULL_REGRESSION",
        acceptance=(
            "C08 contract has deterministic policy/evolution boundaries.",
            "Contract cannot approve gates, bypass verifier/commit boundaries, or self-schedule execution.",
            "Tests prove invalid or authority-expanding governance requests are rejected.",
        ),
        allowed=("src/ai_ent/**", "tests/**", "manifest/project/ai-ent/**"),
    )
    service = _make_task(
        task_id="RES-C08-SERVICE",
        title="Implement bounded C08 governance evolution service",
        objective=(
            "Implement the C08 service behavior behind the approved contract, preserving existing runtime, "
            "verification, human-gate, and Git authority separation."
        ),
        task_type="IMPLEMENTATION_TASK",
        gap=gap,
        capability_name=capability_name,
        pir=pir,
        head=head,
        risk_level="HIGH",
        risk_reason="C08 service changes governance intelligence behavior.",
        agent_role="AGT-003",
        model_profile="CODING_HIGH",
        verification_profile="FULL_REGRESSION",
        acceptance=(
            "Service decisions are deterministic and bounded by manifest policy.",
            "Service emits recommendations/evidence only and does not mutate runtime completion authority.",
            "Security and regression tests cover prohibited authority escalation.",
        ),
        allowed=("src/ai_ent/**", "tests/**"),
    )
    verification = _make_task(
        task_id="RES-C08-VERIFICATION",
        title="Add C08 governance verification evidence",
        objective="Add deterministic verification coverage and manifest evidence proving C08 is implemented within its bounded contract.",
        task_type="VERIFICATION_TASK",
        gap=gap,
        capability_name=capability_name,
        pir=pir,
        head=head,
        risk_level="MEDIUM",
        risk_reason="Verification evidence changes capability satisfaction claims but not runtime authority.",
        agent_role="AGT-004",
        model_profile="SECURITY_REVIEW",
        verification_profile="FULL_REGRESSION",
        acceptance=(
            "C08 tests verify deterministic governance outcomes and authority separation.",
            "Capability evidence references accepted source, tests, and task completion provenance.",
            "PIR residual gap for C08 can be closed by evidence without unreviewed authority expansion.",
        ),
        allowed=("src/ai_ent/**", "tests/**", "manifest/project/ai-ent/**"),
    )
    return (contract, service, verification)


def _evidence_closure_task(
    gap: ResidualGap,
    capability_name: str,
    pir: dict[str, Any],
    head: str,
) -> ResidualGeneratedTask:
    capability_id = gap.capability_ids[0]
    risk = "MEDIUM" if capability_id in {"C06", "C07", "C09"} else "LOW"
    return _make_task(
        task_id=f"RES-{capability_id}-EVIDENCE",
        title=f"Close {capability_id} evidence for {capability_name}",
        objective=(
            f"Close the {gap.gap_id} evidence gap by linking existing implementation, tests, and accepted runtime evidence. "
            "Do not implement new runtime behavior unless the evidence audit proves behavior is actually missing."
        ),
        task_type="EVIDENCE_CLOSURE_TASK",
        gap=gap,
        capability_name=capability_name,
        pir=pir,
        head=head,
        risk_level=risk,
        risk_reason="Evidence closure updates traceability; no new executor/runtime authority is authorized.",
        agent_role="AGT-004",
        model_profile="SECURITY_REVIEW" if capability_id in {"C06", "C07", "C09"} else "CODING_STANDARD",
        verification_profile="STANDARD_REGRESSION",
        acceptance=(
            f"{gap.gap_id} is mapped to concrete accepted source, tests, or runtime evidence.",
            "Evidence Graph remains non-authoritative and only indexes provenance.",
            "No already-closed capability work is regenerated as implementation work.",
        ),
        allowed=("manifest/project/ai-ent/**", "src/ai_ent/**", "tests/**"),
    )


def _verification_task(
    gap: ResidualGap,
    capability_name: str,
    pir: dict[str, Any],
    head: str,
) -> ResidualGeneratedTask:
    capability_id = gap.capability_ids[0]
    return _make_task(
        task_id=f"RES-{capability_id}-VERIFY",
        title=f"Verify residual closure for {capability_name}",
        objective=f"Add deterministic verification needed to close {gap.gap_id}.",
        task_type="VERIFICATION_TASK",
        gap=gap,
        capability_name=capability_name,
        pir=pir,
        head=head,
        risk_level="MEDIUM",
        risk_reason="Verification closure affects evidence confidence only.",
        agent_role="AGT-004",
        model_profile="SECURITY_REVIEW",
        verification_profile="FULL_REGRESSION",
        acceptance=(f"{gap.gap_id} has machine-checkable verification.",),
        allowed=("src/ai_ent/**", "tests/**", "manifest/project/ai-ent/**"),
    )


def _make_task(
    *,
    task_id: str,
    title: str,
    objective: str,
    task_type: ResidualTaskType,
    gap: ResidualGap,
    capability_name: str,
    pir: dict[str, Any],
    head: str,
    risk_level: str,
    risk_reason: str,
    agent_role: str,
    model_profile: str,
    verification_profile: str,
    acceptance: tuple[str, ...],
    allowed: tuple[str, ...],
) -> ResidualGeneratedTask:
    provenance = {
        "source_head": head,
        "post_compiled_hash": str(pir["hashes"]["post_compiled_hash"]),
        "post_capability_hash": str(pir["hashes"]["post_capability_resolution_hash"]),
        "post_trace_hash": str(pir["hashes"]["post_trace_validation_hash"]),
        "pir_residual_gap_hash": str(pir["hashes"]["residual_gap_hash"]),
        "pir_gate_id": "PIR-001",
        "ipcg_gate_id": "IPCG-001",
        "generator_version": RESIDUAL_DAG_GENERATOR_VERSION,
    }
    material = {
        "id": task_id,
        "task_type": task_type,
        "gap_ids": (gap.gap_id,),
        "gap": gap.as_dict(),
        "capabilities": gap.capability_ids,
        "requirements": gap.requirement_ids,
        "components": gap.component_interface_ids,
        "objective": objective,
        "risk": risk_level,
        "acceptance": acceptance,
        "provenance": provenance,
    }
    return ResidualGeneratedTask(
        id=task_id,
        title=title,
        objective=objective,
        task_type=task_type,
        gap_ids=(gap.gap_id,),
        capabilities=gap.capability_ids,
        requirements=gap.requirement_ids,
        components=gap.component_interface_ids,
        interfaces=(),
        depends_on=(),
        write_scope={
            "allowed": allowed,
            "prohibited": (".env", ".env.*", "aient/**", ".bootstrap/**", ".build/**"),
        },
        risk={"level": risk_level, "reason": risk_reason},
        execution={"agent_role": agent_role, "model_profile": model_profile, "executor": "codex"},
        verification={"profile": verification_profile, "acceptance_criteria": acceptance},
        provenance=provenance,
        fingerprint=hashlib.sha256(canonical_bytes(material)).hexdigest(),
    )


def _dependency_edges(tasks: tuple[ResidualGeneratedTask, ...]) -> tuple[tuple[str, str], ...]:
    by_id = {task.id: task for task in tasks}
    edges = {
        ("RES-C06-EVIDENCE", "RES-C05-EVIDENCE"),
        ("RES-C07-EVIDENCE", "RES-C06-EVIDENCE"),
        ("RES-C08-CONTRACT", "RES-C07-EVIDENCE"),
        ("RES-C08-SERVICE", "RES-C08-CONTRACT"),
        ("RES-C08-VERIFICATION", "RES-C08-SERVICE"),
        ("RES-C09-EVIDENCE", "RES-C07-EVIDENCE"),
    }
    return tuple(sorted(edge for edge in edges if edge[0] in by_id and edge[1] in by_id))


def _replace_dependencies(task: ResidualGeneratedTask, depends_on: tuple[str, ...]) -> ResidualGeneratedTask:
    return ResidualGeneratedTask(
        id=task.id,
        title=task.title,
        objective=task.objective,
        task_type=task.task_type,
        gap_ids=task.gap_ids,
        capabilities=task.capabilities,
        requirements=task.requirements,
        components=task.components,
        interfaces=task.interfaces,
        depends_on=depends_on,
        write_scope=task.write_scope,
        risk=task.risk,
        execution=task.execution,
        verification=task.verification,
        provenance=task.provenance,
        fingerprint=task.fingerprint,
    )


def _validate_residual_plan(
    tasks: tuple[ResidualGeneratedTask, ...],
    edges: tuple[tuple[str, str], ...],
    residual_gap_ids: tuple[str, ...],
    pir: dict[str, Any],
) -> tuple[ResidualPlanFinding, ...]:
    findings: list[ResidualPlanFinding] = []
    task_ids = {task.id for task in tasks}
    duplicate_ids = sorted({task.id for task in tasks if [candidate.id for candidate in tasks].count(task.id) > 1})
    for task_id in duplicate_ids:
        findings.append(ResidualPlanFinding(f"RDG-{task_id}-DUPLICATE", "ERROR", task_id, "duplicate residual task id"))
    for task_id, dependency_id in edges:
        if task_id == dependency_id:
            findings.append(ResidualPlanFinding(f"RDG-{task_id}-SELF", "ERROR", task_id, "task depends on itself"))
        if task_id not in task_ids or dependency_id not in task_ids:
            findings.append(ResidualPlanFinding(f"RDG-{task_id}-{dependency_id}-MISSING", "ERROR", task_id, "dependency references missing task"))
    covered_gap_ids = {gap_id for task in tasks for gap_id in task.gap_ids}
    for gap_id in residual_gap_ids:
        if gap_id not in covered_gap_ids:
            findings.append(ResidualPlanFinding(f"RDG-{gap_id}-UNCOVERED", "ERROR", gap_id, "PIR residual gap has no residual task"))
    for task in tasks:
        if not task.gap_ids or not task.capabilities:
            findings.append(ResidualPlanFinding(f"RDG-{task.id}-ORPHAN", "ERROR", task.id, "residual task lacks gap/capability justification"))
        if task.task_type == "EVIDENCE_CLOSURE_TASK" and task.id == "RES-C08-EVIDENCE":
            findings.append(ResidualPlanFinding(f"RDG-{task.id}-WRONG-TYPE", "ERROR", task.id, "C08 implementation gap cannot be evidence-only"))
        if not task.verification["acceptance_criteria"]:
            findings.append(ResidualPlanFinding(f"RDG-{task.id}-NO-VERIFY", "ERROR", task.id, "residual task lacks acceptance criteria"))
    closed = {
        item["capability_id"]
        for item in pir.get("previous_gap_evaluation", [])
        if item.get("state") == "CLOSED"
    }
    regenerated_closed = sorted(
        capability_id
        for task in tasks
        for capability_id in task.capabilities
        if capability_id in closed and capability_id not in {"C05", "C06", "C07", "C08", "C09"}
    )
    for capability_id in regenerated_closed:
        findings.append(ResidualPlanFinding(f"RDG-{capability_id}-REGENERATED", "ERROR", capability_id, "closed capability regenerated as residual work"))
    cycle = _cycle({task.id: set(task.depends_on) for task in tasks})
    if cycle:
        findings.append(ResidualPlanFinding("RDG-CYCLE", "ERROR", "residual-dag", f"cycle: {' -> '.join(cycle)}"))
    return tuple(sorted(findings, key=lambda item: item.finding_id))


def _waves(
    tasks: tuple[ResidualGeneratedTask, ...],
    edges: tuple[tuple[str, str], ...],
) -> tuple[dict[str, Any], ...]:
    remaining = {task.id for task in tasks}
    dependencies = {task.id: {dependency for task_id, dependency in edges if task_id == task.id} for task in tasks}
    completed: set[str] = set()
    waves: list[dict[str, Any]] = []
    while remaining:
        ready = sorted(task_id for task_id in remaining if dependencies[task_id].issubset(completed))
        if not ready:
            raise ValueError("residual DAG contains a cycle")
        waves.append({"id": f"RES-WAVE-{len(waves) + 1:03d}", "task_ids": ready})
        completed.update(ready)
        remaining.difference_update(ready)
    return tuple(waves)


def _critical_path(
    tasks: tuple[ResidualGeneratedTask, ...],
    edges: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    dependencies = {task.id: tuple(sorted(dependency for task_id, dependency in edges if task_id == task.id)) for task in tasks}
    memo: dict[str, tuple[str, ...]] = {}

    def path_to(task_id: str) -> tuple[str, ...]:
        if task_id in memo:
            return memo[task_id]
        candidates = [path_to(dependency) for dependency in dependencies.get(task_id, ())]
        prefix = max(candidates, key=lambda path: (len(path), path), default=())
        memo[task_id] = (*prefix, task_id)
        return memo[task_id]

    return max((path_to(task.id) for task in tasks), key=lambda path: (len(path), path), default=())


def _human_gates(tasks: tuple[ResidualGeneratedTask, ...]) -> list[dict[str, Any]]:
    return [
        {
            "id": f"GATE-{task.id}",
            "task_id": task.id,
            "reason": task.risk["reason"],
            "required_before": "execution",
        }
        for task in tasks
        if task.risk["level"] == "HIGH"
    ]


def _cycle(dependencies: dict[str, set[str]]) -> list[str]:
    visiting: set[str] = set()
    visited: set[str] = set()
    path: list[str] = []

    def visit(task_id: str) -> list[str]:
        if task_id in visiting:
            return path[path.index(task_id) :] + [task_id]
        if task_id in visited:
            return []
        visiting.add(task_id)
        path.append(task_id)
        for dependency_id in sorted(dependencies.get(task_id, set())):
            found = visit(dependency_id)
            if found:
                return found
        path.pop()
        visiting.remove(task_id)
        visited.add(task_id)
        return []

    for task_id in sorted(dependencies):
        found = visit(task_id)
        if found:
            return found
    return []


def _load_pir(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("gate_id") != "PIR-001":
        raise ValueError(f"{path} is not a PIR-001 artifact")
    if payload.get("result") != "RESIDUAL_GAPS_REQUIRE_NEW_DAG":
        raise ValueError("PIR-001 did not request residual DAG generation")
    return payload


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


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(canonical_bytes(value) + b"\n")
