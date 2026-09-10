from __future__ import annotations

import json
from pathlib import Path

from ai_ent.productization_plan import (
    PRD_PLAN_ID,
    evaluate_productization_plan,
    write_productization_plan,
)


def write_accepted_evidence(root: Path) -> None:
    saag = root / "saag-001"
    e2e = root / "e2e-001"
    pir = root / "pir-002"
    saag.mkdir(parents=True)
    e2e.mkdir(parents=True)
    pir.mkdir(parents=True)
    (saag / "SAAG-001.json").write_text(
        json.dumps(
            {
                "result": "ACCEPTED_WITH_LIMITATIONS",
                "recommendation": "READY_FOR_FIRST_REAL_APPLICATION_CREATION_PROOF",
                "head": "head-saag",
                "acceptance_hash": "hash-saag",
                "proofs": [{} for _ in range(13)],
                "negative_tests": [{} for _ in range(6)],
                "limitations": [
                    {
                        "classification": "FUTURE_HARDENING",
                        "description": "human-gate DB rows need richer approval scope documents",
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (e2e / "E2E-001.json").write_text(
        json.dumps(
            {
                "result": "ACCEPTED_WITH_LIMITATIONS",
                "recommendation": "READY_FOR_PRODUCTIZATION_PLANNING",
                "head": "head-e2e",
                "target_project_id": "E2E-TEAM-WORK-TRACKER",
                "target_workspace": "/tmp/team-work-tracker",
                "application_commit": "commit-e2e",
                "hashes": {"acceptance_hash": "hash-e2e"},
                "proofs": [{} for _ in range(13)],
                "limitations": [
                    {
                        "classification": "FUTURE_HARDENING",
                        "description": "target-app E2E harness should become reusable",
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (pir / "PIR-002.json").write_text(
        json.dumps(
            {
                "result": "NO_RESIDUAL_IMPLEMENTATION_GAPS",
                "recommendation": "READY_FOR_SYSTEM_ACCEPTANCE_GATE",
                "hashes": {"pir_002_trace_hash": "trace"},
                "residual_gaps": [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def test_prd_001_accepts_productization_plan(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    write_accepted_evidence(artifacts)

    result = evaluate_productization_plan(artifacts_dir=artifacts)
    payload = result.as_dict()
    dag = payload["productization_dag"]

    assert result.result == "ACCEPTED_WITH_LIMITATIONS"
    assert result.recommendation == "READY_FOR_PRODUCT_PLAN_ACCEPTANCE"
    assert dag["plan_id"] == PRD_PLAN_ID
    assert dag["summary"]["task_count"] == 25
    assert dag["summary"]["dependency_edges"] == 51
    assert dag["summary"]["human_gates"] == 8
    assert dag["theoretical_parallel_width"] >= 2
    assert {task["task_id"] for task in dag["tasks"]} == {
        f"PRD-TASK-{index:03d}" for index in range(1, 26)
    }


def test_prd_001_preserves_control_plane_authority(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    write_accepted_evidence(artifacts)

    result = evaluate_productization_plan(artifacts_dir=artifacts)
    payload = result.as_dict()

    assert "cannot bypass gates" in payload["architecture"]["authority_rule"]
    assert payload["approval_model"]["first_class_objects"] is True
    assert "agent self-approval" in payload["approval_model"]["prohibited"]
    assert payload["evidence_artifact_model"]["authority"].startswith("Artifact Evidence Graph is NON_AUTHORITATIVE")


def test_prd_001_external_project_runtime_is_generic(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    write_accepted_evidence(artifacts)

    result = evaluate_productization_plan(artifacts_dir=artifacts)
    runtime = result.external_project_runtime
    carried = result.bound_evidence["e2e_001"]

    assert carried["target_project_id"] == "E2E-TEAM-WORK-TRACKER"
    assert runtime["genericity_rule"].startswith("No E2E-TEAM-WORK-TRACKER-specific")
    assert "ExternalProject" in runtime["entities"]
    assert runtime["lifecycle"] == [
        "CREATE",
        "INTAKE",
        "PLAN",
        "FREEZE",
        "APPROVE",
        "IMPORT",
        "EXECUTE",
        "VERIFY",
        "COMPLETE",
        "RUN",
        "ARCHIVE",
    ]


def test_prd_001_promotes_future_hardening_to_requirements(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    write_accepted_evidence(artifacts)

    result = evaluate_productization_plan(artifacts_dir=artifacts)
    requirement_text = "\n".join(item["requirement"] for item in result.promoted_requirements)

    assert "reusable external-project runtime importer" in requirement_text
    assert "approval scope documents" in requirement_text
    assert "target-app E2E harness should become reusable" in requirement_text


def test_prd_001_writer_is_deterministic(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    output_dir = tmp_path / "compiled"
    write_accepted_evidence(artifacts)

    first = write_productization_plan(artifacts_dir=artifacts, output_dir=output_dir)
    first_json = (artifacts / "prd-001" / "PRD-001.json").read_bytes()
    first_projection = (output_dir / "productization-plan.json").read_bytes()
    second = write_productization_plan(artifacts_dir=artifacts, output_dir=output_dir)

    assert first.plan_hash == second.plan_hash
    assert (artifacts / "prd-001" / "PRD-001.json").read_bytes() == first_json
    assert (output_dir / "productization-plan.json").read_bytes() == first_projection


def test_prd_001_rejects_without_accepted_system_evidence(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    write_accepted_evidence(artifacts)
    payload = json.loads((artifacts / "saag-001" / "SAAG-001.json").read_text())
    payload["result"] = "REJECTED"
    (artifacts / "saag-001" / "SAAG-001.json").write_text(json.dumps(payload), encoding="utf-8")

    result = evaluate_productization_plan(artifacts_dir=artifacts)

    assert result.result == "REJECTED"
    assert result.recommendation == "REMEDIATION_REQUIRED"
