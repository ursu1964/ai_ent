from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal

from ai_ent.project_manifest import GENERATED_MARKER, canonical_bytes

GOVERNANCE_EVOLUTION_CONTRACT_VERSION = "c08.1"
GOVERNANCE_EVOLUTION_SCHEMA_VERSION = "governance-evolution-contract-v0.1"

GovernanceDecisionStatus = Literal["ACCEPTED", "REJECTED"]

ALLOWED_GOVERNANCE_DOMAINS: tuple[str, ...] = (
    "ai",
    "business",
    "compliance",
    "data",
    "evolution",
    "operations",
    "security",
    "technical",
)
ALLOWED_EVOLUTION_TYPES: tuple[str, ...] = (
    "ai",
    "business",
    "compliance",
    "infrastructure",
    "performance",
    "security",
    "technical",
)
ALLOWED_GOVERNANCE_AUTHORITIES: tuple[str, ...] = (
    "analyze_impact",
    "learn_from_approved_sources",
    "recognize_patterns",
    "recommend",
    "record_evidence",
    "simulate",
)
PROHIBITED_GOVERNANCE_AUTHORITIES: tuple[str, ...] = (
    "approve_gate",
    "bypass_commit_boundary",
    "bypass_verifier",
    "commit",
    "execute_runtime",
    "mutate_manifest",
    "push",
    "schedule_execution",
    "weaken_policy",
)
REQUIRED_EVOLUTION_SEQUENCE: tuple[str, ...] = (
    "proposal",
    "impact_analysis",
    "simulation",
    "deterministic_validation",
    "human_approval",
    "generation",
    "deployment",
    "observation",
)
APPROVED_LEARNING_SOURCES: tuple[str, ...] = (
    "approved_enterprise_patterns",
    "approved_generators",
    "approved_manifests",
    "approved_templates",
    "verified_implementation_outcomes",
)
PROHIBITED_LEARNING_SOURCES: tuple[str, ...] = (
    "proprietary_customer_knowledge_without_authorization",
    "runtime_speculation",
    "temporary_execution_state",
    "undocumented_business_behavior",
    "unapproved_modifications",
)
MANIFEST_AUTHORITY_ROOT = "manifest/project/ai-ent"


@dataclass(frozen=True)
class GovernanceEvolutionContract:
    contract_id: str = "C08-GOVERNANCE-EVOLUTION-CONTRACT"
    contract_version: str = GOVERNANCE_EVOLUTION_CONTRACT_VERSION
    schema_version: str = GOVERNANCE_EVOLUTION_SCHEMA_VERSION
    manifest_authority_root: str = MANIFEST_AUTHORITY_ROOT
    domains: tuple[str, ...] = ALLOWED_GOVERNANCE_DOMAINS
    evolution_types: tuple[str, ...] = ALLOWED_EVOLUTION_TYPES
    allowed_authorities: tuple[str, ...] = ALLOWED_GOVERNANCE_AUTHORITIES
    prohibited_authorities: tuple[str, ...] = PROHIBITED_GOVERNANCE_AUTHORITIES
    required_evolution_sequence: tuple[str, ...] = REQUIRED_EVOLUTION_SEQUENCE
    approved_learning_sources: tuple[str, ...] = APPROVED_LEARNING_SOURCES
    prohibited_learning_sources: tuple[str, ...] = PROHIBITED_LEARNING_SOURCES
    deterministic_validation_required: bool = True
    human_approval_required: bool = True
    policy_weakening_allowed: bool = False
    gate_approval_authority: bool = False
    verifier_bypass_authority: bool = False
    commit_boundary_bypass_authority: bool = False
    self_scheduling_authority: bool = False
    runtime_execution_authority: bool = False

    @property
    def contract_hash(self) -> str:
        return hashlib.sha256(canonical_bytes(self._payload())).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["contract_hash"] = self.contract_hash
        return payload

    def _payload(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "schema_version": self.schema_version,
            "manifest_authority_root": self.manifest_authority_root,
            "domains": list(self.domains),
            "evolution_types": list(self.evolution_types),
            "allowed_authorities": list(self.allowed_authorities),
            "prohibited_authorities": list(self.prohibited_authorities),
            "required_evolution_sequence": list(self.required_evolution_sequence),
            "approved_learning_sources": list(self.approved_learning_sources),
            "prohibited_learning_sources": list(self.prohibited_learning_sources),
            "boundaries": {
                "deterministic_validation_required": self.deterministic_validation_required,
                "human_approval_required": self.human_approval_required,
                "policy_weakening_allowed": self.policy_weakening_allowed,
                "gate_approval_authority": self.gate_approval_authority,
                "verifier_bypass_authority": self.verifier_bypass_authority,
                "commit_boundary_bypass_authority": self.commit_boundary_bypass_authority,
                "self_scheduling_authority": self.self_scheduling_authority,
                "runtime_execution_authority": self.runtime_execution_authority,
            },
        }


