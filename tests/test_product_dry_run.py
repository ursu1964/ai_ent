from __future__ import annotations

import copy
import json
from pathlib import Path

from ai_ent.product_decisions import record_prd_dec_001
from ai_ent.product_dry_run import dry_run_product_plan, write_product_dry_run
from ai_ent.product_feasibility import evaluate_product_feasibility
from ai_ent.project_manifest import canonical_bytes
from tests.test_product_feasibility import available_environment, write_pfe_inputs


def _pdf_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, str, str, str, str]:
    plan_path, ppa_path, plan_hash = write_pfe_inputs(tmp_path)
    pfe = evaluate_product_feasibility(
        plan_path=plan_path,
        ppa_path=ppa_path,
        repository_root=tmp_path,
        expected_plan_hash=plan_hash,
        environment_override=available_environment(),
    )
    pfe_path = tmp_path / "artifacts" / "pfe-001" / "PFE-001.json"
    pfe_path.parent.mkdir(parents=True, exist_ok=True)
    pfe_path.write_bytes(canonical_bytes(pfe.as_dict()) + b"\n")
    ppa = json.loads(ppa_path.read_text())
    decision = record_prd_dec_001(
        plan_path=plan_path,
        ppa_path=ppa_path,
        pfe_path=pfe_path,
        artifacts_dir=tmp_path / "artifacts",
        output_dir=tmp_path / "compiled",
        repository_root=tmp_path,
        decided_at="2026-09-10T12:00:00+00:00",
        expected_plan_hash=plan_hash,
        expected_ppa_acceptance_hash=str(ppa["acceptance_hash"]),
        expected_pfe_hash=pfe.product_feasibility_hash,
    )
    return (
        plan_path,
        ppa_path,
        pfe_path,
        tmp_path / "artifacts" / "product-decisions" / "PRD-DEC-001.json",
        plan_hash,
        str(ppa["acceptance_hash"]),
        pfe.product_feasibility_hash,
        decision.decision_hash,
    )


def _dry_run(tmp_path: Path, candidate: dict[str, object] | None = None):
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
    return dry_run_product_plan(
        accepted_plan_path=plan_path,
        ppa_path=ppa_path,
        pfe_path=pfe_path,
        decision_path=decision_path,
        artifacts_dir=tmp_path / "artifacts",
        repository_root=tmp_path,
        expected_plan_hash=plan_hash,
        expected_ppa_acceptance_hash=ppa_hash,
        expected_pfe_hash=pfe_hash,
        expected_decision_hash=decision_hash,
        regenerated_candidate=candidate,
    )


def _dry_run_with_inputs(
    *,
    plan_path: Path,
    ppa_path: Path,
    pfe_path: Path,
    decision_path: Path,
    plan_hash: str,
    ppa_hash: str,
    pfe_hash: str,
    decision_hash: str,
    artifacts_dir: Path,
    repository_root: Path,
    candidate: dict[str, object],
):
    return dry_run_product_plan(
        accepted_plan_path=plan_path,
        ppa_path=ppa_path,
        pfe_path=pfe_path,
        decision_path=decision_path,
        artifacts_dir=artifacts_dir,
        repository_root=repository_root,
        expected_plan_hash=plan_hash,
        expected_ppa_acceptance_hash=ppa_hash,
        expected_pfe_hash=pfe_hash,
        expected_decision_hash=decision_hash,
        regenerated_candidate=candidate,
    )


