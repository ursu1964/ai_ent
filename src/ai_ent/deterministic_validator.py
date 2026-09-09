from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ai_ent.canonical_project_model import (
    CANONICAL_PROJECT_MODEL_CONTRACT_VERSION,
    CANONICAL_PROJECT_MODEL_SCHEMA_VERSION,
    CanonicalProjectModel,
    CanonicalProjectModelService,
    CanonicalProjectRelationship,
)
from ai_ent.project_manifest import PROJECT_MANIFEST_ROOT, canonical_bytes

DETERMINISTIC_VALIDATOR_VERSION = "c03.1"

ValidationSeverity = Literal["ERROR", "WARNING", "INFO"]
ValidationStatus = Literal["PASS", "FAIL"]
ArtifactAuthorityState = Literal["VALIDATED", "NON_AUTHORITATIVE"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_AI_PRODUCERS = {"ai", "agent", "codex", "llm", "model"}


@dataclass(frozen=True)
class GeneratedArtifact:
    artifact_id: str
    artifact_type: str
    source_model_hash: str
    payload: dict[str, Any]
    producer: str = "deterministic"
    claimed_payload_hash: str | None = None
    claimed_authoritative: bool = False

    @property
    def payload_hash(self) -> str:
        return hashlib.sha256(canonical_bytes(self.payload)).hexdigest()

    def as_report_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "producer": self.producer,
            "source_model_hash": self.source_model_hash,
            "claimed_payload_hash": self.claimed_payload_hash,
            "actual_payload_hash": self.payload_hash,
            "claimed_authoritative": self.claimed_authoritative,
        }


@dataclass(frozen=True)
class DeterministicValidationFinding:
    finding_id: str
    severity: ValidationSeverity
    requirement_id: str
    subject_id: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "severity": self.severity,
            "requirement_id": self.requirement_id,
            "subject_id": self.subject_id,
            "message": self.message,
        }


@dataclass(frozen=True)
class DeterministicValidationCheck:
    check_id: str
    requirement_id: str
    subject_id: str
    status: ValidationStatus
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "requirement_id": self.requirement_id,
            "subject_id": self.subject_id,
            "status": self.status,
            "message": self.message,
        }


@dataclass(frozen=True)
class ArtifactAuthorityDecision:
    artifact_id: str
    authority_state: ArtifactAuthorityState
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "authority_state": self.authority_state,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DeterministicValidationReport:
    source_compiled_hash: str
    model_hash: str
    validator_version: str
    report_hash: str
    checks: tuple[DeterministicValidationCheck, ...]
    findings: tuple[DeterministicValidationFinding, ...]
    generated_artifacts: tuple[dict[str, Any], ...]
    artifact_authority: tuple[ArtifactAuthorityDecision, ...]

    @property
    def ok(self) -> bool:
        return not any(finding.severity == "ERROR" for finding in self.findings)

    @property
    def authoritative_artifact_ids(self) -> tuple[str, ...]:
        return tuple(
            decision.artifact_id
            for decision in self.artifact_authority
            if decision.authority_state == "VALIDATED"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_compiled_hash": self.source_compiled_hash,
            "model_hash": self.model_hash,
            "validator_version": self.validator_version,
            "report_hash": self.report_hash,
            "checks": [check.as_dict() for check in self.checks],
            "findings": [finding.as_dict() for finding in self.findings],
            "generated_artifacts": list(self.generated_artifacts),
            "artifact_authority": [decision.as_dict() for decision in self.artifact_authority],
            "summary": {
                "ok": self.ok,
                "error_count": _finding_count(self.findings, "ERROR"),
                "warning_count": _finding_count(self.findings, "WARNING"),
                "info_count": _finding_count(self.findings, "INFO"),
                "checks_passed": len([check for check in self.checks if check.status == "PASS"]),
                "checks_failed": len([check for check in self.checks if check.status == "FAIL"]),
                "artifacts_validated": len(self.authoritative_artifact_ids),
                "authoritative_artifacts": list(self.authoritative_artifact_ids),
                "requirements_covered": sorted({check.requirement_id for check in self.checks}),
            },
        }


ArtifactInput = GeneratedArtifact | dict[str, Any]


