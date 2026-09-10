from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ai_ent.project_manifest import EnvironmentProfile
from ai_ent.residual_dag import generate_residual_implementation_plan
from ai_ent.residual_dry_run import dry_run_evaluated_residual_plan
from ai_ent.residual_feasibility import (
    default_residual_feasibility_policy,
    evaluate_residual_plan_feasibility,
)
from ai_ent.residual_plan_acceptance import (
    RPG_ACCEPTANCE_VERSION,
    accept_residual_plan,
    write_residual_plan_acceptance,
)


def environment(*, codex_configured: bool = False) -> EnvironmentProfile:
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
        python_path=str(Path("aient/bin/python")),
        python_available=True,
        ram_mb=8192,
        gpu_available=False,
        external_network="available",
        configured_secret_names=(),
    )


def test_residual_plan_acceptance_accepts_with_prerequisites() -> None:
    plan = generate_residual_implementation_plan()
    feasibility = evaluate_residual_plan_feasibility(
        plan,
        environment(codex_configured=False),
        default_residual_feasibility_policy(plan),
    )
    dry_run = dry_run_evaluated_residual_plan(
        plan,
        feasibility,
        expected_residual_plan_hash=plan.residual_plan_hash,
    )
    gate = accept_residual_plan(
        plan=plan,
        feasibility=feasibility,
        dry_run=dry_run,
        expected_residual_plan_hash=plan.residual_plan_hash,
    )

    assert gate.gate_version == RPG_ACCEPTANCE_VERSION
    assert gate.gate_result == "ACCEPTED_WITH_PREREQUISITES"
    assert gate.recommendation == "READY_FOR_RESIDUAL_RUNTIME_IMPORT_WITH_EXECUTION_PREREQUISITES"
    assert gate.counts["tasks"] == 7
    assert gate.counts["dependency_edges"] == 6
    assert gate.counts["human_gates"] == 2
    assert gate.counts["technical_blockers"] == 0
    assert gate.risk_distribution == {"HIGH": 2, "LOW": 1, "MEDIUM": 4}
    assert not gate.defects
    assert "AIENT_CODEX_COMMAND must be configured before runtime import/execution" in gate.execution_prerequisites


def test_residual_plan_acceptance_accepts_without_prerequisites_when_codex_configured() -> None:
    plan = generate_residual_implementation_plan()
    feasibility = evaluate_residual_plan_feasibility(
        plan,
        environment(codex_configured=True),
        default_residual_feasibility_policy(plan),
    )
    dry_run = dry_run_evaluated_residual_plan(
        plan,
        feasibility,
        expected_residual_plan_hash=plan.residual_plan_hash,
    )
    gate = accept_residual_plan(
        plan=plan,
        feasibility=feasibility,
        dry_run=dry_run,
        expected_residual_plan_hash=plan.residual_plan_hash,
    )

    assert gate.gate_result == "ACCEPTED"
    assert gate.recommendation == "READY_FOR_RESIDUAL_RUNTIME_IMPORT"
    assert gate.execution_prerequisites == ()


def test_residual_plan_acceptance_rejects_hash_drift() -> None:
    plan = generate_residual_implementation_plan()
    feasibility = evaluate_residual_plan_feasibility(
        plan,
        environment(),
        default_residual_feasibility_policy(plan),
    )
    dry_run = dry_run_evaluated_residual_plan(plan, feasibility, expected_residual_plan_hash="0" * 64)
    gate = accept_residual_plan(
        plan=plan,
        feasibility=feasibility,
        dry_run=dry_run,
        expected_residual_plan_hash="0" * 64,
    )

    assert gate.gate_result == "REJECTED"
    assert "expected_residual_plan_hash" in gate.defects
    assert gate.recommendation == "REMEDIATION_REQUIRED"


def test_residual_plan_acceptance_rejects_fingerprint_mismatch() -> None:
    plan = generate_residual_implementation_plan()
    changed_task = replace(plan.tasks[0], fingerprint="changed")
    changed_plan = replace(plan, tasks=(changed_task, *plan.tasks[1:]))
    feasibility = evaluate_residual_plan_feasibility(
        plan,
        environment(),
        default_residual_feasibility_policy(plan),
    )
    dry_run = dry_run_evaluated_residual_plan(
        changed_plan,
        evaluate_residual_plan_feasibility(
            changed_plan,
            environment(),
            default_residual_feasibility_policy(changed_plan),
        ),
    )
    gate = accept_residual_plan(
        plan=plan,
        feasibility=feasibility,
        dry_run=dry_run,
        expected_residual_plan_hash=plan.residual_plan_hash,
    )

    assert gate.gate_result == "REJECTED"
    assert "task_fingerprints_match_lock" in gate.defects
    assert "task_fingerprints_match_preview" in gate.defects


def test_write_residual_plan_acceptance_artifacts(tmp_path: Path) -> None:
    plan = generate_residual_implementation_plan()
    gate = write_residual_plan_acceptance(
        artifacts_dir=tmp_path / "rpg-001",
        expected_residual_plan_hash=plan.residual_plan_hash,
    )

    assert gate.ok
    assert (tmp_path / "rpg-001" / "RPG-001.json").exists()
    assert (tmp_path / "rpg-001" / "RPG-001.md").exists()