def test_pdf_001_reconciles_provenance_only_candidate_drift(tmp_path: Path) -> None:
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
    candidate = copy.deepcopy(json.loads(plan_path.read_text()))
    candidate["plan_hash"] = "candidate-provenance-hash"
    candidate["baseline_head"] = "new-head"
    candidate["bound_evidence"] = {"saag_001": {"head": "new-head"}}

    result = _dry_run_with_inputs(
        plan_path=plan_path,
        ppa_path=ppa_path,
        pfe_path=pfe_path,
        decision_path=decision_path,
        plan_hash=plan_hash,
        ppa_hash=ppa_hash,
        pfe_hash=pfe_hash,
        decision_hash=decision_hash,
        artifacts_dir=tmp_path / "artifacts",
        repository_root=tmp_path,
        candidate=candidate,
    )

    assert result.result == "READY_WITH_AFFECTED_TASK_DECISIONS"
    assert result.frozen_plan_state == "FROZEN"
    assert result.lineage_reconciliation["result"] == "PASS"
    assert result.lineage_reconciliation["material_difference_count"] == 0
    assert set(result.lineage_reconciliation["classification"]) == {
        "DECISION_BINDING",
        "PROVENANCE_ONLY",
    }


def test_pdf_001_blocks_material_plan_drift(tmp_path: Path) -> None:
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
    candidate = copy.deepcopy(json.loads(plan_path.read_text()))
    candidate["productization_dag"]["tasks"][0]["objective"] = "changed objective"

    result = _dry_run_with_inputs(
        plan_path=plan_path,
        ppa_path=ppa_path,
        pfe_path=pfe_path,
        decision_path=decision_path,
        plan_hash=plan_hash,
        ppa_hash=ppa_hash,
        pfe_hash=pfe_hash,
        decision_hash=decision_hash,
        artifacts_dir=tmp_path / "artifacts",
        repository_root=tmp_path,
        candidate=candidate,
    )

    assert result.result == "PLAN_REACCEPTANCE_REQUIRED"
    assert result.frozen_plan_state == "INVALIDATED"
    assert "MATERIAL_TASK_CHANGE" in result.lineage_reconciliation["classification"]
    assert any(finding.finding_id == "PDF-LINEAGE-RECONCILIATION" for finding in result.findings)


def test_pdf_001_freezes_all_task_contracts_edges_and_gates(tmp_path: Path) -> None:
    result = _dry_run(tmp_path)

    assert result.task_count == 25
    assert len(result.import_preview) == 25
    assert result.dependency_edge_count == 51
    assert result.wave_count == 9
    assert result.human_gate_count == 8
    assert result.risk_distribution == {"HIGH": 8, "LOW": 2, "MEDIUM": 15}
    assert result.policy_distribution == {
        "AUTO_ALLOWED": 2,
        "GUARDED_ALLOWED": 15,
        "HUMAN_APPROVAL_REQUIRED": 8,
    }
    assert all(preview.fingerprint for preview in result.import_preview)
    assert all(preview.agent_role for preview in result.import_preview)
    assert all(preview.model_profile for preview in result.import_preview)
    assert all(preview.executor for preview in result.import_preview)
    assert all(preview.verification_profile for preview in result.import_preview)


def test_pdf_001_binds_prd_dec_001_and_preserves_affected_task_decisions(
    tmp_path: Path,
) -> None:
    result = _dry_run(tmp_path)

    assert result.decision_boundaries["PRD-DEC-001"]["state"] == "ACCEPTED"
    assert result.decision_boundaries["PRD-DEC-001"]["accepted_stack"] == {
        "backend_api": "FastAPI + Pydantic",
        "frontend": "React + TypeScript + Vite",
        "realtime": "SSE-first",
    }
    assert result.decision_boundaries["PRD-DEC-002"]["state"] == (
        "UNRESOLVED_MUST_RESOLVE_BEFORE_AFFECTED_TASK"
    )
    assert result.decision_boundaries["PRD-DEC-003"]["state"] == (
        "UNRESOLVED_MUST_RESOLVE_BEFORE_AFFECTED_TASK"
    )
    assert "PRD-TASK-019" in result.decision_boundaries["PRD-DEC-002"]["affected_tasks"]
    assert "PRD-TASK-020" in result.decision_boundaries["PRD-DEC-003"]["affected_tasks"]
    assert any(
        "PRD-DEC-002" in batch.required_human_gates
        for batch in result.execution_batches
        if batch.task_ids == ("PRD-TASK-019",)
    )
    assert any(
        "PRD-DEC-003" in batch.required_human_gates
        for batch in result.execution_batches
        if batch.task_ids == ("PRD-TASK-020",)
    )