class DeterministicValidatorService:
    """Service boundary for the C03 Deterministic Validator contract."""

    def __init__(self, *, validator_version: str = DETERMINISTIC_VALIDATOR_VERSION) -> None:
        self.validator_version = validator_version

    def validate(
        self,
        root: Path = PROJECT_MANIFEST_ROOT,
        *,
        artifacts: tuple[ArtifactInput, ...] = (),
    ) -> DeterministicValidationReport:
        model = CanonicalProjectModelService().compile(root)
        return self.validate_model(model, artifacts=artifacts)

    def validate_model(
        self,
        model: CanonicalProjectModel,
        *,
        artifacts: tuple[ArtifactInput, ...] = (),
    ) -> DeterministicValidationReport:
        normalized_artifacts = tuple(sorted(
            (_artifact_from_raw(artifact) for artifact in artifacts),
            key=lambda item: item.artifact_id,
        ))
        findings: list[DeterministicValidationFinding] = []
        checks: list[DeterministicValidationCheck] = []

        _validate_source(model, findings, checks)
        _validate_model_hashes(model, findings, checks)
        _validate_relationships(model, findings, checks)
        artifact_error_ids = _validate_artifacts(model, normalized_artifacts, findings, checks)
        _validate_ai_authority(normalized_artifacts, findings, checks, artifact_error_ids)

        sorted_checks = tuple(sorted(checks, key=lambda item: item.check_id))
        sorted_findings = tuple(sorted(findings, key=lambda item: item.finding_id))
        report_artifacts = tuple(artifact.as_report_dict() for artifact in normalized_artifacts)
        has_error = any(finding.severity == "ERROR" for finding in sorted_findings)
        authority = tuple(
            ArtifactAuthorityDecision(
                artifact.artifact_id,
                (
                    "NON_AUTHORITATIVE"
                    if has_error or artifact.artifact_id in artifact_error_ids
                    else "VALIDATED"
                ),
                (
                    "deterministic validation failed"
                    if has_error or artifact.artifact_id in artifact_error_ids
                    else "deterministic validation passed"
                ),
            )
            for artifact in normalized_artifacts
        )
        payload = _report_payload(
            model,
            self.validator_version,
            sorted_checks,
            sorted_findings,
            report_artifacts,
            authority,
        )
        return DeterministicValidationReport(
            source_compiled_hash=model.source_compiled_hash,
            model_hash=model.model_hash,
            validator_version=self.validator_version,
            report_hash=hashlib.sha256(canonical_bytes(payload)).hexdigest(),
            checks=sorted_checks,
            findings=sorted_findings,
            generated_artifacts=report_artifacts,
            artifact_authority=authority,
        )


def validate_deterministically(
    root: Path = PROJECT_MANIFEST_ROOT,
    *,
    artifacts: tuple[ArtifactInput, ...] = (),
) -> DeterministicValidationReport:
    return DeterministicValidatorService().validate(root, artifacts=artifacts)


def _validate_source(
    model: CanonicalProjectModel,
    findings: list[DeterministicValidationFinding],
    checks: list[DeterministicValidationCheck],
) -> None:
    failures: list[str] = []
    if not _is_sha256(model.source_compiled_hash):
        failures.append("source compiled hash is not a canonical sha256 digest")
    if model.schema_version != CANONICAL_PROJECT_MODEL_SCHEMA_VERSION:
        failures.append(f"schema version mismatch: {model.schema_version}")
    if model.contract_version != CANONICAL_PROJECT_MODEL_CONTRACT_VERSION:
        failures.append(f"contract version mismatch: {model.contract_version}")

    _append_check(
        checks,
        findings,
        "DV-FR-003-SOURCE",
        "FR-003",
        model.model_id,
        failures,
        "source and canonical model contract are valid",
    )


def _validate_model_hashes(
    model: CanonicalProjectModel,
    findings: list[DeterministicValidationFinding],
    checks: list[DeterministicValidationCheck],
) -> None:
    failures: list[str] = []
    expected_model_hash = _model_hash(model)
    if model.model_hash != expected_model_hash:
        failures.append("canonical model hash does not match model payload")

    for item in model.objects:
        expected = hashlib.sha256(canonical_bytes(item.payload)).hexdigest()
        if item.payload_hash != expected:
            failures.append(f"object payload hash mismatch:{item.object_id}")

    for relationship in model.relationships:
        expected_payload_hash = hashlib.sha256(canonical_bytes(relationship.payload)).hexdigest()
        expected_id = _relationship_id(relationship)
        if relationship.payload_hash != expected_payload_hash:
            failures.append(f"relationship payload hash mismatch:{relationship.relationship_id}")
        if relationship.relationship_id != expected_id:
            failures.append(f"relationship id mismatch:{relationship.relationship_id}")

    _append_check(
        checks,
        findings,
        "DV-FR-003-MODEL-HASH",
        "FR-003",
        model.model_id,
        failures,
        "model, object, and relationship hashes are reproducible",
    )


