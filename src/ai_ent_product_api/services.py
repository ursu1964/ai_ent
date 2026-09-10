from __future__ import annotations

from dataclasses import dataclass

from ai_ent.external_project import (
    CONTROL_PLANE_AUTHORITY,
    DEFAULT_PROHIBITED_PATHS,
    MANDATORY_VERIFICATION_COMMANDS,
)
from ai_ent.governance_contract import validate_governance_evolution_request
from ai_ent_product_api.schemas import (
    BoundarySnapshot,
    GovernanceValidationRequest,
    GovernanceValidationResponse,
    GovernanceValidationSummary,
    HealthStatus,
    ServiceDependencyStatus,
)


@dataclass(frozen=True)
class ProductBoundaryService:
    def boundary(self) -> BoundarySnapshot:
        return BoundarySnapshot(
            control_plane_authority=CONTROL_PLANE_AUTHORITY,
            mandatory_verification_commands=MANDATORY_VERIFICATION_COMMANDS,
            allowed_operations=(
                "observe_boundary",
                "validate_governance_request",
            ),
            denied_operations=(
                "approve_gate",
                "bypass_commit_boundary",
                "bypass_verifier",
                "commit",
                "execute_runtime",
                "push",
                "schedule_execution",
                "weaken_policy",
            ),
            prohibited_paths=DEFAULT_PROHIBITED_PATHS,
        )

    def dependency_status(self) -> ServiceDependencyStatus:
        return ServiceDependencyStatus(
            name="product_boundary",
            boundary="existing control-plane authority projection",
            status="wired",
        )


@dataclass(frozen=True)
class GovernanceValidationService:
    def validate(self, request: GovernanceValidationRequest) -> GovernanceValidationResponse:
        decision = validate_governance_evolution_request(request.model_dump(mode="python"))
        payload = decision.as_dict()
        summary = payload["summary"]
        return GovernanceValidationResponse(
            contract_version=str(payload["contract_version"]),
            schema_version=str(payload["schema_version"]),
            status=decision.status,
            request_id=decision.request_id,
            request_hash=decision.request_hash,
            contract_hash=decision.contract_hash,
            accepted_authorities=decision.accepted_authorities,
            blockers=decision.blockers,
            required_evolution_sequence=decision.required_evolution_sequence,
            summary=GovernanceValidationSummary(
                ok=bool(summary["ok"]),
                gate_approval_granted=bool(summary["gate_approval_granted"]),
                verifier_bypass_granted=bool(summary["verifier_bypass_granted"]),
                commit_boundary_bypass_granted=bool(summary["commit_boundary_bypass_granted"]),
                self_scheduling_granted=bool(summary["self_scheduling_granted"]),
                runtime_execution_granted=bool(summary["runtime_execution_granted"]),
                policy_weakening_granted=bool(summary["policy_weakening_granted"]),
            ),
        )

    def dependency_status(self) -> ServiceDependencyStatus:
        return ServiceDependencyStatus(
            name="governance_validation",
            boundary="ai_ent.governance_contract.validate_governance_evolution_request",
            status="wired",
        )


@dataclass(frozen=True)
class ProductApiServices:
    boundary_service: ProductBoundaryService
    governance_service: GovernanceValidationService

    @classmethod
    def defaults(cls) -> ProductApiServices:
        return cls(
            boundary_service=ProductBoundaryService(),
            governance_service=GovernanceValidationService(),
        )

    def health(self) -> HealthStatus:
        dependencies = (
            self.boundary_service.dependency_status(),
            self.governance_service.dependency_status(),
        )
        return HealthStatus(dependencies=dependencies)

    def boundary(self) -> BoundarySnapshot:
        return self.boundary_service.boundary()

    def validate_governance_request(
        self,
        request: GovernanceValidationRequest,
    ) -> GovernanceValidationResponse:
        return self.governance_service.validate(request)
