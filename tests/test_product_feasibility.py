from __future__ import annotations

from pathlib import Path

from ai_ent.product_feasibility import (
    evaluate_product_feasibility,
    write_product_feasibility,
)
from ai_ent.product_plan_acceptance import write_product_plan_acceptance
from ai_ent.productization_plan import write_productization_plan
from tests.test_productization_plan import write_accepted_evidence


def write_pfe_inputs(tmp_path: Path) -> tuple[Path, Path, str]:
    artifacts = tmp_path / "artifacts"
    output = tmp_path / "compiled"
    write_accepted_evidence(artifacts)
    plan = write_productization_plan(artifacts_dir=artifacts, output_dir=output)
    write_product_plan_acceptance(
        plan_path=output / "productization-plan.json",
        artifacts_dir=artifacts,
        expected_plan_hash=plan.plan_hash,
    )
    return (
        output / "productization-plan.json",
        artifacts / "ppa-001" / "PPA-001.json",
        plan.plan_hash,
    )


def available_environment() -> dict[str, object]:
    return {
        "aient_codex_command": {"configured": True, "status": "AVAILABLE"},
        "codex": {"status": "AVAILABLE"},
        "docker": {"status": "AVAILABLE"},
        "docker_compose": {"status": "AVAILABLE"},
        "fastapi": {"status": "AVAILABLE"},
        "git_worktrees": {"status": "AVAILABLE"},
        "node": {"status": "AVAILABLE"},
        "npm": {"status": "AVAILABLE"},
        "postgresql": {"status": "AVAILABLE"},
        "react_typescript_vite": {"status": "AVAILABLE"},
        "reverse_proxy": {"status": "AVAILABLE"},
        "uvicorn": {"status": "AVAILABLE"},
    }


def test_pfe_001_evaluates_all_product_tasks_and_contracts(tmp_path: Path) -> None:
    plan_path, ppa_path, plan_hash = write_pfe_inputs(tmp_path)

    result = evaluate_product_feasibility(
        plan_path=plan_path,
        ppa_path=ppa_path,
        repository_root=tmp_path,
        expected_plan_hash=plan_hash,
        environment_override=available_environment(),
    )
    payload = result.as_dict()

    assert result.result == "READY_WITH_DECISIONS_REQUIRED"
    assert payload["summary"]["total_tasks"] == 25
    assert len(result.task_contracts) == 25
    assert payload["summary"]["human_gates"] == 8
    assert payload["summary"]["risk_distribution"] == {"HIGH": 8, "LOW": 2, "MEDIUM": 15}
    assert all(contract.agent_role for contract in result.task_contracts)
    assert all(contract.model_profile for contract in result.task_contracts)
    assert all(contract.executor for contract in result.task_contracts)
    assert all(contract.verification_profile for contract in result.task_contracts)
    assert all(contract.contract_fingerprint for contract in result.task_contracts)


def test_pfe_001_missing_profiles_block_affected_tasks(tmp_path: Path) -> None:
    plan_path, ppa_path, plan_hash = write_pfe_inputs(tmp_path)

    result = evaluate_product_feasibility(
        plan_path=plan_path,
        ppa_path=ppa_path,
        repository_root=tmp_path,
        expected_plan_hash=plan_hash,
        environment_override=available_environment(),
        policy_override={
            "available_agent_roles": [],
            "available_model_profiles": [],
            "available_verification_profiles": [],
        },
    )

    assert result.result == "BLOCKED"
    assert any("missing agent role" in contract.blockers for contract in result.task_contracts)
    assert any("missing model profile" in contract.blockers for contract in result.task_contracts)
    assert any(
        "missing verification profile" in contract.blockers for contract in result.task_contracts
    )


def test_pfe_001_decision_dependencies_are_explicit(tmp_path: Path) -> None:
    plan_path, ppa_path, plan_hash = write_pfe_inputs(tmp_path)

    result = evaluate_product_feasibility(
        plan_path=plan_path,
        ppa_path=ppa_path,
        repository_root=tmp_path,
        expected_plan_hash=plan_hash,
        environment_override=available_environment(),
    )
    by_id = {contract.task_id: contract for contract in result.task_contracts}

    assert result.decision_plan["PRD-DEC-001"]["pfe_recommendation"] == "ACCEPT"
    assert result.decision_plan["PRD-DEC-001"]["blocks_plan_freeze"] is True
    assert "DECISION_REQUIRED:PRD-DEC-001" in by_id["PRD-TASK-005"].required_decisions
    assert "DECISION_REQUIRED:PRD-DEC-002" in by_id["PRD-TASK-019"].required_decisions
    assert "DECISION_REQUIRED:PRD-DEC-003" in by_id["PRD-TASK-020"].required_decisions
    assert result.as_dict()["summary"]["decision_blockers"] == 1