def _validate_relationships(
    model: CanonicalProjectModel,
    findings: list[DeterministicValidationFinding],
    checks: list[DeterministicValidationCheck],
) -> None:
    failures: list[str] = []
    object_ids = {item.object_id for item in model.objects}
    object_by_id = {item.object_id: item for item in model.objects}
    object_id_counts = {item.object_id: 0 for item in model.objects}
    relationship_id_counts = {item.relationship_id: 0 for item in model.relationships}
    for item in model.objects:
        object_id_counts[item.object_id] += 1
    for item in model.relationships:
        relationship_id_counts[item.relationship_id] += 1
    duplicate_object_ids = sorted(
        object_id for object_id, count in object_id_counts.items() if count > 1
    )
    duplicate_relationship_ids = sorted(
        relationship_id for relationship_id, count in relationship_id_counts.items() if count > 1
    )
    failures.extend(f"duplicate object id:{object_id}" for object_id in duplicate_object_ids)
    failures.extend(
        f"duplicate relationship id:{relationship_id}"
        for relationship_id in duplicate_relationship_ids
    )

    for relationship in model.relationships:
        if relationship.source_object_id not in object_ids:
            failures.append(f"relationship missing source:{relationship.relationship_id}")
            continue
        if relationship.target_object_id not in object_ids:
            failures.append(f"relationship missing target:{relationship.relationship_id}")
            continue
        if object_by_id[relationship.source_object_id].native_id != relationship.source_native_id:
            failures.append(
                f"relationship source native id mismatch:{relationship.relationship_id}"
            )
        if object_by_id[relationship.target_object_id].native_id != relationship.target_native_id:
            failures.append(
                f"relationship target native id mismatch:{relationship.relationship_id}"
            )

    relationships_by_requirement = _relationships_by_requirement(model.relationships)
    for requirement in model.objects_by_type("requirement"):
        if requirement.classification == "FUTURE/DEFERRED":
            continue
        relationship_types = relationships_by_requirement.get(requirement.object_id, set())
        if "SOURCED_FROM" not in relationship_types:
            failures.append(f"requirement has no source trace:{requirement.native_id}")
        if "REQUIRES_CAPABILITY" not in relationship_types:
            failures.append(f"requirement has no capability trace:{requirement.native_id}")

    _append_check(
        checks,
        findings,
        "DV-FR-003-RELATIONSHIPS",
        "FR-003",
        model.model_id,
        failures,
        "relationships resolve to canonical objects and required traces",
    )


def _validate_artifacts(
    model: CanonicalProjectModel,
    artifacts: tuple[GeneratedArtifact, ...],
    findings: list[DeterministicValidationFinding],
    checks: list[DeterministicValidationCheck],
) -> set[str]:
    failures: list[str] = []
    artifact_error_ids: set[str] = set()
    duplicate_ids = sorted(
        {
            artifact.artifact_id
            for artifact in artifacts
            if [candidate.artifact_id for candidate in artifacts].count(artifact.artifact_id) > 1
        }
    )
    for artifact_id in duplicate_ids:
        failures.append(f"duplicate generated artifact id:{artifact_id}")
        artifact_error_ids.add(artifact_id)

    for artifact in artifacts:
        artifact_failures: list[str] = []
        if not artifact.artifact_id.strip():
            artifact_failures.append("artifact id is required")
        if not artifact.artifact_type.strip():
            artifact_failures.append("artifact type is required")
        if artifact.source_model_hash != model.model_hash:
            artifact_failures.append("artifact source model hash does not match canonical model")
        if (
            artifact.claimed_payload_hash is not None
            and artifact.claimed_payload_hash != artifact.payload_hash
        ):
            artifact_failures.append("artifact claimed payload hash does not match payload")
        if artifact_failures:
            artifact_error_ids.add(artifact.artifact_id)
            failures.extend(f"{artifact.artifact_id}:{failure}" for failure in artifact_failures)

    _append_check(
        checks,
        findings,
        "DV-FR-003-ARTIFACTS",
        "FR-003",
        model.model_id,
        failures,
        "generated artifacts are bound to the canonical model and payload hashes",
    )
    _append_check(
        checks,
        findings,
        "DV-NFR-004-REPORT",
        "NFR-004",
        model.model_id,
        [],
        "validation report is derived only from canonical sorted inputs",
    )
    return artifact_error_ids


