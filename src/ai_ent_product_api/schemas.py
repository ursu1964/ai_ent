from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ai_ent.external_project import CONTROL_PLANE_AUTHORITY

PRODUCT_API_VERSION = "v1"
PRODUCT_API_PREFIX = "/api/v1"
PRODUCT_API_SCHEMA_VERSION = "ai-ent-product-api-v1.0"
PRODUCT_API_CONTRACT_VERSION = "prd-task-011.1"
PRODUCT_API_DECISION_DEPENDENCIES = ("DECISION_REQUIRED:PRD-DEC-001",)
PRODUCT_API_ACCESS_DECISION_DEPENDENCIES = ("DECISION_REQUIRED:PRD-DEC-002",)


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


type RuntimeComponentState = Literal["available", "unavailable", "unknown"]
type RuntimeComponentName = Literal["readiness", "recovery", "docker", "codex", "postgresql"]


class RuntimeComponentStatus(SchemaModel):
    name: RuntimeComponentName
    boundary: str
    status: RuntimeComponentState
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    passive_probe: bool = True
    actions_performed: bool = False
    grants_control_plane_authority: bool = False
    gate_approval_authority: bool = False
    verifier_bypass_authority: bool = False
    commit_boundary_bypass_authority: bool = False
    scheduling_authority: bool = False
    runtime_execution_authority: bool = False
    policy_weakening_authority: bool = False
    secret_values_exposed: bool = False


class RuntimeAuthoritySnapshot(SchemaModel):
    authority_mode: Literal["facade_only"] = "facade_only"
    operation_mode: Literal["read_only_projection"] = "read_only_projection"
    control_plane_authority: str = CONTROL_PLANE_AUTHORITY
    grants_control_plane_authority: bool = False
    gate_approval_authority: bool = False
    verifier_bypass_authority: bool = False
    commit_boundary_bypass_authority: bool = False
    scheduling_authority: bool = False
    runtime_execution_authority: bool = False
    repair_execution_authority: bool = False
    artifact_authority: bool = False
    policy_weakening_authority: bool = False
    mutating_operations_exposed: bool = False
    human_gate_policy: Literal["explicit_control_plane_gate_required"] = (
        "explicit_control_plane_gate_required"
    )
    implicit_human_gate_approval: bool = False
    independent_verification_required: bool = True
    mandatory_verification_commands: tuple[str, ...]
    allowed_operations: tuple[str, ...] = (
        "query_provenance",
        "read_artifact_metadata",
        "read_artifact_provenance",
        "read_generated_plan",
        "read_generated_dag",
        "read_task_snapshot",
        "read_execution_snapshot",
        "read_repair_snapshot",
        "read_runtime_state",
        "read_human_gate_review",
        "validate_human_gate_evidence",
        "submit_human_gate_decision",
    )
    denied_operations: tuple[str, ...] = (
        "approve_gate",
        "bypass_commit_boundary",
        "bypass_verifier",
        "claim_task",
        "commit",
        "execute_repair",
        "execute_runtime",
        "push",
        "record_evidence",
        "schedule_execution",
        "weaken_policy",
    )
    secret_values_exposed: bool = False


class BoundarySnapshot(SchemaModel):
    api_version: str = PRODUCT_API_VERSION
    contract_version: str = PRODUCT_API_CONTRACT_VERSION
    schema_version: str = PRODUCT_API_SCHEMA_VERSION
    control_plane_authority: str
    authority_mode: Literal["facade_only"] = "facade_only"
    operation_mode: Literal["read_only_projection"] = "read_only_projection"
    grants_control_plane_authority: bool = False
    gate_approval_authority: bool = False
    verifier_bypass_authority: bool = False
    commit_boundary_bypass_authority: bool = False
    scheduling_authority: bool = False
    runtime_execution_authority: bool = False
    repair_execution_authority: bool = False
    artifact_authority: bool = False
    policy_weakening_authority: bool = False
    mutating_operations_exposed: bool = False
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


class AccessBindingProfile(SchemaModel):
    bind_address: str
    port: int = Field(ge=1, le=65535)
    lan_bind_address: str
    effective_bind_address: str
    network_scope: Literal["local_loopback", "lan_gated"]
    lan_access_enabled: bool = False
    blocked_by_decisions: tuple[str, ...] = PRODUCT_API_ACCESS_DECISION_DEPENDENCIES