def test_pfe_001_security_policy_rejects_authority_bypasses(tmp_path: Path) -> None:
    plan_path, ppa_path, plan_hash = write_pfe_inputs(tmp_path)

    result = evaluate_product_feasibility(
        plan_path=plan_path,
        ppa_path=ppa_path,
        repository_root=tmp_path,
        expected_plan_hash=plan_hash,
        environment_override=available_environment(),
    )
    security = result.security_plan

    assert "mark tasks passed" in security["api_security"]["forbidden_direct_operations"]
    assert "invoke unrestricted Codex" in security["api_security"]["forbidden_direct_operations"]
    assert "path traversal" in security["artifact_serving"]["rejected_patterns"]
    assert security["sse"]["commands"] == "prohibited"
    assert (
        "generated apps read AI-Enterprise secrets"
        in security["external_project_runtime"]["isolation_rejections"]
    )
    assert "privileged containers" in security["docker_boundary"]["prohibited"]


def test_pfe_001_high_risk_tasks_remain_human_gated(tmp_path: Path) -> None:
    plan_path, ppa_path, plan_hash = write_pfe_inputs(tmp_path)

    result = evaluate_product_feasibility(
        plan_path=plan_path,
        ppa_path=ppa_path,
        repository_root=tmp_path,
        expected_plan_hash=plan_hash,
        environment_override=available_environment(),
    )
    high = [contract for contract in result.task_contracts if contract.risk == "HIGH"]

    assert len(high) == 8
    assert all(contract.policy_decision == "HUMAN_APPROVAL_REQUIRED" for contract in high)
    assert all(contract.human_gate for contract in high)
    assert all(gate["status"] == "PENDING_NOT_APPROVED" for gate in result.human_gate_plan)


def test_pfe_001_dependency_blocking_propagates(tmp_path: Path) -> None:
    plan_path, ppa_path, plan_hash = write_pfe_inputs(tmp_path)

    result = evaluate_product_feasibility(
        plan_path=plan_path,
        ppa_path=ppa_path,
        repository_root=tmp_path,
        expected_plan_hash=plan_hash,
        environment_override={**available_environment(), "node": {"status": "MISSING"}},
    )
    by_id = {contract.task_id: contract for contract in result.task_contracts}

    assert by_id["PRD-TASK-012"].feasibility_status == "BLOCKED"
    assert by_id["PRD-TASK-013"].feasibility_status == "BLOCKED"
    assert "BLOCKED_BY_DEPENDENCY:PRD-TASK-012" in by_id["PRD-TASK-013"].blockers


def test_pfe_001_concurrency_is_deterministic_and_conservative(tmp_path: Path) -> None:
    plan_path, ppa_path, plan_hash = write_pfe_inputs(tmp_path)

    result = evaluate_product_feasibility(
        plan_path=plan_path,
        ppa_path=ppa_path,
        repository_root=tmp_path,
        expected_plan_hash=plan_hash,
        environment_override=available_environment(),
    )

    assert result.concurrency_plan["theoretical_parallel_width"] == 6
    assert result.concurrency_plan["feasible_width"] == 1
    assert "write-scope overlap" in result.concurrency_plan["conflict_classes"]


def test_pfe_001_repeated_output_is_byte_identical(tmp_path: Path) -> None:
    plan_path, ppa_path, plan_hash = write_pfe_inputs(tmp_path)
    artifacts = tmp_path / "pfe-artifacts"
    output = tmp_path / "pfe-compiled"

    first = write_product_feasibility(
        plan_path=plan_path,
        ppa_path=ppa_path,
        artifacts_dir=artifacts,
        output_dir=output,
        repository_root=tmp_path,
        expected_plan_hash=plan_hash,
    )
    first_bytes = (artifacts / "pfe-001" / "PFE-001.json").read_bytes()
    second = write_product_feasibility(
        plan_path=plan_path,
        ppa_path=ppa_path,
        artifacts_dir=artifacts,
        output_dir=output,
        repository_root=tmp_path,
        expected_plan_hash=plan_hash,
    )

    assert first.product_feasibility_hash == second.product_feasibility_hash
    assert (artifacts / "pfe-001" / "PFE-001.json").read_bytes() == first_bytes


def test_pfe_001_material_policy_or_environment_change_changes_hash(tmp_path: Path) -> None:
    plan_path, ppa_path, plan_hash = write_pfe_inputs(tmp_path)

    first = evaluate_product_feasibility(
        plan_path=plan_path,
        ppa_path=ppa_path,
        repository_root=tmp_path,
        expected_plan_hash=plan_hash,
        environment_override=available_environment(),
    )
    second = evaluate_product_feasibility(
        plan_path=plan_path,
        ppa_path=ppa_path,
        repository_root=tmp_path,
        expected_plan_hash=plan_hash,
        environment_override={**available_environment(), "docker": {"status": "MISSING"}},
    )

    assert first.product_feasibility_hash != second.product_feasibility_hash


def test_pfe_001_rejects_plan_hash_mismatch(tmp_path: Path) -> None:
    plan_path, ppa_path, _plan_hash = write_pfe_inputs(tmp_path)

    result = evaluate_product_feasibility(
        plan_path=plan_path,
        ppa_path=ppa_path,
        repository_root=tmp_path,
        expected_plan_hash="wrong",
        environment_override=available_environment(),
    )

    assert result.result == "BLOCKED"
    assert any(finding["id"] == "PFE-PLAN-HASH" for finding in result.findings)
