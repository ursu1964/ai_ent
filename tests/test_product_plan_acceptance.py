from __future__ import annotations

import json
from pathlib import Path

from ai_ent.product_plan_acceptance import (
    EXPECTED_PRODUCTIZATION_PLAN_HASH,
    evaluate_product_plan_acceptance,
    write_product_plan_acceptance,
)
from ai_ent.productization_plan import write_productization_plan
from tests.test_productization_plan import write_accepted_evidence


def write_plan(tmp_path: Path) -> Path:
    artifacts = tmp_path / "artifacts"
    output = tmp_path / "compiled"
    write_accepted_evidence(artifacts)
    write_productization_plan(artifacts_dir=artifacts, output_dir=output)
    return output / "productization-plan.json"


def test_ppa_001_accepts_plan_with_decisions_required(tmp_path: Path) -> None:
    plan_path = write_plan(tmp_path)
    plan = json.loads(plan_path.read_text())

    result = evaluate_product_plan_acceptance(
        plan_path=plan_path,
        expected_plan_hash=plan["plan_hash"],
    )

    assert result.result == "ACCEPTED_WITH_DECISIONS_REQUIRED"
    assert result.recommendation == "READY_FOR_PRODUCT_FEASIBILITY_AND_POLICY_WITH_DECISION_TRACKING"
    assert result.productization_plan_hash == plan["plan_hash"]
    assert result.plan_identity["task_count"] == 25
    assert result.plan_identity["dependency_edges"] == 51
    assert result.plan_identity["waves"] == 9
    assert result.plan_identity["human_gates"] == 8
    assert result.plan_identity["theoretical_parallel_width"] == 6
    assert result.plan_identity["risk_distribution"] == {"HIGH": 8, "LOW": 2, "MEDIUM": 15}
    assert result.current_blockers == ()


def test_ppa_001_records_resolution_timing_for_three_decisions(tmp_path: Path) -> None:
    plan_path = write_plan(tmp_path)
    plan = json.loads(plan_path.read_text())

    result = evaluate_product_plan_acceptance(
        plan_path=plan_path,
        expected_plan_hash=plan["plan_hash"],
    )
    by_id = {decision["decision_id"]: decision for decision in result.unresolved_human_decisions}

    assert set(by_id) == {"PRD-DEC-001", "PRD-DEC-002", "PRD-DEC-003"}
    assert by_id["PRD-DEC-001"]["resolution_timing"] == "MUST_RESOLVE_BEFORE_PLAN_FREEZE"
    assert by_id["PRD-DEC-002"]["resolution_timing"] == "MUST_RESOLVE_BEFORE_AFFECTED_TASK"
    assert by_id["PRD-DEC-003"]["resolution_timing"] == "MUST_RESOLVE_BEFORE_AFFECTED_TASK"


def test_ppa_001_reviews_authority_boundaries(tmp_path: Path) -> None:
    plan_path = write_plan(tmp_path)
    plan = json.loads(plan_path.read_text())

    result = evaluate_product_plan_acceptance(
        plan_path=plan_path,
        expected_plan_hash=plan["plan_hash"],
    )
    reviews = result.authority_reviews

    assert "mark task passed" in reviews["product_api_authority"]["forbidden_direct_operations"]
    assert "human gate approval" in reviews["ui_authority"]["forbidden_implicit_actions"]
    assert reviews["generated_app_isolation"]["status"] == "ACCEPTED"
    assert reviews["evidence_model"]["authority"].startswith("Artifact Evidence Graph is NON_AUTHORITATIVE")
    assert reviews["sse_realtime"]["authority"] == "observation/update stream only"


def test_ppa_001_reviews_human_gates_and_high_risk_tasks(tmp_path: Path) -> None:
    plan_path = write_plan(tmp_path)
    plan = json.loads(plan_path.read_text())

    result = evaluate_product_plan_acceptance(
        plan_path=plan_path,
        expected_plan_hash=plan["plan_hash"],
    )

    assert len(result.human_gate_review) == 8
    assert all(gate["status"] == "REVIEWED_NOT_APPROVED" for gate in result.human_gate_review)
    assert len(result.high_risk_task_review) == 8
    assert {task["task_id"] for task in result.high_risk_task_review} == {
        "PRD-TASK-003",
        "PRD-TASK-006",
        "PRD-TASK-008",
        "PRD-TASK-009",
        "PRD-TASK-016",
        "PRD-TASK-018",
        "PRD-TASK-019",
        "PRD-TASK-020",
    }


def test_ppa_001_rejects_plan_hash_mismatch(tmp_path: Path) -> None:
    plan_path = write_plan(tmp_path)

    result = evaluate_product_plan_acceptance(
        plan_path=plan_path,
        expected_plan_hash=EXPECTED_PRODUCTIZATION_PLAN_HASH,
    )

    assert result.result == "REJECTED"
    assert "productization plan hash mismatch" in result.current_blockers


def test_ppa_001_writer_is_deterministic(tmp_path: Path) -> None:
    plan_path = write_plan(tmp_path)
    plan = json.loads(plan_path.read_text())
    artifacts = tmp_path / "ppa-artifacts"

    first = write_product_plan_acceptance(
        plan_path=plan_path,
        artifacts_dir=artifacts,
        expected_plan_hash=plan["plan_hash"],
    )
    first_json = (artifacts / "ppa-001" / "PPA-001.json").read_bytes()
    second = write_product_plan_acceptance(
        plan_path=plan_path,
        artifacts_dir=artifacts,
        expected_plan_hash=plan["plan_hash"],
    )

    assert first.acceptance_hash == second.acceptance_hash
    assert (artifacts / "ppa-001" / "PPA-001.json").read_bytes() == first_json