def _validate_ai_authority(
    artifacts: tuple[GeneratedArtifact, ...],
    findings: list[DeterministicValidationFinding],
    checks: list[DeterministicValidationCheck],
    artifact_error_ids: set[str],
) -> None:
    failures: list[str] = []
    for artifact in artifacts:
        if _is_ai_producer(artifact.producer) and artifact.claimed_authoritative:
            artifact_error_ids.add(artifact.artifact_id)
            failures.append(
                f"{artifact.artifact_id}:AI artifact claims authority before validation"
            )
    _append_check(
        checks,
        findings,
        "DV-SEC-001-AI-AUTHORITY",
        "SEC-001",
        "generated-artifacts",
        failures,
        "AI-produced artifacts remain non-authoritative until deterministic validation passes",
    )


def _append_check(
    checks: list[DeterministicValidationCheck],
    findings: list[DeterministicValidationFinding],
    check_id: str,
    requirement_id: str,
    subject_id: str,
    failures: list[str],
    pass_message: str,
) -> None:
    if not failures:
        checks.append(
            DeterministicValidationCheck(check_id, requirement_id, subject_id, "PASS", pass_message)
        )
        return
    checks.append(
        DeterministicValidationCheck(
            check_id,
            requirement_id,
            subject_id,
            "FAIL",
            "; ".join(failures),
        )
    )
    for index, failure in enumerate(sorted(failures), start=1):
        findings.append(
            DeterministicValidationFinding(
                f"{check_id}-F{index:03d}",
                "ERROR",
                requirement_id,
                subject_id,
                failure,
            )
        )


def _report_payload(
    model: CanonicalProjectModel,
    validator_version: str,
    checks: tuple[DeterministicValidationCheck, ...],
    findings: tuple[DeterministicValidationFinding, ...],
    generated_artifacts: tuple[dict[str, Any], ...],
    artifact_authority: tuple[ArtifactAuthorityDecision, ...],
) -> dict[str, Any]:
    return {
        "source_compiled_hash": model.source_compiled_hash,
        "model_hash": model.model_hash,
        "validator_version": validator_version,
        "checks": [check.as_dict() for check in checks],
        "findings": [finding.as_dict() for finding in findings],
        "generated_artifacts": list(generated_artifacts),
        "artifact_authority": [decision.as_dict() for decision in artifact_authority],
    }


def _model_hash(model: CanonicalProjectModel) -> str:
    payload = {
        "schema_version": model.schema_version,
        "contract_version": model.contract_version,
        "compiler_version": model.compiler_version,
        "source_compiled_hash": model.source_compiled_hash,
        "objects": [item.as_dict() for item in model.objects],
        "relationships": [item.as_dict() for item in model.relationships],
    }
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _relationship_id(relationship: CanonicalProjectRelationship) -> str:
    payload = {
        "relationship_type": relationship.relationship_type,
        "source_object_id": relationship.source_object_id,
        "target_object_id": relationship.target_object_id,
        "provenance_refs": sorted(relationship.provenance_refs),
        **relationship.payload,
    }
    return f"relationship:{hashlib.sha256(canonical_bytes(payload)).hexdigest()[:32]}"


def _relationships_by_requirement(
    relationships: tuple[CanonicalProjectRelationship, ...],
) -> dict[str, set[str]]:
    by_requirement: dict[str, set[str]] = {}
    for relationship in relationships:
        if relationship.source_object_id.startswith("requirement:"):
            by_requirement.setdefault(relationship.source_object_id, set()).add(
                relationship.relationship_type
            )
    return by_requirement


def _artifact_from_raw(raw: ArtifactInput) -> GeneratedArtifact:
    if isinstance(raw, GeneratedArtifact):
        return raw
    return GeneratedArtifact(
        artifact_id=str(raw.get("artifact_id", "")),
        artifact_type=str(raw.get("artifact_type", "")),
        source_model_hash=str(raw.get("source_model_hash", "")),
        payload=dict(raw.get("payload", {})),
        producer=str(raw.get("producer", "deterministic")),
        claimed_payload_hash=_optional_string(raw.get("claimed_payload_hash")),
        claimed_authoritative=bool(raw.get("claimed_authoritative", False)),
    )


def _is_sha256(value: str) -> bool:
    return _SHA256_RE.match(value) is not None


def _is_ai_producer(producer: str) -> bool:
    normalized = producer.strip().lower()
    return normalized in _AI_PRODUCERS or normalized.startswith("ai-")


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def _finding_count(
    findings: tuple[DeterministicValidationFinding, ...],
    severity: ValidationSeverity,
) -> int:
    return len([finding for finding in findings if finding.severity == severity])
