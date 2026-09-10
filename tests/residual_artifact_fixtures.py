from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any

from ai_ent.project_manifest import (
    GENERATED_MARKER,
    EnvironmentProfile,
    canonical_bytes,
    compile_project_manifest,
)
from ai_ent.residual_dag import generate_residual_implementation_plan
from ai_ent.residual_dry_run import dry_run_residual_plan
from ai_ent.residual_feasibility import (
    default_residual_feasibility_policy,
    evaluate_residual_plan_feasibility,
)
from ai_ent.residual_plan_acceptance import accept_residual_plan

MANIFEST_ROOT = Path("manifest/project/ai-ent")


def residual_test_environment(*, codex_configured: bool = False) -> EnvironmentProfile:
    return EnvironmentProfile(
        profile_id="test",
        repository_path=str(Path.cwd()),
        git_available=True,
        postgresql_available=True,
        alembic_available=True,
        docker_available=True,
        docker_compose_available=True,
        codex_command_configured=codex_configured,
        codex_executable_available=True,
        python_path=sys.executable,
        python_available=True,
        ram_mb=8192,
        gpu_available=False,
        external_network="available",
        configured_secret_names=(),
    )


def write_test_pir_artifact(tmp_path: Path) -> Path:
    compilation = compile_project_manifest(MANIFEST_ROOT)
    compiled = compilation.compiled.as_dict()
    residual_gaps = _residual_gaps(compiled)
    residual_gap_hash = hashlib.sha256(canonical_bytes(residual_gaps)).hexdigest()
    artifact = {
        "generated": GENERATED_MARKER,
        "gate_id": "PIR-001",
        "result": "RESIDUAL_GAPS_REQUIRE_NEW_DAG",
        "head": _git_rev("HEAD"),
        "head_tree": _git_rev("HEAD^{tree}"),
        "plan_id": "PLAN-test-residual",
        "plan_version": "1",
        "evaluator_version": "pir-test-fixture",
        "hashes": {
            "pre_compiled_hash": compilation.lock.compiled_hash,
            "post_compiled_hash": compilation.lock.compiled_hash,
            "pre_capability_resolution_hash": "0" * 64,
            "post_capability_resolution_hash": hashlib.sha256(canonical_bytes(compiled["capabilities"])).hexdigest(),
            "pre_trace_validation_hash": "1" * 64,
            "post_trace_validation_hash": hashlib.sha256(canonical_bytes(compiled["requirements"])).hexdigest(),
            "residual_gap_hash": residual_gap_hash,
        },
        "runtime_summary": {},
        "capability_matrix": [],
        "requirement_coverage": {},
        "previous_gap_evaluation": [
            {
                "capability_id": "C20",
                "state": "CLOSED",
                "evidence": ["BEAG-001"],
                "remaining_missing_evidence": [],
            }
        ],
        "residual_gaps": residual_gaps,
        "manifest_material_differences": [],
        "evidence_authority": {
            "artifact_evidence_graph": "NON_AUTHORITATIVE provenance/index only",
        },
        "limitations": [],
    }
    path = tmp_path / "pir-001" / "PIR-001.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(artifact) + b"\n")
    return path


def write_test_rpg_artifact(tmp_path: Path, pir_artifact: Path) -> Path:
    plan = generate_residual_implementation_plan(pir_artifact=pir_artifact)
    feasibility = evaluate_residual_plan_feasibility(
        plan,
        residual_test_environment(codex_configured=False),
        default_residual_feasibility_policy(plan),
    )
    dry_run = dry_run_residual_plan(
        plan=plan,
        feasibility=feasibility,
        pir_artifact=pir_artifact,
        expected_residual_plan_hash=plan.residual_plan_hash,
    )
    gate = accept_residual_plan(
        plan=plan,
        feasibility=feasibility,
        dry_run=dry_run,
        expected_residual_plan_hash=plan.residual_plan_hash,
    )
    feasibility_path = residual_feasibility_artifact_path(tmp_path)
    feasibility_path.parent.mkdir(parents=True, exist_ok=True)
    feasibility_path.write_bytes(canonical_bytes(feasibility.as_dict()) + b"\n")
    path = tmp_path / "rpg-001" / "RPG-001.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(gate.as_dict()) + b"\n")
    return path


def residual_feasibility_artifact_path(tmp_path: Path) -> Path:
    return tmp_path / "compiled" / "residual-feasibility-report.json"


def _residual_gaps(compiled: dict[str, Any]) -> list[dict[str, Any]]:
    requirements = _requirements_by_capability(compiled["requirements"])
    components = _components_by_capability(compiled["architecture"].get("components", []))
    evidence_by_capability = {
        str(capability["id"]): tuple(str(item) for item in capability.get("evidence_refs", []))
        for capability in compiled["capabilities"]
    }
    gaps: list[dict[str, Any]] = []
    for capability_id in ("C05", "C06", "C07", "C09"):
        gaps.append(
            _gap(
                capability_id,
                "EVIDENCE_GAP",
                requirements=requirements,
                components=components,
                current_evidence=evidence_by_capability.get(capability_id, ()),
                missing=f"{capability_id}:complete-capability-contract-evidence",
            )
        )
    gaps.append(
        _gap(
            "C08",
            "IMPLEMENTATION_GAP",
            requirements=requirements,
            components=components,
            current_evidence=(),
            missing="C08:accepted-implementation-evidence",
        )
    )
    return sorted(gaps, key=lambda item: str(item["gap_id"]))


def _gap(
    capability_id: str,
    gap_type: str,
    *,
    requirements: dict[str, tuple[str, ...]],
    components: dict[str, tuple[str, ...]],
    current_evidence: tuple[str, ...],
    missing: str,
) -> dict[str, Any]:
    return {
        "gap_id": f"PIR-GAP-{capability_id}",
        "gap_type": gap_type,
        "requirement_ids": list(requirements.get(capability_id, ())),
        "capability_ids": [capability_id],
        "component_interface_ids": list(components.get(capability_id, ())),
        "current_evidence": list(current_evidence),
        "missing_behavior_or_evidence": missing,
        "verification_needed": f"capability-specific verification for {capability_id}",
        "severity": "HIGH" if gap_type == "IMPLEMENTATION_GAP" else "MEDIUM",
        "current_release_relevance": "CURRENT_RELEASE",
    }


def _requirements_by_capability(requirements: list[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    result: dict[str, set[str]] = {}
    for requirement in requirements:
        if requirement.get("classification") == "FUTURE/DEFERRED":
            continue
        for capability_id in requirement.get("capabilities", []):
            result.setdefault(str(capability_id), set()).add(str(requirement["id"]))
    return {
        capability_id: tuple(sorted(requirement_ids))
        for capability_id, requirement_ids in result.items()
    }


def _components_by_capability(components: list[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    result: dict[str, set[str]] = {}
    for component in components:
        for capability_id in component.get("capabilities", []):
            result.setdefault(str(capability_id), set()).add(str(component["id"]))
    return {
        capability_id: tuple(sorted(component_ids))
        for capability_id, component_ids in result.items()
    }


def _git_rev(rev: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", rev],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "UNKNOWN"
