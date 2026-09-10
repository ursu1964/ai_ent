from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ai_ent.governance_contract import (
    GovernanceEvolutionContract,
    GovernanceEvolutionDecision,
    GovernanceRequestInput,
    default_governance_evolution_contract,
    validate_governance_evolution_request,
)
from ai_ent.project_manifest import (
    GENERATED_MARKER,
    PROJECT_MANIFEST_ROOT,
    CompilationResult,
    canonical_bytes,
    compile_project_manifest,
)

GOVERNANCE_EVOLUTION_SERVICE_VERSION = "c08.2"
GOVERNANCE_EVOLUTION_OUTPUT_SCHEMA_VERSION = "governance-evolution-service-output-v0.1"
GOVERNANCE_EVOLUTION_SERVICE_NAME = "governance-evolution-service"
GOVERNANCE_EVOLUTION_COMPONENT_ID = "CMP-C08"
GOVERNANCE_EVOLUTION_CAPABILITY_ID = "C08"
GOVERNANCE_EVOLUTION_OUTPUT_AUTHORITY_STATE = "NON_AUTHORITATIVE"

GovernanceEvolutionStatus = Literal["RECOMMENDED", "REJECTED"]
GovernanceRecommendationKind = Literal["MANIFEST_POLICY_RECOMMENDATION"]
GovernanceEvidenceKind = Literal["GOVERNANCE_EVOLUTION_DECISION"]


@dataclass(frozen=True)
class GovernanceManifestPolicy:
    manifest_root: str
    source_compiled_hash: str
    capability_id: str
    capability_status: str
    capability_maturity: str
    capability_source_refs: tuple[str, ...]
    repository_branch_policy: str
    protected_paths: tuple[str, ...]
    verification_gate_ids: tuple[str, ...]

    @property
    def policy_hash(self) -> str:
        return hashlib.sha256(canonical_bytes(self.as_dict())).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest_root": self.manifest_root,
            "source_compiled_hash": self.source_compiled_hash,
            "capability_id": self.capability_id,
            "capability_status": self.capability_status,
            "capability_maturity": self.capability_maturity,
            "capability_source_refs": list(self.capability_source_refs),
            "repository_branch_policy": self.repository_branch_policy,
            "protected_paths": list(self.protected_paths),
            "verification_gate_ids": list(self.verification_gate_ids),
        }


@dataclass(frozen=True)
class GovernanceServiceBoundary:
    boundary_id: str
    authority: str
    granted: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "boundary_id": self.boundary_id,
            "authority": self.authority,
            "granted": self.granted,
        }


@dataclass(frozen=True)
class GovernanceRecommendation:
    recommendation_id: str
    request_id: str
    recommendation_kind: GovernanceRecommendationKind
    target_manifest_paths: tuple[str, ...]
    allowed_authorities_used: tuple[str, ...]
    required_sequence: tuple[str, ...]
    required_human_approval: bool
    evidence_refs: tuple[str, ...]
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "recommendation_id": self.recommendation_id,
            "request_id": self.request_id,
            "recommendation_kind": self.recommendation_kind,
            "target_manifest_paths": list(self.target_manifest_paths),
            "allowed_authorities_used": list(self.allowed_authorities_used),
            "required_sequence": list(self.required_sequence),
            "required_human_approval": self.required_human_approval,
            "evidence_refs": list(self.evidence_refs),
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class GovernanceEvidenceRecord:
    evidence_id: str
    request_id: str
    evidence_kind: GovernanceEvidenceKind
    request_hash: str
    contract_hash: str
    manifest_policy_hash: str
    output_authority_state: str
    validation_required: str
    evidence_refs: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "request_id": self.request_id,
            "evidence_kind": self.evidence_kind,
            "request_hash": self.request_hash,
            "contract_hash": self.contract_hash,
            "manifest_policy_hash": self.manifest_policy_hash,
            "output_authority_state": self.output_authority_state,
            "validation_required": self.validation_required,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class GovernanceEvolutionServiceResult:
    contract_version: str
    schema_version: str
    status: GovernanceEvolutionStatus
    request_id: str
    request_hash: str
    contract_hash: str
    manifest_policy: GovernanceManifestPolicy
    contract_decision: GovernanceEvolutionDecision
    recommendations: tuple[GovernanceRecommendation, ...]
    evidence_records: tuple[GovernanceEvidenceRecord, ...]
    service_boundaries: tuple[GovernanceServiceBoundary, ...]
    accepted_authorities: tuple[str, ...]
    denied_authorities: tuple[str, ...]
    blockers: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.status == "RECOMMENDED"

    @property
    def output_hash(self) -> str:
        return hashlib.sha256(canonical_bytes(self._payload())).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["output_hash"] = self.output_hash
        return payload

    def _payload(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "component_id": GOVERNANCE_EVOLUTION_COMPONENT_ID,
            "capability_id": GOVERNANCE_EVOLUTION_CAPABILITY_ID,
            "service": GOVERNANCE_EVOLUTION_SERVICE_NAME,
            "contract_version": self.contract_version,
            "schema_version": self.schema_version,
            "status": self.status,
            "request_id": self.request_id,
            "request_hash": self.request_hash,
            "contract_hash": self.contract_hash,
            "manifest_policy": self.manifest_policy.as_dict(),
            "manifest_policy_hash": self.manifest_policy.policy_hash,
            "contract_decision": self.contract_decision.as_dict(),
            "recommendations": [
                recommendation.as_dict()
                for recommendation in sorted(
                    self.recommendations,
                    key=lambda item: item.recommendation_id,
                )
            ],
            "evidence_records": [
                evidence.as_dict()
                for evidence in sorted(self.evidence_records, key=lambda item: item.evidence_id)
            ],
            "service_boundaries": [boundary.as_dict() for boundary in self.service_boundaries],
            "accepted_authorities": list(self.accepted_authorities),
            "denied_authorities": list(self.denied_authorities),
            "blockers": list(self.blockers),
            "summary": {
                "recommended": self.ok,
                "output_authority_state": GOVERNANCE_EVOLUTION_OUTPUT_AUTHORITY_STATE,
                "recommendations_only": True,
                "evidence_only": True,
                "runtime_completion_authority_granted": False,
                "gate_approval_granted": False,
                "verifier_bypass_granted": False,
                "commit_boundary_bypass_granted": False,
                "self_scheduling_granted": False,
                "runtime_execution_granted": False,
                "policy_weakening_granted": False,
                "human_approval_required": True,
            },
        }