class ReverseProxyAccessConfig(SchemaModel):
    enabled: bool = False
    provider: Literal["none", "caddy", "nginx"] = "none"
    allowed_providers: tuple[Literal["caddy", "nginx"], ...] = ("caddy", "nginx")
    decision_dependency: str = "DECISION_REQUIRED:PRD-DEC-002"
    forwards_to_bind_address: str
    forwards_to_port: int = Field(ge=1, le=65535)


class TlsAccessConfig(SchemaModel):
    enabled: bool = False
    mode: Literal["disabled_for_loopback", "reverse_proxy_terminated_when_enabled"] = (
        "disabled_for_loopback"
    )
    required_for_lan: bool = True
    certificate_material: Literal["not_configured_until_decision"] = (
        "not_configured_until_decision"
    )


class FirewallAssumptionConfig(SchemaModel):
    host_firewall_managed_externally: bool = True
    inbound_lan_ports_allowed: tuple[int, ...] = ()
    inbound_public_ports_allowed: tuple[int, ...] = ()
    operator_confirmation_required_for_changes: bool = True


class ProductAccessProfileSnapshot(SchemaModel):
    api_version: str = PRODUCT_API_VERSION
    contract_version: str = PRODUCT_API_CONTRACT_VERSION
    schema_version: str = PRODUCT_API_SCHEMA_VERSION
    profile_id: str
    active_profile: Literal["local_loopback"]
    configured_profiles: tuple[Literal["local_loopback", "lan_gated"], ...]
    binding: AccessBindingProfile
    reverse_proxy: ReverseProxyAccessConfig
    tls: TlsAccessConfig
    firewall_assumptions: FirewallAssumptionConfig
    explicit_human_review_required: bool = True
    human_gate_id: Literal["GATE-PRD-LAN-ACCESS"] = "GATE-PRD-LAN-ACCESS"
    independent_verification_required: bool = True
    mandatory_verification_commands: tuple[str, ...]
    decision_dependencies: tuple[str, ...] = PRODUCT_API_ACCESS_DECISION_DEPENDENCIES
    grants_control_plane_authority: bool = False
    gate_approval_authority: bool = False
    verifier_bypass_authority: bool = False
    scheduling_authority: bool = False
    runtime_execution_authority: bool = False
    credential_values_exposed: bool = False
    authority: RuntimeAuthoritySnapshot


class ReviewBoundarySnapshot(SchemaModel):
    source_authority: str
    control_plane_authority: str
    authority_mode: Literal["facade_only"] = "facade_only"
    grants_control_plane_authority: bool = False
    gate_approval_authority: bool = False
    verifier_bypass_authority: bool = False
    commit_boundary_bypass_authority: bool = False
    scheduling_authority: bool = False
    runtime_execution_authority: bool = False
    policy_weakening_authority: bool = False
    implicit_human_gate_approval: bool = False
    human_gate_policy: Literal["explicit_control_plane_gate_required"] = (
        "explicit_control_plane_gate_required"
    )
    independent_verification_required: bool = True
    mandatory_verification_commands: tuple[str, ...]
    decision_dependencies: tuple[str, ...] = PRODUCT_API_DECISION_DEPENDENCIES
    secret_values_exposed: bool = False
    allowed_operations: tuple[str, ...]
    denied_operations: tuple[str, ...]
    prohibited_paths: tuple[str, ...]


class ProjectReview(SchemaModel):
    project_id: str
    source_compiled_hash: str
    project_hash: str
    project: dict[str, Any]
    review_boundary: ReviewBoundarySnapshot


class ProjectIntakeReview(SchemaModel):
    project_id: str
    source_compiled_hash: str
    intake_hash: str
    intake: dict[str, Any]
    review_boundary: ReviewBoundarySnapshot


class ProjectRequirementsReview(SchemaModel):
    project_id: str
    source_compiled_hash: str
    requirements_hash: str
    requirements_count: int
    requirements: tuple[dict[str, Any], ...]
    review_boundary: ReviewBoundarySnapshot


class ProjectArchitectureReview(SchemaModel):
    project_id: str
    source_compiled_hash: str
    architecture_hash: str
    component_count: int
    interface_count: int
    data_object_count: int
    architecture: dict[str, Any]
    review_boundary: ReviewBoundarySnapshot


class CapabilityReview(SchemaModel):
    project_id: str
    source_compiled_hash: str
    capability_resolution_hash: str
    capability_count: int
    capabilities: tuple[dict[str, Any], ...]
    required_capabilities: tuple[str, ...]
    satisfied_capabilities: tuple[str, ...]
    partially_satisfied_capabilities: tuple[str, ...]
    unsatisfied_capabilities: tuple[str, ...]
    deferred_capabilities: tuple[str, ...]
    blocked_capabilities: tuple[str, ...]
    independent_verification_required: bool = True
    mandatory_verification_commands: tuple[str, ...]
    review_boundary: ReviewBoundarySnapshot