def test_pdf_001_enforces_conservative_runtime_limits(tmp_path: Path) -> None:
    result = _dry_run(tmp_path)

    assert result.theoretical_parallel_width == 6
    assert result.effective_concurrency == 1
    assert result.execution_limits == {
        "effective_concurrency": 1,
        "max_failures_per_run": 1,
        "max_repairs_per_task": 1,
        "max_tasks_per_run": 1,
    }
    assert len(result.execution_batches) == 25
    assert all(batch.task_ids and len(batch.task_ids) == 1 for batch in result.execution_batches)


def test_pdf_001_security_rbac_api_ui_sse_artifact_and_docker_dry_runs_pass(
    tmp_path: Path,
) -> None:
    result = _dry_run(tmp_path)

    assert result.security_dry_run["result"] == "PASS"
    assert result.rbac_dry_run["roles"]["approver"]
    assert result.external_project_runtime_dry_run["hardcoded_e2e_identity_used"] is False
    assert result.api_dry_run["result"] == "PASS"
    assert result.api_dry_run["raw_database_filesystem_control_plane_authority"] == "DENIED"
    assert result.ui_dry_run["result"] == "PASS"
    assert result.sse_dry_run["observation_only_messages"] == "PASS"
    assert result.artifact_dry_run["arbitrary_filesystem_path"] == "DENIED"
    assert result.docker_network_secrets_dry_run["docker_operations"][
        "unrestricted_daemon_control"
    ] == "DENIED"
    assert result.docker_network_secrets_dry_run["networking"]["lan"] == (
        "DECISION_GATED_BY_PRD-DEC-002"
    )
    assert result.docker_network_secrets_dry_run["secrets"]["production_backend"] == (
        "DECISION_GATED_BY_PRD-DEC-003"
    )


def test_pdf_001_import_preview_and_recovery_are_runtime_compatible(tmp_path: Path) -> None:
    result = _dry_run(tmp_path)

    assert all(preview.execution_class == "implementation" for preview in result.import_preview)
    assert all(isinstance(preview.depends_on, tuple) for preview in result.import_preview)
    assert "at_unresolved_decision_boundary" in result.recovery_points
    assert len(result.repair_path_coverage) == 25
    assert any("PLAN-1a75a2e3c5a7 v1" in item for item in result.prior_plan_coexistence)
    assert any("RESIDUAL-PLAN-a918c449cfe5 v1" in item for item in result.prior_plan_coexistence)


def test_pdf_001_repeated_dry_run_and_freeze_are_byte_identical(tmp_path: Path) -> None:
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
    kwargs = {
        "accepted_plan_path": plan_path,
        "ppa_path": ppa_path,
        "pfe_path": pfe_path,
        "decision_path": decision_path,
        "artifacts_dir": tmp_path / "artifacts",
        "output_dir": tmp_path / "compiled",
        "repository_root": tmp_path,
        "expected_plan_hash": plan_hash,
        "expected_ppa_acceptance_hash": ppa_hash,
        "expected_pfe_hash": pfe_hash,
        "expected_decision_hash": decision_hash,
    }

    first = write_product_dry_run(**kwargs)
    first_payload = canonical_bytes(first.as_dict())
    first_freeze = (tmp_path / "compiled" / "product-plan-freeze.json").read_bytes()
    second = write_product_dry_run(**kwargs)

    assert first.product_dry_run_hash == second.product_dry_run_hash
    assert canonical_bytes(second.as_dict()) == first_payload
    assert (tmp_path / "compiled" / "product-plan-freeze.json").read_bytes() == first_freeze
