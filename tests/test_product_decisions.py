from __future__ import annotations

import json
from pathlib import Path

from ai_ent.product_decisions import record_prd_dec_001
from ai_ent.product_feasibility import write_product_feasibility
from tests.test_product_feasibility import available_environment, write_pfe_inputs


def write_decision_inputs(tmp_path: Path) -> tuple[Path, Path, Path, str, str, str]:
    plan_path, ppa_path, plan_hash = write_pfe_inputs(tmp_path)
    pfe = write_product_feasibility(
        plan_path=plan_path,
        ppa_path=ppa_path,
        artifacts_dir=tmp_path / "artifacts",
        output_dir=tmp_path / "compiled",
        repository_root=tmp_path,
        expected_plan_hash=plan_hash,
    )
    ppa = json.loads(ppa_path.read_text())
    pfe_path = tmp_path / "artifacts" / "pfe-001" / "PFE-001.json"
    return (
        plan_path,
        ppa_path,
        pfe_path,
        plan_hash,
        str(ppa["acceptance_hash"]),
        pfe.product_feasibility_hash,
    )


def test_prd_dec_001_records_accepted_stack_decision(tmp_path: Path) -> None:
    plan_path, ppa_path, pfe_path, plan_hash, ppa_hash, pfe_hash = write_decision_inputs(tmp_path)

    result = record_prd_dec_001(
        plan_path=plan_path,
        ppa_path=ppa_path,
        pfe_path=pfe_path,
        artifacts_dir=tmp_path / "artifacts",
        output_dir=tmp_path / "compiled",
        repository_root=tmp_path,
        decided_at="2026-09-10T12:00:00+00:00",
        expected_plan_hash=plan_hash,
        expected_ppa_acceptance_hash=ppa_hash,
        expected_pfe_hash=pfe_hash,
    )

    assert result.result == "RECORDED"
    assert result.recommendation == "READY_FOR_PDF_001"
    assert result.decision_status == "ACCEPTED"
    assert result.accepted_stack == {
        "backend_api": "FastAPI + Pydantic",
        "frontend": "React + TypeScript + Vite",
        "realtime": "SSE-first",
    }
    assert result.accepted_product_plan_lineage["productization_plan_hash"] == plan_hash
    assert result.accepted_product_plan_lineage["pfe_001_hash"] == pfe_hash
    assert result.implementation_tasks_created == 0
    assert result.implementation_executions == 0


def test_prd_dec_001_keeps_other_decisions_unresolved(tmp_path: Path) -> None:
    plan_path, ppa_path, pfe_path, plan_hash, ppa_hash, pfe_hash = write_decision_inputs(tmp_path)

    result = record_prd_dec_001(
        plan_path=plan_path,
        ppa_path=ppa_path,
        pfe_path=pfe_path,
        artifacts_dir=tmp_path / "artifacts",
        output_dir=tmp_path / "compiled",
        repository_root=tmp_path,
        decided_at="2026-09-10T12:00:00+00:00",
        expected_plan_hash=plan_hash,
        expected_ppa_acceptance_hash=ppa_hash,
        expected_pfe_hash=pfe_hash,
    )

    assert result.prd_dec_002_state == "UNRESOLVED_MUST_RESOLVE_BEFORE_AFFECTED_TASK"
    assert result.prd_dec_003_state == "UNRESOLVED_MUST_RESOLVE_BEFORE_AFFECTED_TASK"
    assert "No productization human gate is approved." in result.non_authorizations
    assert "No productization implementation is authorized." in result.non_authorizations


def test_prd_dec_001_writes_artifacts_and_compiled_projection(tmp_path: Path) -> None:
    plan_path, ppa_path, pfe_path, plan_hash, ppa_hash, pfe_hash = write_decision_inputs(tmp_path)

    result = record_prd_dec_001(
        plan_path=plan_path,
        ppa_path=ppa_path,
        pfe_path=pfe_path,
        artifacts_dir=tmp_path / "artifacts",
        output_dir=tmp_path / "compiled",
        repository_root=tmp_path,
        decided_at="2026-09-10T12:00:00+00:00",
        expected_plan_hash=plan_hash,
        expected_ppa_acceptance_hash=ppa_hash,
        expected_pfe_hash=pfe_hash,
    )
    artifact = json.loads(
        (tmp_path / "artifacts" / "product-decisions" / "PRD-DEC-001.json").read_text()
    )
    projection = json.loads((tmp_path / "compiled" / "product-decision-records.json").read_text())

    assert artifact["decision_hash"] == result.decision_hash
    assert projection["decisions"][0]["decision_hash"] == result.decision_hash


def test_prd_dec_001_is_idempotent_for_same_decision(tmp_path: Path) -> None:
    plan_path, ppa_path, pfe_path, plan_hash, ppa_hash, pfe_hash = write_decision_inputs(tmp_path)
    kwargs = {
        "plan_path": plan_path,
        "ppa_path": ppa_path,
        "pfe_path": pfe_path,
        "artifacts_dir": tmp_path / "artifacts",
        "output_dir": tmp_path / "compiled",
        "repository_root": tmp_path,
        "expected_plan_hash": plan_hash,
        "expected_ppa_acceptance_hash": ppa_hash,
        "expected_pfe_hash": pfe_hash,
    }

    first = record_prd_dec_001(decided_at="2026-09-10T12:00:00+00:00", **kwargs)
    second = record_prd_dec_001(decided_at="2026-09-10T12:05:00+00:00", **kwargs)

    assert first.result == "RECORDED"
    assert second.result == "ALREADY_RECORDED"
    assert second.decided_at == first.decided_at
    assert second.decision_hash == first.decision_hash


def test_prd_dec_001_blocks_on_wrong_pfe_hash(tmp_path: Path) -> None:
    plan_path, ppa_path, pfe_path, plan_hash, ppa_hash, _pfe_hash = write_decision_inputs(tmp_path)

    result = record_prd_dec_001(
        plan_path=plan_path,
        ppa_path=ppa_path,
        pfe_path=pfe_path,
        artifacts_dir=tmp_path / "artifacts",
        output_dir=tmp_path / "compiled",
        repository_root=tmp_path,
        decided_at="2026-09-10T12:00:00+00:00",
        expected_plan_hash=plan_hash,
        expected_ppa_acceptance_hash=ppa_hash,
        expected_pfe_hash="wrong",
    )

    assert result.result == "BLOCKED"
    assert any(finding["id"] == "PRD-DEC-001-PFE-HASH" for finding in result.validation_findings)


def test_pfe_input_helper_uses_available_environment() -> None:
    assert available_environment()["react_typescript_vite"] == {"status": "AVAILABLE"}