class GeneratedPlanSnapshot(SchemaModel):
    api_version: str = PRODUCT_API_VERSION
    contract_version: str = PRODUCT_API_CONTRACT_VERSION
    schema_version: str = PRODUCT_API_SCHEMA_VERSION
    product_plan_id: str
    plan_version: str
    plan_state: str
    plan_hash: str
    task_count: int
    dependency_edge_count: int
    human_gate_count: int
    effective_concurrency: int
    critical_path: tuple[str, ...]
    waves: tuple[dict[str, Any], ...]
    decision_dependencies: tuple[str, ...] = PRODUCT_API_DECISION_DEPENDENCIES
    authority: RuntimeAuthoritySnapshot


class GeneratedDagEdge(SchemaModel):
    dependency_task_id: str
    task_id: str


class GeneratedHumanGateSnapshot(SchemaModel):
    gate_id: str
    task_id: str
    status: str
    reason: str
    risk_level: str
    approval_boundary: str
    resume_semantics: str


class GeneratedDagSnapshot(SchemaModel):
    api_version: str = PRODUCT_API_VERSION
    contract_version: str = PRODUCT_API_CONTRACT_VERSION
    schema_version: str = PRODUCT_API_SCHEMA_VERSION
    product_plan_id: str
    plan_version: str
    dependency_edges: tuple[GeneratedDagEdge, ...]
    waves: tuple[dict[str, Any], ...]
    critical_path: tuple[str, ...]
    human_gates: tuple[GeneratedHumanGateSnapshot, ...]
    authority: RuntimeAuthoritySnapshot


class TaskSnapshot(SchemaModel):
    task_id: str
    title: str
    objective: str
    status: str
    execution_class: str
    schedulable: bool
    fingerprint: str
    dependencies: tuple[str, ...]
    readiness_status: str
    readiness_reasons: tuple[str, ...]
    risk_level: str
    agent_role: str
    model_profile: str
    executor: str
    verification_profile: str
    feasibility_status: str
    policy_decision: str
    acceptance_criteria: tuple[str, ...]
    required_decisions: tuple[str, ...]
    allowed_write_scope: tuple[str, ...]
    prohibited_paths: tuple[str, ...]
    human_gate_ids: tuple[str, ...]


class TaskCollectionSnapshot(SchemaModel):
    api_version: str = PRODUCT_API_VERSION
    contract_version: str = PRODUCT_API_CONTRACT_VERSION
    schema_version: str = PRODUCT_API_SCHEMA_VERSION
    product_plan_id: str
    plan_version: str
    tasks: tuple[TaskSnapshot, ...]
    authority: RuntimeAuthoritySnapshot


class ExecutionSnapshot(SchemaModel):
    execution_id: str
    task_id: str
    executor_type: str
    status: str
    attempt: int
    terminal_state: str | None
    error_classification: str | None
    candidate_tree_hash: str | None
    commit_hash: str | None
    active_lease: bool
    checkpoint_count: int


class ExecutionCollectionSnapshot(SchemaModel):
    api_version: str = PRODUCT_API_VERSION
    contract_version: str = PRODUCT_API_CONTRACT_VERSION
    schema_version: str = PRODUCT_API_SCHEMA_VERSION
    product_plan_id: str
    plan_version: str
    executions: tuple[ExecutionSnapshot, ...]
    authority: RuntimeAuthoritySnapshot


class RepairSnapshot(SchemaModel):
    failed_execution_id: str
    task_id: str
    category: str
    stage: str
    retryability: str
    reason: str
    recommended_action: str
    repair_attempt_count: int
    max_autonomous_repair_attempts: int
    human_gate_required: bool
    creates_execution: bool = False
    grants_repair_authority: bool = False


class RepairCollectionSnapshot(SchemaModel):
    api_version: str = PRODUCT_API_VERSION
    contract_version: str = PRODUCT_API_CONTRACT_VERSION
    schema_version: str = PRODUCT_API_SCHEMA_VERSION
    product_plan_id: str
    plan_version: str
    repairs: tuple[RepairSnapshot, ...]
    authority: RuntimeAuthoritySnapshot


class RuntimeHumanGateStateSnapshot(SchemaModel):
    gate_id: str
    task_id: str
    status: str
    reason: str
    approval_boundary: str