class GovernanceEvolutionService:
    """Deterministic C08 advisory service behind the governance contract."""

    def __init__(
        self,
        *,
        contract: GovernanceEvolutionContract | None = None,
        contract_version: str = GOVERNANCE_EVOLUTION_SERVICE_VERSION,
        schema_version: str = GOVERNANCE_EVOLUTION_OUTPUT_SCHEMA_VERSION,
    ) -> None:
        self.contract = contract or default_governance_evolution_contract()
        self.contract_version = contract_version
        self.schema_version = schema_version

    def evaluate(
        self,
        request: GovernanceRequestInput,
        *,
        manifest_root: Path = PROJECT_MANIFEST_ROOT,
    ) -> GovernanceEvolutionServiceResult:
        contract_decision = validate_governance_evolution_request(
            request,
            contract=self.contract,
        )
        compilation = compile_project_manifest(manifest_root)
        manifest_policy = _manifest_policy_snapshot(compilation, self.contract.manifest_authority_root)
        target_manifest_paths = _requested_target_manifest_paths(request)
        denied_authorities = _denied_authorities(
            request,
            contract=self.contract,
            accepted_authorities=contract_decision.accepted_authorities,
        )
        manifest_blockers = tuple(
            f"manifest validation error:{error}"
            for error in compilation.validation.validation_errors
        )
        recommendations = (
            _recommendation(contract_decision, manifest_policy, target_manifest_paths)
            if contract_decision.ok and not manifest_blockers
            else ()
        )
        evidence_records = (
            _evidence_record(contract_decision, manifest_policy),
        )
        blockers = list(contract_decision.blockers)
        blockers.extend(manifest_blockers)
        blockers_tuple = tuple(sorted(dict.fromkeys(blockers)))
        return GovernanceEvolutionServiceResult(
            contract_version=self.contract_version,
            schema_version=self.schema_version,
            status="REJECTED" if blockers_tuple else "RECOMMENDED",
            request_id=contract_decision.request_id,
            request_hash=contract_decision.request_hash,
            contract_hash=contract_decision.contract_hash,
            manifest_policy=manifest_policy,
            contract_decision=contract_decision,
            recommendations=recommendations,
            evidence_records=evidence_records,
            service_boundaries=governance_evolution_service_boundaries(),
            accepted_authorities=contract_decision.accepted_authorities,
            denied_authorities=denied_authorities,
            blockers=blockers_tuple,
        )


GovernanceEvolution = GovernanceEvolutionService


def evaluate_governance_evolution(
    request: GovernanceRequestInput,
    *,
    manifest_root: Path = PROJECT_MANIFEST_ROOT,
    contract: GovernanceEvolutionContract | None = None,
) -> GovernanceEvolutionServiceResult:
    return GovernanceEvolutionService(contract=contract).evaluate(
        request,
        manifest_root=manifest_root,
    )


def governance_evolution_service_boundaries() -> tuple[GovernanceServiceBoundary, ...]:
    return (
        GovernanceServiceBoundary(
            boundary_id="human-gate",
            authority="approve_gate",
            granted=False,
        ),
        GovernanceServiceBoundary(
            boundary_id="deterministic-verifier",
            authority="bypass_verifier",
            granted=False,
        ),
        GovernanceServiceBoundary(
            boundary_id="verified-commit",
            authority="bypass_commit_boundary",
            granted=False,
        ),
        GovernanceServiceBoundary(
            boundary_id="runtime-completion",
            authority="execute_runtime",
            granted=False,
        ),
        GovernanceServiceBoundary(
            boundary_id="runtime-scheduling",
            authority="schedule_execution",
            granted=False,
        ),
        GovernanceServiceBoundary(
            boundary_id="manifest-policy",
            authority="weaken_policy",
            granted=False,
        ),
    )


