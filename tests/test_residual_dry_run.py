from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

from ai_ent.project_manifest import EnvironmentProfile
from ai_ent.residual_dag import generate_residual_implementation_plan
from ai_ent.residual_dry_run import (
    RESIDUAL_DRY_RUNNER_VERSION,
    dry_run_evaluated_residual_plan,
    dry_run_residual_plan,
    freeze_residual_plan,
    residual_plan_invalidation_rules,
)
from ai_ent.residual_feasibility import (
    default_residual_feasibility_policy,
    evaluate_residual_plan_feasibility,
)
from tests.residual_artifact_fixtures import write_test_pir_artifact


def environment(*, codex_configured: bool = True, docker_available: bool = True) -> EnvironmentProfile:
    return EnvironmentProfile(
        profile_id="test",
        repository_path=str(Path.cwd()),
        git_available=True,
        postgresql_available=True,
        alembic_available=True,
        docker_available=docker_available,
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


def test_residual_dry_run_simulates_exact_plan_shape(tmp_path: Path) -> None:
    dry_run = dry_run_residual_plan(feasibility=None, pir_artifact=write_test_pir_artifact(tmp_path))

    assert dry_run.dry_runner_version == RESIDUAL_DRY_RUNNER_VERSION
    assert dry_run.task_count == 7
    assert dry_run.implementation_task_count == 2
    assert dry_run.evidence_task_count == 4
    assert dry_run.verification_task_count == 1
    assert dry_run.dependency_edge_count == 6
    assert dry_run.wave_count == 6
    assert dry_run.theoretical_parallel_width == 2
    assert dry_run.effective_parallel_width == 1
    assert len(dry_run.lock.human_gate_definitions) == 2


def test_residual_dry_run_preserves_task_identity_and_fingerprints(tmp_path: Path) -> None:
    pir_artifact = write_test_pir_artifact(tmp_path)
    plan = generate_residual_implementation_plan(pir_artifact=pir_artifact)
    dry_run = dry_run_residual_plan(
        plan=plan,
        pir_artifact=pir_artifact,
        expected_residual_plan_hash=plan.residual_plan_hash,
    )
    preview_by_id = {preview.task_id: preview for preview in dry_run.import_preview}

    assert set(preview_by_id) == {task.id for task in plan.tasks}
    assert dry_run.lock.task_fingerprints == {task.id: task.fingerprint for task in plan.tasks}
    assert preview_by_id["RES-C08-CONTRACT"].execution_class == "residual_implementation"
    assert preview_by_id["RES-C08-CONTRACT"].human_gate_ids == ("GATE-RES-C08-CONTRACT",)
    assert preview_by_id["RES-C05-EVIDENCE"].residual_gap_refs == ("PIR-GAP-C05",)


def test_residual_dry_run_keeps_evidence_tasks_as_evidence_closure(tmp_path: Path) -> None:
    dry_run = dry_run_residual_plan(pir_artifact=write_test_pir_artifact(tmp_path))

    assert {item["task_id"] for item in dry_run.evidence_semantics} == {
        "RES-C05-EVIDENCE",
        "RES-C06-EVIDENCE",
        "RES-C07-EVIDENCE",
        "RES-C09-EVIDENCE",
    }
    assert all("do not rebuild existing runtime behavior" in item["behavior"] for item in dry_run.evidence_semantics)


def test_c08_verification_cannot_run_before_implementation(tmp_path: Path) -> None:
    pir_artifact = write_test_pir_artifact(tmp_path)
    plan = generate_residual_implementation_plan(pir_artifact=pir_artifact)
    dry_run = dry_run_residual_plan(plan=plan, pir_artifact=pir_artifact)
    dependencies = {task.id: task.depends_on for task in plan.tasks}

    assert dry_run.c08_sequence == ("RES-C08-CONTRACT", "RES-C08-SERVICE", "RES-C08-VERIFICATION")
    assert dependencies["RES-C08-SERVICE"] == ("RES-C08-CONTRACT",)
    assert dependencies["RES-C08-VERIFICATION"] == ("RES-C08-SERVICE",)
    assert dry_run.ok


def test_gated_c08_task_cannot_bypass_approval(tmp_path: Path) -> None:
    dry_run = dry_run_residual_plan(pir_artifact=write_test_pir_artifact(tmp_path))
    gated_batches = [batch for batch in dry_run.execution_batches if batch.required_human_gates]

    assert [batch.required_human_gates for batch in gated_batches] == [
        ("GATE-RES-C08-CONTRACT",),
        ("GATE-RES-C08-SERVICE",),
    ]
    assert gated_batches[0].task_ids == ("RES-C08-CONTRACT",)
    assert gated_batches[1].task_ids == ("RES-C08-SERVICE",)


def test_missing_codex_command_is_execution_prerequisite_not_error(tmp_path: Path) -> None:
    plan = generate_residual_implementation_plan(pir_artifact=write_test_pir_artifact(tmp_path))
    evaluation = evaluate_residual_plan_feasibility(
        plan,
        environment(codex_configured=False),
        default_residual_feasibility_policy(plan),
    )
    dry_run = dry_run_evaluated_residual_plan(plan, evaluation)

    assert dry_run.ok
    assert dry_run.frozen_plan_state == "FROZEN"
    assert dry_run.plan_acceptance_state == "READY_WITH_EXECUTION_PREREQUISITES"
    assert dry_run.execution_prerequisites == ("AIENT_CODEX_COMMAND must be configured before runtime import/execution",)
    assert not [finding for finding in dry_run.findings if finding.severity == "ERROR"]


def test_missing_technical_dependency_blocks_freeze(tmp_path: Path) -> None:
    pir_artifact = write_test_pir_artifact(tmp_path)
    plan = generate_residual_implementation_plan(pir_artifact=pir_artifact)
    evaluation = evaluate_residual_plan_feasibility(
        plan,
        environment(docker_available=False),
        default_residual_feasibility_policy(plan),
    )
    dry_run = dry_run_evaluated_residual_plan(plan, evaluation)

    assert dry_run.plan_acceptance_state == "BLOCKED"
    assert dry_run.frozen_plan_state == "INVALIDATED"
    assert any(finding.severity == "ERROR" for finding in dry_run.findings)
    try:
        freeze_residual_plan(
            output_dir=tmp_path / "compiled",
            pir_artifact=pir_artifact,
            expected_residual_plan_hash="0" * 64,
        )
    except ValueError as exc:
        assert "cannot be frozen" in str(exc)
    else:
        raise AssertionError("freeze_residual_plan should reject ERROR findings")


def test_expected_residual_plan_hash_mismatch_invalidates_plan(tmp_path: Path) -> None:
    pir_artifact = write_test_pir_artifact(tmp_path)
    plan = generate_residual_implementation_plan(pir_artifact=pir_artifact)
    dry_run = dry_run_residual_plan(plan=plan, pir_artifact=pir_artifact, expected_residual_plan_hash="0" * 64)

    assert not dry_run.ok
    assert dry_run.frozen_plan_state == "INVALIDATED"
    assert any(finding.finding_id == "RDF-EXPECTED-RESIDUAL-PLAN-HASH" for finding in dry_run.findings)


def test_task_fingerprint_change_changes_residual_dry_run_hash(tmp_path: Path) -> None:
    plan = generate_residual_implementation_plan(pir_artifact=write_test_pir_artifact(tmp_path))
    evaluation = evaluate_residual_plan_feasibility(plan, environment(), default_residual_feasibility_policy(plan))
    original = dry_run_evaluated_residual_plan(plan, evaluation)
    changed_task = replace(plan.tasks[0], fingerprint="changed")
    changed_plan = replace(plan, tasks=(changed_task, *plan.tasks[1:]))
    changed_evaluation = evaluate_residual_plan_feasibility(
        changed_plan,
        environment(),
        default_residual_feasibility_policy(changed_plan),
    )
    changed = dry_run_evaluated_residual_plan(changed_plan, changed_evaluation)

    assert original.residual_dry_run_hash != changed.residual_dry_run_hash


def test_residual_dry_run_and_freeze_are_byte_identical(tmp_path: Path) -> None:
    pir_artifact = write_test_pir_artifact(tmp_path)
    plan = generate_residual_implementation_plan(pir_artifact=pir_artifact)
    first = freeze_residual_plan(
        output_dir=tmp_path / "compiled-a",
        pir_artifact=pir_artifact,
        expected_residual_plan_hash=plan.residual_plan_hash,
    )
    second = freeze_residual_plan(
        output_dir=tmp_path / "compiled-b",
        pir_artifact=pir_artifact,
        expected_residual_plan_hash=plan.residual_plan_hash,
    )

    assert first.residual_dry_run_hash == second.residual_dry_run_hash
    assert first.lock.as_dict() == second.lock.as_dict()
    assert (tmp_path / "compiled-a" / "residual-implementation-dry-run.json").read_bytes() == (
        tmp_path / "compiled-b" / "residual-implementation-dry-run.json"
    ).read_bytes()
    assert (tmp_path / "compiled-a" / "residual-execution-batches.json").exists()
    assert (tmp_path / "compiled-a" / "residual-task-import-preview.json").exists()
    assert (tmp_path / "compiled-a" / "residual-recovery-plan.json").exists()
    assert (tmp_path / "compiled-a" / "residual-dry-run-findings.json").exists()
    assert (tmp_path / "compiled-a" / "residual-implementation-plan.lock").exists()
    assert (tmp_path / "compiled-a" / "residual-plan-freeze.json").exists()


def test_prior_plan_coexistence_and_recovery_are_recorded(tmp_path: Path) -> None:
    dry_run = dry_run_residual_plan(pir_artifact=write_test_pir_artifact(tmp_path))

    assert any("PLAN-1a75a2e3c5a7 v1" in item for item in dry_run.prior_plan_coexistence)
    assert {
        "after_claim",
        "after_worktree_created",
        "after_executor",
        "after_verification",
        "after_verified_commit",
        "before_db_completion",
        "between_tasks",
        "at_human_gates",
    } <= set(dry_run.recovery_points)
    assert set(dry_run.repair_path_coverage) == {preview.task_id for preview in dry_run.import_preview}


def test_residual_plan_invalidation_rules_cover_material_inputs() -> None:
    rules = set(residual_plan_invalidation_rules())

    assert "residual gap hash change" in rules
    assert "residual plan hash change" in rules
    assert "residual feasibility hash change" in rules
    assert "residual human gate definition change" in rules