class RuntimeStateSnapshot(SchemaModel):
    api_version: str = PRODUCT_API_VERSION
    contract_version: str = PRODUCT_API_CONTRACT_VERSION
    schema_version: str = PRODUCT_API_SCHEMA_VERSION
    project_id: str
    product_plan_id: str
    plan_version: str
    import_status: str | None
    imported_tasks: int
    dependency_edges: int
    human_gates: int
    pending_human_gates: int
    approved_human_gates: int
    rejected_human_gates: int
    ready_tasks: tuple[str, ...]
    waiting_tasks: tuple[str, ...]
    gated_tasks: tuple[str, ...]
    decision_blocked_tasks: tuple[str, ...]
    executions: int
    failed_executions: int
    active_leases: int
    runtime_human_gates: tuple[RuntimeHumanGateStateSnapshot, ...]
    authority: RuntimeAuthoritySnapshot


class RuntimeStatus(SchemaModel):
    status: Literal["ok", "degraded"]
    api_version: str = PRODUCT_API_VERSION
    contract_version: str = PRODUCT_API_CONTRACT_VERSION
    schema_version: str = PRODUCT_API_SCHEMA_VERSION
    control_plane_authority: str
    authority_mode: Literal["facade_only"] = "facade_only"
    operation_mode: Literal["read_only_projection"] = "read_only_projection"
    grants_control_plane_authority: bool = False
    gate_approval_authority: bool = False
    verifier_bypass_authority: bool = False
    commit_boundary_bypass_authority: bool = False
    scheduling_authority: bool = False
    runtime_execution_authority: bool = False
    repair_execution_authority: bool = False
    artifact_authority: bool = False
    policy_weakening_authority: bool = False
    mutating_operations_exposed: bool = False
    human_gate_policy: Literal["explicit_control_plane_gate_required"] = (
        "explicit_control_plane_gate_required"
    )
    implicit_human_gate_approval: bool = False
    independent_verification_required: bool = True
    mandatory_verification_commands: tuple[str, ...]
    decision_dependencies: tuple[str, ...] = PRODUCT_API_DECISION_DEPENDENCIES
    secret_values_exposed: bool = False
    readiness: RuntimeComponentStatus
    recovery: RuntimeComponentStatus
    docker: RuntimeComponentStatus
    codex: RuntimeComponentStatus
    postgresql: RuntimeComponentStatus


class HumanGateReviewSnapshot(SchemaModel):
    api_version: str = PRODUCT_API_VERSION
    contract_version: str = PRODUCT_API_CONTRACT_VERSION
    schema_version: str = PRODUCT_API_SCHEMA_VERSION
    gate_id: str
    task_id: str
    status: str
    reason: str
    risk_level: str
    approval_boundary: str
    expected_evidence: tuple[str, ...]
    downstream_task_ids: tuple[str, ...]
    resume_semantics: str
    explicit_human_review_required: bool = True
    independent_verification_required: bool = True
    decision_dependencies: tuple[str, ...] = PRODUCT_API_DECISION_DEPENDENCIES
    authority: RuntimeAuthoritySnapshot


class HumanGateReviewCollectionSnapshot(SchemaModel):
    api_version: str = PRODUCT_API_VERSION
    contract_version: str = PRODUCT_API_CONTRACT_VERSION
    schema_version: str = PRODUCT_API_SCHEMA_VERSION
    product_plan_id: str
    plan_version: str
    reviews: tuple[HumanGateReviewSnapshot, ...]
    pending_count: int
    approved_count: int
    rejected_count: int
    authority: RuntimeAuthoritySnapshot


class HumanGateEvidenceValidationRequest(SchemaModel):
    actor_id: str = Field(min_length=1)
    evidence_refs: tuple[str, ...]
    verification_commands: tuple[str, ...]
    scope_task_ids: tuple[str, ...] = ()


class HumanGateEvidenceValidationResponse(SchemaModel):
    gate_id: str
    task_id: str
    valid: bool
    missing_expected_evidence: tuple[str, ...]
    missing_mandatory_verification_commands: tuple[str, ...]
    unknown_scope_task_ids: tuple[str, ...]
    allowed_scope_task_ids: tuple[str, ...]
    explicit_human_review_required: bool = True
    independent_verification_required: bool = True
    applies_runtime_gate_mutation: bool = False
    grants_gate_approval_authority: bool = False
    verifier_bypass_granted: bool = False
    authority: RuntimeAuthoritySnapshot


class HumanGateApprovalRequest(SchemaModel):
    actor_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    evidence_refs: tuple[str, ...]
    verification_commands: tuple[str, ...]
    scope_task_ids: tuple[str, ...] = ()


