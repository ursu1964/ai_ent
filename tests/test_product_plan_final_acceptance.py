from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


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


def test_ppg_001_records_control_plane_authority_and_coexisting_plan_boundaries(
    tmp_path: Path,
) -> None:
    gate = _acceptance(tmp_path)

    assert gate.runtime_snapshot["postgresql_authority"] == "active"
    assert gate.runtime_snapshot["prior_plan_13_passed"] is True
    assert gate.runtime_snapshot["residual_plan_7_passed"] is True
    assert gate.runtime_snapshot["product_active_leases"] == 0
    assert gate.import_compatibility["coexists_with"] == [
        "PLAN-1a75a2e3c5a7 v1",
        "RESIDUAL-PLAN-a918c449cfe5 v1",
    ]
    assert gate.import_compatibility["idempotency_requirements"] == {
        "duplicates": "denied",
        "exact_repeat_import": "ALREADY_IMPORTED / IN_SYNC",
        "material_mismatch": "CONFLICT",
        "partial_failure": "rollback",
    }


def test_ppg_001_rejects_if_human_gate_boundary_is_implicitly_approved(
    tmp_path: Path,
) -> None:
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
    lock = json.loads(lock_path.read_text())
    lock["human_gate_definitions"][0]["status"] = "APPROVED_WITHOUT_OPERATOR"
    _write_json(lock_path, lock)

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
        expected_pdf_hash=pdf_hash,
        generated_at="2026-09-10T12:00:00Z",
        runtime_snapshot_override=_runtime_snapshot(),
    )

    assert gate.result == "REJECTED"
    assert "human_gates_pending" in gate.blockers
    assert gate.validations["human_gates_pending"] == "FAIL"


def test_ppg_001_rejects_if_independent_verification_contract_is_removed(
    tmp_path: Path,
) -> None:
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
    preview = json.loads(import_preview_path.read_text())
    preview["task_import_preview"][0]["verification_profile"] = ""
    _write_json(import_preview_path, preview)

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
        expected_pdf_hash=pdf_hash,
        generated_at="2026-09-10T12:00:00Z",
        runtime_snapshot_override=_runtime_snapshot(),
    )

    assert gate.result == "REJECTED"
    assert "task_contracts_complete" in gate.blockers
    assert "runtime_import_compatibility" in gate.blockers
    assert gate.import_compatibility["result"] == "FAIL"


def test_ppg_001_rejects_lan_readiness_if_prd_dec_002_gate_is_relaxed(
    tmp_path: Path,
) -> None:
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
    pdf = json.loads(pdf_path.read_text())
    pdf["docker_network_secrets_dry_run"]["networking"]["lan"] = "ALLOWED"
    _write_json(pdf_path, pdf)

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
        expected_pdf_hash=pdf_hash,
        generated_at="2026-09-10T12:00:00Z",
        runtime_snapshot_override=_runtime_snapshot(),
    )

    assert gate.result == "REJECTED"
    assert "networking_policy" in gate.blockers
    assert gate.networking_policy["lan"] == "ALLOWED"


def test_ppg_001_rejects_when_security_boundary_or_secret_artifact_policy_fails(
    tmp_path: Path,
) -> None:
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
    pdf = json.loads(pdf_path.read_text())
    pdf["security_dry_run"]["checks"]["api_cannot_bypass_control_plane"] = "FAIL"
    pdf["docker_network_secrets_dry_run"]["secrets"]["secret_values_in_artifacts"] = "ALLOWED"
    _write_json(pdf_path, pdf)

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
        expected_pdf_hash=pdf_hash,
        generated_at="2026-09-10T12:00:00Z",
        runtime_snapshot_override=_runtime_snapshot(),
    )

    assert gate.result == "REJECTED"
    assert "security_acceptance" in gate.blockers
    assert "secrets_policy" in gate.blockers
    assert gate.security_acceptance["checks"]["api_cannot_bypass_control_plane"] == "FAIL"
    assert gate.secrets_policy["secret_values_in_artifacts"] == "ALLOWED"


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