@dataclass(frozen=True)
class GovernanceEvolutionRequest:
    request_id: str
    actor_id: str
    domain: str
    evolution_type: str
    summary: str
    target_manifest_paths: tuple[str, ...]
    requested_authorities: tuple[str, ...] = ("recommend",)
    policy_delta: dict[str, Any] = field(default_factory=dict)
    learning_sources: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "actor_id": self.actor_id,
            "domain": self.domain,
            "evolution_type": self.evolution_type,
            "summary": self.summary,
            "target_manifest_paths": sorted(self.target_manifest_paths),
            "requested_authorities": sorted(self.requested_authorities),
            "policy_delta": _canonical_policy_delta(self.policy_delta),
            "learning_sources": sorted(self.learning_sources),
            "evidence_refs": sorted(self.evidence_refs),
        }


@dataclass(frozen=True)
class GovernanceEvolutionDecision:
    contract_version: str
    schema_version: str
    status: GovernanceDecisionStatus
    request_id: str
    request_hash: str
    contract_hash: str
    accepted_authorities: tuple[str, ...]
    blockers: tuple[str, ...]
    required_evolution_sequence: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.status == "ACCEPTED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "contract_version": self.contract_version,
            "schema_version": self.schema_version,
            "status": self.status,
            "request_id": self.request_id,
            "request_hash": self.request_hash,
            "contract_hash": self.contract_hash,
            "accepted_authorities": list(self.accepted_authorities),
            "blockers": list(self.blockers),
            "required_evolution_sequence": list(self.required_evolution_sequence),
            "summary": {
                "ok": self.ok,
                "gate_approval_granted": False,
                "verifier_bypass_granted": False,
                "commit_boundary_bypass_granted": False,
                "self_scheduling_granted": False,
                "runtime_execution_granted": False,
                "policy_weakening_granted": False,
            },
        }


GovernanceRequestInput = GovernanceEvolutionRequest | dict[str, Any]


def default_governance_evolution_contract() -> GovernanceEvolutionContract:
    return GovernanceEvolutionContract()


def validate_governance_evolution_request(
    request: GovernanceRequestInput,
    *,
    contract: GovernanceEvolutionContract | None = None,
) -> GovernanceEvolutionDecision:
    active_contract = contract or default_governance_evolution_contract()
    normalized = _request_from_raw(request)
    blockers = _request_blockers(normalized, active_contract)
    accepted_authorities = (
        ()
        if blockers
        else tuple(
            authority
            for authority in sorted(set(normalized.requested_authorities))
            if authority in active_contract.allowed_authorities
        )
    )
    return GovernanceEvolutionDecision(
        contract_version=active_contract.contract_version,
        schema_version=active_contract.schema_version,
        status="REJECTED" if blockers else "ACCEPTED",
        request_id=normalized.request_id,
        request_hash=hashlib.sha256(canonical_bytes(normalized.as_dict())).hexdigest(),
        contract_hash=active_contract.contract_hash,
        accepted_authorities=accepted_authorities,
        blockers=tuple(sorted(blockers)),
        required_evolution_sequence=active_contract.required_evolution_sequence,
    )


def _request_blockers(
    request: GovernanceEvolutionRequest,
    contract: GovernanceEvolutionContract,
) -> list[str]:
    blockers: list[str] = []
    if not request.request_id.strip():
        blockers.append("request id is required")
    if not request.actor_id.strip():
        blockers.append(f"actor id is required:{request.request_id}")
    if not request.summary.strip():
        blockers.append(f"summary is required:{request.request_id}")
    if request.domain not in contract.domains:
        blockers.append(f"unsupported governance domain:{request.domain}")
    if request.evolution_type not in contract.evolution_types:
        blockers.append(f"unsupported evolution type:{request.evolution_type}")

    if not request.target_manifest_paths:
        blockers.append(f"target manifest path is required:{request.request_id}")
    for path in request.target_manifest_paths:
        if not _is_manifest_path(path, contract.manifest_authority_root):
            blockers.append(f"target path outside manifest authority:{path}")

    for authority in sorted(set(request.requested_authorities)):
        if authority in contract.prohibited_authorities:
            blockers.append(f"prohibited authority requested:{authority}")
        elif authority not in contract.allowed_authorities:
            blockers.append(f"unknown governance authority requested:{authority}")

    for source in sorted(set(request.learning_sources)):
        if source in contract.prohibited_learning_sources:
            blockers.append(f"prohibited learning source requested:{source}")
        elif source not in contract.approved_learning_sources:
            blockers.append(f"unknown learning source requested:{source}")

    blockers.extend(_policy_delta_blockers(request.policy_delta, contract))
    return blockers