class HumanGateRejectionRequest(SchemaModel):
    actor_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()
    verification_commands: tuple[str, ...]
    scope_task_ids: tuple[str, ...] = ()


class HumanGateScopedDecisionRequest(SchemaModel):
    actor_id: str = Field(min_length=1)
    decision: Literal["approve", "reject"]
    rationale: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()
    verification_commands: tuple[str, ...]
    scope_task_ids: tuple[str, ...] = Field(min_length=1)


class HumanGateDecisionResponse(SchemaModel):
    gate_id: str
    task_id: str
    requested_decision: Literal["approve", "reject"]
    request_accepted: bool
    decision_status: Literal["CONTROL_PLANE_REVIEW_REQUIRED", "EVIDENCE_INCOMPLETE"]
    decision_request_hash: str
    scope_task_ids: tuple[str, ...]
    evidence_valid: bool
    missing_expected_evidence: tuple[str, ...]
    missing_mandatory_verification_commands: tuple[str, ...]
    unknown_scope_task_ids: tuple[str, ...]
    allowed_scope_task_ids: tuple[str, ...]
    runtime_gate_status_before: str
    runtime_gate_status_after: str
    control_plane_authority: str
    control_plane_review_required: bool = True
    applied_to_runtime: bool = False
    runtime_mutation_allowed: bool = False
    explicit_human_review_required: bool = True
    independent_verification_required: bool = True
    grants_gate_approval_authority: bool = False
    verifier_bypass_granted: bool = False
    scheduling_authority_granted: bool = False
    decision_dependencies: tuple[str, ...] = PRODUCT_API_DECISION_DEPENDENCIES
    authority: RuntimeAuthoritySnapshot


class ArtifactEvidenceServiceBoundarySnapshot(SchemaModel):
    service: str
    interface_id: str
    contract_version: str
    schema_version: str


class ArtifactRecordMetadataSnapshot(SchemaModel):
    artifact_id: str
    node_id: str
    kind: str | None
    source: str
    project_id: str | None
    task_id: str | None
    execution_id: str | None
    commit_hash: str | None
    tree_hash: str | None
    payload_hash: str
    payload_redacted: bool = True


class ArtifactMetadataSnapshot(SchemaModel):
    api_version: str = PRODUCT_API_VERSION
    contract_version: str = PRODUCT_API_CONTRACT_VERSION
    schema_version: str = PRODUCT_API_SCHEMA_VERSION
    project_id: str
    product_plan_id: str
    plan_version: str
    graph_available: bool
    graph_id: str | None
    artifact_graph_contract_version: str | None
    artifact_graph_schema_version: str | None
    evidence_graph_hash: str | None
    source_knowledge_graph_hash: str | None
    source_project_memory_hash: str | None
    output_authority_state: Literal["NON_AUTHORITATIVE"] = "NON_AUTHORITATIVE"
    payloads_redacted: bool = True
    secret_values_exposed: bool = False
    requirements_covered: tuple[str, ...]
    capabilities_covered: tuple[str, ...]
    service_boundaries: tuple[ArtifactEvidenceServiceBoundarySnapshot, ...]
    artifacts: tuple[ArtifactRecordMetadataSnapshot, ...]
    summary: dict[str, Any]
    authority: RuntimeAuthoritySnapshot


class ProvenanceNodeSnapshot(SchemaModel):
    node_id: str
    node_type: str
    native_id: str
    source: str
    classification: str | None
    payload_hash: str
    payload_redacted: bool = True


class ProvenanceEdgeSnapshot(SchemaModel):
    edge_id: str
    edge_type: str
    source_node_id: str
    target_node_id: str
    provenance_refs: tuple[str, ...]
    payload_hash: str


class ProvenanceTraceSnapshot(SchemaModel):
    api_version: str = PRODUCT_API_VERSION
    contract_version: str = PRODUCT_API_CONTRACT_VERSION
    schema_version: str = PRODUCT_API_SCHEMA_VERSION
    project_id: str
    product_plan_id: str
    plan_version: str
    query_kind: Literal["requirement"] = "requirement"
    query_id: str
    requirement_id: str
    requirement_node_id: str
    evidence_graph_hash: str | None
    output_authority_state: Literal["NON_AUTHORITATIVE"] = "NON_AUTHORITATIVE"
    payloads_redacted: bool = True
    secret_values_exposed: bool = False
    nodes: tuple[ProvenanceNodeSnapshot, ...]
    edges: tuple[ProvenanceEdgeSnapshot, ...]
    summary: dict[str, Any]
    authority: RuntimeAuthoritySnapshot


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