def _manifest_policy_snapshot(
    compilation: CompilationResult,
    manifest_authority_root: str,
) -> GovernanceManifestPolicy:
    capability = next(
        (
            item
            for item in compilation.compiled.capabilities
            if str(item.get("id", "")) == GOVERNANCE_EVOLUTION_CAPABILITY_ID
        ),
        {},
    )
    repository = compilation.compiled.repository
    verification = compilation.compiled.verification.get("verification", {})
    gate_ids = tuple(sorted(str(gate.get("id", "")) for gate in verification.get("gates", ())))
    return GovernanceManifestPolicy(
        manifest_root=manifest_authority_root,
        source_compiled_hash=compilation.lock.compiled_hash,
        capability_id=GOVERNANCE_EVOLUTION_CAPABILITY_ID,
        capability_status=str(capability.get("status", "")),
        capability_maturity=str(capability.get("maturity", "")),
        capability_source_refs=tuple(sorted(str(ref) for ref in capability.get("source_refs", ()))),
        repository_branch_policy=str(repository.get("repository", {}).get("branch_policy", "")),
        protected_paths=tuple(
            sorted(str(path) for path in repository.get("repository", {}).get("protected_paths", ()))
        ),
        verification_gate_ids=gate_ids,
    )


def _recommendation(
    decision: GovernanceEvolutionDecision,
    manifest_policy: GovernanceManifestPolicy,
    target_manifest_paths: tuple[str, ...],
) -> tuple[GovernanceRecommendation, ...]:
    payload = {
        "request_id": decision.request_id,
        "request_hash": decision.request_hash,
        "contract_hash": decision.contract_hash,
        "manifest_policy_hash": manifest_policy.policy_hash,
        "accepted_authorities": list(decision.accepted_authorities),
    }
    recommendation_hash = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    return (
        GovernanceRecommendation(
            recommendation_id=f"GOV-REC-{recommendation_hash[:12]}",
            request_id=decision.request_id,
            recommendation_kind="MANIFEST_POLICY_RECOMMENDATION",
            target_manifest_paths=target_manifest_paths,
            allowed_authorities_used=decision.accepted_authorities,
            required_sequence=decision.required_evolution_sequence,
            required_human_approval=True,
            evidence_refs=(
                f"GOV-EVIDENCE:{decision.request_hash}",
                f"GOV-CONTRACT:{decision.contract_hash}",
                f"GOV-MANIFEST-POLICY:{manifest_policy.policy_hash}",
            ),
            rationale="Advisory governance evolution is bounded to manifest policy and remains non-authoritative.",
        ),
    )


def _evidence_record(
    decision: GovernanceEvolutionDecision,
    manifest_policy: GovernanceManifestPolicy,
) -> GovernanceEvidenceRecord:
    payload = {
        "request_id": decision.request_id,
        "request_hash": decision.request_hash,
        "contract_hash": decision.contract_hash,
        "manifest_policy_hash": manifest_policy.policy_hash,
        "status": decision.status,
    }
    evidence_hash = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    return GovernanceEvidenceRecord(
        evidence_id=f"GOV-EVID-{evidence_hash[:12]}",
        request_id=decision.request_id,
        evidence_kind="GOVERNANCE_EVOLUTION_DECISION",
        request_hash=decision.request_hash,
        contract_hash=decision.contract_hash,
        manifest_policy_hash=manifest_policy.policy_hash,
        output_authority_state=GOVERNANCE_EVOLUTION_OUTPUT_AUTHORITY_STATE,
        validation_required="deterministic validation, human approval, and verified commit finalization remain external",
        evidence_refs=(
            f"GOV-CONTRACT:{decision.contract_hash}",
            f"GOV-MANIFEST-POLICY:{manifest_policy.policy_hash}",
            f"GOV-REQUEST:{decision.request_hash}",
        ),
    )


def _denied_authorities(
    request: GovernanceRequestInput,
    *,
    contract: GovernanceEvolutionContract,
    accepted_authorities: tuple[str, ...],
) -> tuple[str, ...]:
    raw = _raw_request(request)
    requested = raw.get("requested_authorities", ()) if isinstance(raw, dict) else ()
    accepted = set(accepted_authorities)
    allowed = set(contract.allowed_authorities)
    return tuple(
        sorted(
            str(authority)
            for authority in _raw_sequence(requested)
            if str(authority) not in accepted and str(authority) not in allowed
        )
    )


def _requested_target_manifest_paths(request: GovernanceRequestInput) -> tuple[str, ...]:
    raw = _raw_request(request)
    targets = raw.get("target_manifest_paths", ()) if isinstance(raw, dict) else ()
    return tuple(sorted(str(path).strip().strip("/") for path in _raw_sequence(targets)))


def _raw_request(request: GovernanceRequestInput) -> dict[str, Any]:
    if isinstance(request, dict):
        return request
    return request.as_dict()


def _raw_sequence(value: object) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set)):
        return tuple(value)
    return (value,)
