from __future__ import annotations

import hashlib
from pathlib import Path

from ai_ent.canonical_project_model import CanonicalProjectModel, compile_canonical_project_model
from ai_ent.deterministic_validator import (
    DeterministicValidatorService,
    GeneratedArtifact,
)
from ai_ent.project_manifest import canonical_bytes

MANIFEST_ROOT = Path("manifest/project/ai-ent")


def test_fr_003_validates_source_model_relationships_and_generated_artifacts() -> None:
    model = compile_canonical_project_model(MANIFEST_ROOT)
    artifact = _artifact(model, "GEN-FR-003", payload={"requirement": "FR-003"})

    report = DeterministicValidatorService().validate_model(model, artifacts=(artifact,))
    checks = {check.check_id: check for check in report.checks}

    assert report.ok
    assert checks["DV-FR-003-SOURCE"].status == "PASS"
    assert checks["DV-FR-003-MODEL-HASH"].status == "PASS"
    assert checks["DV-FR-003-RELATIONSHIPS"].status == "PASS"
    assert checks["DV-FR-003-ARTIFACTS"].status == "PASS"
    assert report.generated_artifacts[0]["actual_payload_hash"] == artifact.payload_hash
    assert report.authoritative_artifact_ids == ("GEN-FR-003",)
    assert "FR-003" in report.as_dict()["summary"]["requirements_covered"]


def test_fr_003_rejects_stale_or_mutated_generated_artifact_claims() -> None:
    model = compile_canonical_project_model(MANIFEST_ROOT)
    artifact = _artifact(
        model,
        "GEN-FR-003-STALE",
        source_model_hash="0" * 64,
        claimed_payload_hash="1" * 64,
    )

    report = DeterministicValidatorService().validate_model(model, artifacts=(artifact,))
    checks = {check.check_id: check for check in report.checks}

    assert not report.ok
    assert checks["DV-FR-003-ARTIFACTS"].status == "FAIL"
    assert report.authoritative_artifact_ids == ()
    assert any(finding.requirement_id == "FR-003" for finding in report.findings)


def test_nfr_004_validation_report_is_reproducible_for_canonical_inputs() -> None:
    model = compile_canonical_project_model(MANIFEST_ROOT)
    first_artifact = _artifact(model, "GEN-NFR-004-A", payload={"requirement": "NFR-004"})
    second_artifact = _artifact(model, "GEN-NFR-004-B", payload={"requirement": "NFR-004"})
    service = DeterministicValidatorService()

    first = service.validate_model(model, artifacts=(second_artifact, first_artifact))
    second = service.validate_model(model, artifacts=(first_artifact, second_artifact))
    checks = {check.check_id: check for check in first.checks}

    assert first.report_hash == second.report_hash
    assert first.as_dict() == second.as_dict()
    assert checks["DV-NFR-004-REPORT"].status == "PASS"
    assert [artifact["artifact_id"] for artifact in first.generated_artifacts] == [
        "GEN-NFR-004-A",
        "GEN-NFR-004-B",
    ]


def test_sec_001_ai_output_cannot_claim_authority_before_validation() -> None:
    model = compile_canonical_project_model(MANIFEST_ROOT)
    artifact = _artifact(
        model,
        "AI-SEC-001",
        producer="codex",
        claimed_authoritative=True,
        payload={"requirement": "SEC-001", "candidate": "model-produced"},
    )

    report = DeterministicValidatorService().validate_model(model, artifacts=(artifact,))
    checks = {check.check_id: check for check in report.checks}

    assert not report.ok
    assert checks["DV-SEC-001-AI-AUTHORITY"].status == "FAIL"
    assert report.authoritative_artifact_ids == ()
    assert report.artifact_authority[0].authority_state == "NON_AUTHORITATIVE"
    assert any(
        finding.requirement_id == "SEC-001" and "claims authority" in finding.message
        for finding in report.findings
    )


def _artifact(
    model: CanonicalProjectModel,
    artifact_id: str,
    *,
    payload: dict[str, object] | None = None,
    producer: str = "deterministic",
    source_model_hash: str | None = None,
    claimed_payload_hash: str | None = None,
    claimed_authoritative: bool = False,
) -> GeneratedArtifact:
    payload = {
        "model_hash": model.model_hash,
        "relationship_count": len(model.relationships),
        **(payload or {}),
    }
    return GeneratedArtifact(
        artifact_id=artifact_id,
        artifact_type="validation-evidence",
        source_model_hash=source_model_hash or model.model_hash,
        payload=payload,
        producer=producer,
        claimed_payload_hash=(
            claimed_payload_hash or hashlib.sha256(canonical_bytes(payload)).hexdigest()
        ),
        claimed_authoritative=claimed_authoritative,
    )
