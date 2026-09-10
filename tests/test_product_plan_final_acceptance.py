from __future__ import annotations

import json
from pathlib import Path

from ai_ent.product_dry_run import write_product_dry_run
from ai_ent.product_plan_final_acceptance import (
    evaluate_frozen_product_plan_acceptance,
    write_frozen_product_plan_acceptance,
)
from tests.test_product_dry_run import _pdf_inputs


def _runtime_snapshot(**overrides: object) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "deployment_actions": 0,
        "postgresql_authority": "active",
        "prior_plan_13_passed": True,
        "product_active_leases": 0,
        "product_executions": 0,
        "product_gates_approved": 0,
        "product_plan_imports": 0,
        "product_tasks_imported": 0,
        "project_id": "PRJ-AI-ENT",
        "residual_plan_7_passed": True,
    }
    snapshot.update(overrides)
    return snapshot


def _ppg_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, Path, Path, str, str, str, str, str]:
    (
        plan_path,
        ppa_path,
        pfe_path,
        decision_path,
        plan_hash,
        ppa_hash,
        pfe_hash,
        decision_hash,
    ) = _pdf_inputs(tmp_path)
    pdf = write_product_dry_run(
        accepted_plan_path=plan_path,
        ppa_path=ppa_path,
        pfe_path=pfe_path,
        decision_path=decision_path,
        artifacts_dir=tmp_path / "artifacts",
        output_dir=tmp_path / "compiled",
        repository_root=tmp_path,
        expected_plan_hash=plan_hash,
        expected_ppa_acceptance_hash=ppa_hash,
        expected_pfe_hash=pfe_hash,
        expected_decision_hash=decision_hash,
    )
    return (
        plan_path,
        ppa_path,
        pfe_path,
        decision_path,
        tmp_path / "artifacts" / "pdf-001" / "PDF-001.json",
        tmp_path / "compiled" / "product-plan.lock",
        tmp_path / "compiled" / "product-task-import-preview.json",
        plan_hash,
        ppa_hash,
        pfe_hash,
        decision_hash,
        pdf.product_dry_run_hash,
    )


def _acceptance(tmp_path: Path, **snapshot_overrides: object):
    (
        plan_path,
        ppa_path,
        pfe_path,
        decision_path,
        pdf_path,
        lock_path,
        import_preview_path,
        plan_hash,
        ppa_hash,
        pfe_hash,
        decision_hash,
        pdf_hash,
    ) = _ppg_inputs(tmp_path)
    return evaluate_frozen_product_plan_acceptance(
        plan_path=plan_path,
        ppa_path=ppa_path,
        pfe_path=pfe_path,
        decision_path=decision_path,
        pdf_path=pdf_path,
        lock_path=lock_path,
        import_preview_path=import_preview_path,
        repository_root=tmp_path,
        expected_product_plan_hash=plan_hash,
        expected_ppa_hash=ppa_hash,
        expected_pfe_hash=pfe_hash,
        expected_decision_hash=decision_hash,
        expected_pdf_hash=pdf_hash,
        generated_at="2026-09-10T12:00:00Z",
        runtime_snapshot_override=_runtime_snapshot(**snapshot_overrides),
    )


def test_ppg_001_accepts_frozen_product_plan_with_affected_decisions(tmp_path: Path) -> None:
    gate = _acceptance(tmp_path)

    assert gate.result == "ACCEPTED_WITH_AFFECTED_TASK_DECISIONS"
    assert gate.recommendation == "READY_FOR_PHI_001"
    assert gate.product_plan_id.startswith("PRODUCT-PLAN-")
    assert gate.plan_version == "1"
    assert gate.frozen_state == "FROZEN"
    assert gate.counts["tasks"] == 25
    assert gate.counts["dependency_edges"] == 51
    assert gate.counts["waves"] == 9
    assert gate.counts["human_gates"] == 8
    assert gate.risk_distribution == {"HIGH": 8, "LOW": 2, "MEDIUM": 15}
    assert gate.policy_distribution == {
        "AUTO_ALLOWED": 2,
        "GUARDED_ALLOWED": 15,
        "HUMAN_APPROVAL_REQUIRED": 8,
    }
    assert not gate.blockers


def test_ppg_001_validates_lineage_and_task_contracts(tmp_path: Path) -> None:
    gate = _acceptance(tmp_path)

    assert gate.validations["accepted_lineage_hashes"] == "PASS"
    assert gate.validations["pdf_lineage_reconciliation"] == "PASS"
    assert gate.validations["task_contracts_complete"] == "PASS"
    assert gate.validations["dependency_graph"] == "PASS"
    assert gate.validations["waves_and_critical_path"] == "PASS"
    assert gate.bound_hashes["accepted_prd_hash"]
    assert gate.bound_hashes["frozen_plan_lock_hash"]


