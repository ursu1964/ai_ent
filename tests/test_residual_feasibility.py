from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ai_ent.project_manifest import EnvironmentProfile
from ai_ent.residual_dag import generate_residual_implementation_plan
from ai_ent.residual_feasibility import (
    EXPECTED_RESIDUAL_TASK_IDS,
    default_residual_feasibility_policy,
    evaluate_residual_plan_feasibility,
    write_residual_feasibility,
)


def environment(*, codex_configured: bool = True, ram_mb: int | None = 8192) -> EnvironmentProfile:
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
        ram_mb=ram_mb,
        gpu_available=False,
        external_network="available",
        configured_secret_names=(),
    )


def test_all_seven_residual_tasks_are_evaluated() -> None:
    plan = generate_residual_implementation_plan()
    evaluation = evaluate_residual_plan_feasibility(
        plan,
        environment(),
        default_residual_feasibility_policy(plan),
    )

    assert tuple(result.task_id for result in evaluation.task_results) == EXPECTED_RESIDUAL_TASK_IDS
    assert evaluation.as_dict()["summary"]["total_tasks"] == 7


def test_evidence_only_tasks_remain_evidence_only_when_source_proof_exists() -> None:
    plan = generate_residual_implementation_plan()
    evaluation = evaluate_residual_plan_feasibility(
        plan,
        environment(),
        default_residual_feasibility_policy(plan),
    )

    evidence_results = [result for result in evaluation.task_results if result.task_type == "EVIDENCE_CLOSURE_TASK"]

    assert {result.task_id for result in evidence_results} == {
        "RES-C05-EVIDENCE",
        "RES-C06-EVIDENCE",
        "RES-C07-EVIDENCE",
        "RES-C09-EVIDENCE",
    }
    assert all(result.evidence_feasibility.status == "AVAILABLE" for result in evidence_results)


def test_evidence_only_task_flags_inconsistency_when_source_proof_is_missing(tmp_path: Path) -> None:
    plan = generate_residual_implementation_plan()
    evaluation = evaluate_residual_plan_feasibility(
        plan,
        environment(),
        default_residual_feasibility_policy(plan),
        repository_root=tmp_path,
    )

    result = next(item for item in evaluation.task_results if item.task_id == "RES-C05-EVIDENCE")

    assert result.feasibility_status == "BLOCKED"
    assert any("evidence-only residual lacks source proof file" in blocker for blocker in result.blockers)


def test_c08_missing_model_profile_blocks_and_propagates() -> None:
    plan = generate_residual_implementation_plan()
    policy = default_residual_feasibility_policy(plan)
    policy = replace(policy, model_profiles=("ARCHITECTURE_REASONING", "CODING_STANDARD", "SECURITY_REVIEW"))

    evaluation = evaluate_residual_plan_feasibility(plan, environment(), policy)

    service = next(item for item in evaluation.task_results if item.task_id == "RES-C08-SERVICE")
    verification = next(item for item in evaluation.task_results if item.task_id == "RES-C08-VERIFICATION")
    assert service.feasibility_status == "BLOCKED"
    assert "model profile CODING_HIGH is missing" in service.blockers
    assert verification.feasibility_status == "BLOCKED"
    assert "BLOCKED_BY_DEPENDENCY:RES-C08-SERVICE" in verification.blockers


def test_c08_missing_verification_profile_blocks() -> None:
    plan = generate_residual_implementation_plan()
    policy = default_residual_feasibility_policy(plan)
    policy = replace(policy, verification_profiles=("STANDARD_REGRESSION",))

    evaluation = evaluate_residual_plan_feasibility(plan, environment(), policy)

    contract = next(item for item in evaluation.task_results if item.task_id == "RES-C08-CONTRACT")
    assert contract.feasibility_status == "BLOCKED"
    assert "verification profile FULL_REGRESSION is missing" in contract.blockers


def test_human_gated_high_risk_tasks_remain_unapproved() -> None:
    plan = generate_residual_implementation_plan()
    evaluation = evaluate_residual_plan_feasibility(
        plan,
        environment(),
        default_residual_feasibility_policy(plan),
    )

    gated = [result for result in evaluation.task_results if result.policy_decision == "HUMAN_APPROVAL_REQUIRED"]

    assert {result.task_id for result in gated} == {"RES-C08-CONTRACT", "RES-C08-SERVICE"}
    assert all(result.feasibility_status == "HUMAN_APPROVAL_REQUIRED" for result in gated)
    assert len(evaluation.human_gates) == 2


def test_safe_task_marked_feasible_and_conditional_codex_is_reported() -> None:
    plan = generate_residual_implementation_plan()
    policy = default_residual_feasibility_policy(plan)
    configured = evaluate_residual_plan_feasibility(plan, environment(codex_configured=True), policy)
    conditional = evaluate_residual_plan_feasibility(plan, environment(codex_configured=False), policy)

    safe_task = next(item for item in configured.task_results if item.task_id == "RES-C05-EVIDENCE")
    conditional_task = next(item for item in conditional.task_results if item.task_id == "RES-C05-EVIDENCE")

    assert safe_task.feasibility_status == "FEASIBLE"
    assert conditional_task.feasibility_status == "FEASIBLE_WITH_CONDITIONS"
    assert conditional_task.conditions == ("set AIENT_CODEX_COMMAND before residual execution",)


def test_high_risk_task_cannot_bypass_policy() -> None:
    plan = generate_residual_implementation_plan()
    policy = default_residual_feasibility_policy(plan)
    policy = replace(policy, guarded_allowed_risks=("LOW", "MEDIUM"))

    evaluation = evaluate_residual_plan_feasibility(plan, environment(), policy)

    high = next(item for item in evaluation.task_results if item.task_id == "RES-C08-CONTRACT")
    assert high.feasibility_status == "BLOCKED"
    assert high.policy_decision == "PROHIBITED"


def test_concurrency_reduced_on_write_scope_conflict() -> None:
    plan = generate_residual_implementation_plan()
    evaluation = evaluate_residual_plan_feasibility(
        plan,
        environment(),
        default_residual_feasibility_policy(plan),
    )

    assert max(len(wave["task_ids"]) for wave in plan.waves) == 2
    assert evaluation.feasible_parallel_width == 1


def test_repeat_evaluation_byte_identical_and_material_environment_changes_hash(tmp_path: Path) -> None:
    first = write_residual_feasibility(output_dir=tmp_path / "compiled", repository_root=Path.cwd())
    first_bytes = (tmp_path / "compiled" / "residual-feasibility-report.json").read_bytes()
    second = write_residual_feasibility(output_dir=tmp_path / "compiled", repository_root=Path.cwd())

    plan = generate_residual_implementation_plan()
    constrained = evaluate_residual_plan_feasibility(
        plan,
        environment(ram_mb=512),
        default_residual_feasibility_policy(plan),
    )

    assert first.residual_feasibility_hash == second.residual_feasibility_hash
    assert (tmp_path / "compiled" / "residual-feasibility-report.json").read_bytes() == first_bytes
    assert constrained.residual_feasibility_hash != first.residual_feasibility_hash
