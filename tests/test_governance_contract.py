from __future__ import annotations

from ai_ent.governance_contract import (
    GOVERNANCE_EVOLUTION_CONTRACT_VERSION,
    GOVERNANCE_EVOLUTION_SCHEMA_VERSION,
    GovernanceEvolutionRequest,
    default_governance_evolution_contract,
    validate_governance_evolution_request,
)


def test_c08_contract_has_deterministic_policy_and_evolution_boundaries() -> None:
    contract = default_governance_evolution_contract()
    request = _valid_request(
        requested_authorities=(
            "simulate",
            "recommend",
            "record_evidence",
            "analyze_impact",
        ),
    )

    first = validate_governance_evolution_request(request, contract=contract)
    second = validate_governance_evolution_request(
        _valid_request(
            requested_authorities=(
                "analyze_impact",
                "record_evidence",
                "recommend",
                "simulate",
            ),
        ),
        contract=contract,
    )

    assert first.ok
    assert first.as_dict() == second.as_dict()
    assert first.contract_version == GOVERNANCE_EVOLUTION_CONTRACT_VERSION
    assert first.schema_version == GOVERNANCE_EVOLUTION_SCHEMA_VERSION
    assert first.accepted_authorities == (
        "analyze_impact",
        "recommend",
        "record_evidence",
        "simulate",
    )
    assert contract.required_evolution_sequence == (
        "proposal",
        "impact_analysis",
        "simulation",
        "deterministic_validation",
        "human_approval",
        "generation",
        "deployment",
        "observation",
    )
    assert set(contract.prohibited_authorities) >= {
        "approve_gate",
        "bypass_commit_boundary",
        "bypass_verifier",
        "commit",
        "execute_runtime",
        "push",
        "schedule_execution",
        "weaken_policy",
    }
    assert contract.as_dict()["boundaries"] == {
        "deterministic_validation_required": True,
        "human_approval_required": True,
        "policy_weakening_allowed": False,
        "gate_approval_authority": False,
        "verifier_bypass_authority": False,
        "commit_boundary_bypass_authority": False,
        "self_scheduling_authority": False,
        "runtime_execution_authority": False,
    }


def test_c08_contract_rejects_gate_verifier_commit_and_schedule_authority() -> None:
    result = validate_governance_evolution_request(
        _valid_request(
            requested_authorities=(
                "recommend",
                "approve_gate",
                "bypass_verifier",
                "bypass_commit_boundary",
                "schedule_execution",
                "execute_runtime",
                "commit",
                "push",
            ),
        )
    )

    assert not result.ok
    assert result.status == "REJECTED"
    assert result.accepted_authorities == ()
    assert set(result.blockers) >= {
        "prohibited authority requested:approve_gate",
        "prohibited authority requested:bypass_commit_boundary",
        "prohibited authority requested:bypass_verifier",
        "prohibited authority requested:commit",
        "prohibited authority requested:execute_runtime",
        "prohibited authority requested:push",
        "prohibited authority requested:schedule_execution",
    }
    assert result.as_dict()["summary"] == {
        "ok": False,
        "gate_approval_granted": False,
        "verifier_bypass_granted": False,
        "commit_boundary_bypass_granted": False,
        "self_scheduling_granted": False,
        "runtime_execution_granted": False,
        "policy_weakening_granted": False,
    }


def test_c08_contract_rejects_policy_weakening_requests() -> None:
    result = validate_governance_evolution_request(
        _valid_request(
            policy_delta={
                "allowed_authorities": ["recommend", "commit", "weaken_policy"],
                "approve_gate": True,
                "commit_boundary_bypass_authority": True,
                "deterministic_validation_required": False,
                "gate_approval_authority": True,
                "human_approval_required": False,
                "policy_weakening_allowed": True,
                "removed_boundaries": ["human_approval", "verified_commit"],
                "nested_policy": {
                    "allowed_authorities": ["schedule_execution"],
                    "runtime_execution_authority": True,
                },
                "runtime_execution_authority": True,
                "self_schedule": True,
                "verifier_bypass_authority": True,
            },
        )
    )

    assert not result.ok
    assert set(result.blockers) >= {
        "policy delta grants prohibited authority:approve_gate",
        "policy delta grants prohibited authority:bypass_commit_boundary",
        "policy delta grants prohibited authority:bypass_verifier",
        "policy delta grants prohibited authority:commit",
        "policy delta grants prohibited authority:execute_runtime",
        "policy delta grants prohibited authority:schedule_execution",
        "policy delta grants prohibited authority:weaken_policy",
        "policy delta removes required boundary:human_approval",
        "policy delta removes required boundary:verified_commit",
        "policy delta weakens deterministic validation boundary",
        "policy delta weakens human approval boundary",
        "policy delta weakens policy weakening boundary",
    }


def test_c08_contract_rejects_invalid_scope_and_unapproved_learning() -> None:
    result = validate_governance_evolution_request(
        _valid_request(
            domain="runtime",
            evolution_type="direct_patch",
            target_manifest_paths=("src/ai_ent/runtime_kernel.py", ".env"),
            learning_sources=(
                "approved_manifests",
                "temporary_execution_state",
                "unknown_runtime_memory",
            ),
        )
    )

    assert not result.ok
    assert set(result.blockers) >= {
        "prohibited learning source requested:temporary_execution_state",
        "target path outside manifest authority:.env",
        "target path outside manifest authority:src/ai_ent/runtime_kernel.py",
        "unknown learning source requested:unknown_runtime_memory",
        "unsupported evolution type:direct_patch",
        "unsupported governance domain:runtime",
    }


def _valid_request(
    *,
    requested_authorities: tuple[str, ...] = ("recommend",),
    policy_delta: dict[str, object] | None = None,
    domain: str = "evolution",
    evolution_type: str = "technical",
    target_manifest_paths: tuple[str, ...] = ("manifest/project/ai-ent/capabilities.yaml",),
    learning_sources: tuple[str, ...] = ("approved_manifests",),
) -> GovernanceEvolutionRequest:
    return GovernanceEvolutionRequest(
        request_id="GOV-REQ-C08-001",
        actor_id="AGT-004",
        domain=domain,
        evolution_type=evolution_type,
        summary="Recommend a bounded manifest governance evolution.",
        target_manifest_paths=target_manifest_paths,
        requested_authorities=requested_authorities,
        policy_delta=policy_delta or {},
        learning_sources=learning_sources,
        evidence_refs=("BEAG-001:pytest-regression-suite",),
    )
