from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PRODUCT_API_VERSION = "v1"
PRODUCT_API_PREFIX = "/api/v1"
PRODUCT_API_SCHEMA_VERSION = "ai-ent-product-api-v1.0"
PRODUCT_API_CONTRACT_VERSION = "prd-task-005.1"
PRODUCT_API_DECISION_DEPENDENCIES = ("DECISION_REQUIRED:PRD-DEC-001",)


class SchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApiResponse(SchemaModel):
    api_version: str = PRODUCT_API_VERSION
    schema_version: str = PRODUCT_API_SCHEMA_VERSION
    data: Any


class ErrorPayload(SchemaModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(SchemaModel):
    api_version: str = PRODUCT_API_VERSION
    schema_version: str = PRODUCT_API_SCHEMA_VERSION
    error: ErrorPayload


class ServiceDependencyStatus(SchemaModel):
    name: str
    boundary: str
    status: Literal["wired"]


class BoundarySnapshot(SchemaModel):
    api_version: str = PRODUCT_API_VERSION
    contract_version: str = PRODUCT_API_CONTRACT_VERSION
    schema_version: str = PRODUCT_API_SCHEMA_VERSION
    control_plane_authority: str
    authority_mode: Literal["facade_only"] = "facade_only"
    grants_control_plane_authority: bool = False
    gate_approval_authority: bool = False
    verifier_bypass_authority: bool = False
    commit_boundary_bypass_authority: bool = False
    scheduling_authority: bool = False
    runtime_execution_authority: bool = False
    policy_weakening_authority: bool = False
    human_gate_policy: Literal["explicit_control_plane_gate_required"] = (
        "explicit_control_plane_gate_required"
    )
    implicit_human_gate_approval: bool = False
    independent_verification_required: bool = True
    mandatory_verification_commands: tuple[str, ...]
    decision_dependencies: tuple[str, ...] = PRODUCT_API_DECISION_DEPENDENCIES
    secret_values_exposed: bool = False
    allowed_operations: tuple[str, ...]
    denied_operations: tuple[str, ...]
    prohibited_paths: tuple[str, ...]


class HealthStatus(SchemaModel):
    status: Literal["ok"] = "ok"
    api_version: str = PRODUCT_API_VERSION
    schema_version: str = PRODUCT_API_SCHEMA_VERSION
    contract_version: str = PRODUCT_API_CONTRACT_VERSION
    dependencies: tuple[ServiceDependencyStatus, ...]


class GovernanceValidationRequest(SchemaModel):
    request_id: str
    actor_id: str
    domain: str
    evolution_type: str
    summary: str
    target_manifest_paths: tuple[str, ...]
    requested_authorities: tuple[str, ...] = ("recommend",)
    policy_delta: dict[str, Any] = Field(default_factory=dict)
    learning_sources: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


class GovernanceValidationSummary(SchemaModel):
    ok: bool
    gate_approval_granted: bool
    verifier_bypass_granted: bool
    commit_boundary_bypass_granted: bool
    self_scheduling_granted: bool
    runtime_execution_granted: bool
    policy_weakening_granted: bool


class GovernanceValidationResponse(SchemaModel):
    contract_version: str
    schema_version: str
    status: Literal["ACCEPTED", "REJECTED"]
    request_id: str
    request_hash: str
    contract_hash: str
    accepted_authorities: tuple[str, ...]
    blockers: tuple[str, ...]
    required_evolution_sequence: tuple[str, ...]
    summary: GovernanceValidationSummary


def response(data: object) -> ApiResponse:
    return ApiResponse(data=data)