def _policy_delta_blockers(
    policy_delta: dict[str, Any],
    contract: GovernanceEvolutionContract,
) -> list[str]:
    blockers: set[str] = set()
    if not policy_delta:
        return []

    entries = _policy_delta_entries(policy_delta)
    disabled_boundaries = {
        "deterministic_validation_required": "deterministic validation boundary",
        "human_approval_required": "human approval boundary",
        "policy_weakening_allowed": "policy weakening boundary",
    }
    for key, value in entries:
        label = disabled_boundaries.get(key)
        if label is None:
            continue
        if value is False or (key == "policy_weakening_allowed" and value is True):
            blockers.add(f"policy delta weakens {label}")

    denied_authorities: set[str] = set()
    for key, value in entries:
        if key in {"allowed_authorities", "authorities", "requested_authorities"}:
            denied_authorities |= set(_strings(value)) & set(contract.prohibited_authorities)
    for authority in sorted(denied_authorities):
        blockers.add(f"policy delta grants prohibited authority:{authority}")

    removed_boundaries: set[str] = set()
    for key, value in entries:
        if key == "removed_boundaries":
            removed_boundaries |= set(_strings(value))
    required_boundaries = {
        "deterministic_validation",
        "human_approval",
        "verified_commit",
        "verifier",
    }
    for boundary in sorted(removed_boundaries & required_boundaries):
        blockers.add(f"policy delta removes required boundary:{boundary}")

    boolean_authority_keys = {
        "approve_gate": "approve_gate",
        "commit": "commit",
        "commit_boundary_bypass_authority": "bypass_commit_boundary",
        "bypass_commit_boundary": "bypass_commit_boundary",
        "bypass_verifier": "bypass_verifier",
        "gate_approval_authority": "approve_gate",
        "execute_runtime": "execute_runtime",
        "mutate_manifest": "mutate_manifest",
        "push": "push",
        "runtime_execution_authority": "execute_runtime",
        "schedule_execution": "schedule_execution",
        "self_schedule": "schedule_execution",
        "self_scheduling_authority": "schedule_execution",
        "verifier_bypass_authority": "bypass_verifier",
        "weaken_policy": "weaken_policy",
    }
    for key, authority in boolean_authority_keys.items():
        if any(entry_key == key and value is True for entry_key, value in entries):
            blockers.add(f"policy delta grants prohibited authority:{authority}")

    return sorted(blockers)


def _request_from_raw(raw: GovernanceRequestInput) -> GovernanceEvolutionRequest:
    if isinstance(raw, GovernanceEvolutionRequest):
        return GovernanceEvolutionRequest(
            request_id=raw.request_id,
            actor_id=raw.actor_id,
            domain=raw.domain,
            evolution_type=raw.evolution_type,
            summary=raw.summary,
            target_manifest_paths=tuple(sorted(raw.target_manifest_paths)),
            requested_authorities=tuple(sorted(raw.requested_authorities)),
            policy_delta=_canonical_policy_delta(raw.policy_delta),
            learning_sources=tuple(sorted(raw.learning_sources)),
            evidence_refs=tuple(sorted(raw.evidence_refs)),
        )
    return GovernanceEvolutionRequest(
        request_id=str(raw.get("request_id", "")),
        actor_id=str(raw.get("actor_id", "")),
        domain=str(raw.get("domain", "")),
        evolution_type=str(raw.get("evolution_type", "")),
        summary=str(raw.get("summary", "")),
        target_manifest_paths=_strings(raw.get("target_manifest_paths", ())),
        requested_authorities=_strings(raw.get("requested_authorities", ("recommend",))),
        policy_delta=_mapping(raw.get("policy_delta", {})),
        learning_sources=_strings(raw.get("learning_sources", ())),
        evidence_refs=_strings(raw.get("evidence_refs", ())),
    )


def _is_manifest_path(path: str, manifest_root: str) -> bool:
    normalized = path.strip().strip("/")
    return normalized == manifest_root or normalized.startswith(f"{manifest_root}/")


def _strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set)):
        return tuple(sorted(str(item) for item in value))
    return (str(value),)


def _mapping(value: object) -> dict[str, Any]:
    return _canonical_policy_delta(value if isinstance(value, dict) else {})


def _policy_delta_entries(value: object) -> tuple[tuple[str, object], ...]:
    entries: list[tuple[str, object]] = []
    if isinstance(value, dict):
        for key, raw_value in value.items():
            entries.append((str(key), raw_value))
            entries.extend(_policy_delta_entries(raw_value))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            entries.extend(_policy_delta_entries(item))
    return tuple(entries)


def _canonical_policy_delta(value: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): _canonical_value(raw_value)
        for key, raw_value in sorted(value.items(), key=lambda item: str(item[0]))
    }


def _canonical_value(value: object) -> Any:
    if isinstance(value, dict):
        return _canonical_policy_delta(value)
    if isinstance(value, (list, tuple, set)):
        return sorted((_canonical_value(item) for item in value), key=str)
    return value