def test_ppg_001_preserves_prd_dec_002_and_003_boundaries(tmp_path: Path) -> None:
    gate = _acceptance(tmp_path)

    assert gate.decision_states["PRD-DEC-002"]["state"] == (
        "UNRESOLVED_MUST_RESOLVE_BEFORE_AFFECTED_TASK"
    )
    assert gate.decision_states["PRD-DEC-002"]["affected_tasks"] == [
        "PRD-TASK-019",
        "PRD-TASK-023",
        "PRD-TASK-025",
    ]
    assert gate.decision_states["PRD-DEC-003"]["affected_tasks"] == [
        "PRD-TASK-020",
        "PRD-TASK-023",
        "PRD-TASK-025",
    ]
    assert gate.validations["prd_dec_002_boundary"] == "PASS"
    assert gate.validations["prd_dec_003_boundary"] == "PASS"
    assert len(gate.limitations) == 2


def test_ppg_001_accepts_security_rbac_surfaces_and_runtime_boundaries(
    tmp_path: Path,
) -> None:
    gate = _acceptance(tmp_path)

    assert gate.security_acceptance["result"] == "PASS"
    assert gate.rbac_acceptance["result"] == "PASS"
    assert gate.external_project_runtime_acceptance["result"] == "PASS"
    assert gate.surface_coverage["result"] == "PASS"
    assert gate.networking_policy == {
        "lan": "DECISION_GATED_BY_PRD-DEC-002",
        "localhost": "ALLOWED",
        "public_internet": "PROHIBITED_NOT_IN_CURRENT_PLAN",
    }
    assert gate.secrets_policy["production_backend"] == "DECISION_GATED_BY_PRD-DEC-003"
    assert gate.docker_boundary["unrestricted_daemon_control"] == "DENIED"
    assert gate.recovery_compatibility["result"] == "PASS"
    assert gate.import_compatibility["result"] == "PASS"


def test_ppg_001_rejects_if_product_runtime_already_started(tmp_path: Path) -> None:
    gate = _acceptance(tmp_path, product_tasks_imported=1)

    assert gate.result == "REJECTED"
    assert "runtime_not_started" in gate.blockers
    assert gate.validations["runtime_import_compatibility"] == "FAIL"


def test_ppg_001_rejects_pdf_hash_mismatch(tmp_path: Path) -> None:
    (
        plan_path,
        ppa_path,
        pfe_path,
        decision_path,
        pdf_path,
        lock_path,
        import_preview_path,
        plan_hash,
        ppa_hash,
        pfe_hash,
        decision_hash,
        _pdf_hash,
    ) = _ppg_inputs(tmp_path)

    gate = evaluate_frozen_product_plan_acceptance(
        plan_path=plan_path,
        ppa_path=ppa_path,
        pfe_path=pfe_path,
        decision_path=decision_path,
        pdf_path=pdf_path,
        lock_path=lock_path,
        import_preview_path=import_preview_path,
        repository_root=tmp_path,
        expected_product_plan_hash=plan_hash,
        expected_ppa_hash=ppa_hash,
        expected_pfe_hash=pfe_hash,
        expected_decision_hash=decision_hash,
        expected_pdf_hash="wrong",
        generated_at="2026-09-10T12:00:00Z",
        runtime_snapshot_override=_runtime_snapshot(),
    )

    assert gate.result == "REJECTED"
    assert "accepted_lineage_hashes" in gate.blockers


def test_ppg_001_writer_outputs_evidence_artifacts(tmp_path: Path) -> None:
    (
        plan_path,
        ppa_path,
        pfe_path,
        decision_path,
        pdf_path,
        lock_path,
        import_preview_path,
        plan_hash,
        ppa_hash,
        pfe_hash,
        decision_hash,
        pdf_hash,
    ) = _ppg_inputs(tmp_path)

    gate = write_frozen_product_plan_acceptance(
        plan_path=plan_path,
        ppa_path=ppa_path,
        pfe_path=pfe_path,
        decision_path=decision_path,
        pdf_path=pdf_path,
        lock_path=lock_path,
        import_preview_path=import_preview_path,
        artifacts_dir=tmp_path / "artifacts",
        repository_root=tmp_path,
        expected_product_plan_hash=plan_hash,
        expected_ppa_hash=ppa_hash,
        expected_pfe_hash=pfe_hash,
        expected_decision_hash=decision_hash,
        expected_pdf_hash=pdf_hash,
        runtime_snapshot_override=_runtime_snapshot(),
    )
    artifact = json.loads((tmp_path / "artifacts" / "ppg-001" / "PPG-001.json").read_text())

    assert artifact["acceptance_hash"] == gate.acceptance_hash
    assert (tmp_path / "artifacts" / "ppg-001" / "PPG-001.md").exists()
