from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ai_ent.external_project import (
    CONTROL_PLANE_AUTHORITY,
    DEFAULT_PROHIBITED_PATHS,
    MANDATORY_VERIFICATION_COMMANDS,
)
from ai_ent_product_api.schemas import PRODUCT_API_DECISION_DEPENDENCIES

PRODUCT_UI_VERSION = "v1"
PRODUCT_UI_PREFIX = "/ui/v1"
PRODUCT_UI_SCHEMA_VERSION = "ai-ent-product-ui-v1.0"
PRODUCT_UI_CONTRACT_VERSION = "prd-task-017.1"
PRODUCT_UI_DECISION_DEPENDENCIES = PRODUCT_API_DECISION_DEPENDENCIES

EvidenceStatus = Literal["NON_AUTHORITATIVE"]
ArtifactAuthorityState = Literal["NON_AUTHORITATIVE", "VALIDATED"]
ArtifactKind = Literal["source", "documentation", "log", "package", "evidence", "runtime_config"]


class SchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UiResponse(SchemaModel):
    ui_version: str = PRODUCT_UI_VERSION
    schema_version: str = PRODUCT_UI_SCHEMA_VERSION
    data: Any


class LoginRequest(SchemaModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class DashboardRequest(SchemaModel):
    session_id: str = Field(min_length=1)


class UiHealthStatus(SchemaModel):
    status: Literal["ok"] = "ok"
    ui_version: str = PRODUCT_UI_VERSION
    schema_version: str = PRODUCT_UI_SCHEMA_VERSION
    contract_version: str = PRODUCT_UI_CONTRACT_VERSION
    api_boundary_status: Literal["wired"] = "wired"
    decision_dependencies: tuple[str, ...] = PRODUCT_UI_DECISION_DEPENDENCIES


class UiNavigationItem(SchemaModel):
    label: str
    route: str
    requires_authentication: bool = True


class UiShell(SchemaModel):
    ui_version: str = PRODUCT_UI_VERSION
    contract_version: str = PRODUCT_UI_CONTRACT_VERSION
    schema_version: str = PRODUCT_UI_SCHEMA_VERSION
    product_name: str = "AI Enterprise"
    primary_routes: tuple[UiNavigationItem, ...]
    authority_mode: Literal["facade_only"] = "facade_only"
    control_plane_access: Literal["product_api_only"] = "product_api_only"
    forbidden_implicit_actions: tuple[str, ...]
    decision_dependencies: tuple[str, ...] = PRODUCT_UI_DECISION_DEPENDENCIES
    secret_values_exposed: bool = False


class LoginView(SchemaModel):
    page: Literal["Login"] = "Login"
    action: Literal["POST /ui/v1/login"] = "POST /ui/v1/login"
    requires_password: bool = True
    password_echoed: bool = False
    creates_control_plane_authority: bool = False
    secret_values_exposed: bool = False


class AuthenticatedOperator(SchemaModel):
    username: str
    display_name: str
    roles: tuple[str, ...]


class LoginSession(SchemaModel):
    session_id: str
    operator: AuthenticatedOperator
    session_owner: Literal["server"] = "server"
    browser_secret_material_exposed: bool = False
    grants_control_plane_authority: bool = False
    gate_approval_authority: bool = False


class DashboardMetric(SchemaModel):
    label: str
    value: int
    status: Literal["ok", "attention", "blocked"]


class DashboardGateSummary(SchemaModel):
    pending: int
    approved_by_ui_navigation: int = 0
    approval_requires_explicit_gate_action: bool = True


class DashboardVerificationSummary(SchemaModel):
    independent_verification_required: bool
    mandatory_verification_commands: tuple[str, ...]
    verifier_bypass_authority: bool = False


class DashboardAuthoritySummary(SchemaModel):
    control_plane_authority: str
    authority_mode: Literal["facade_only"]
    grants_control_plane_authority: bool
    gate_approval_authority: bool
    scheduling_authority: bool
    runtime_execution_authority: bool
    policy_weakening_authority: bool
    denied_operations: tuple[str, ...]


class DashboardView(SchemaModel):
    page: Literal["Dashboard"] = "Dashboard"
    operator: AuthenticatedOperator
    metrics: tuple[DashboardMetric, ...]
    gates: DashboardGateSummary
    verification: DashboardVerificationSummary
    authority: DashboardAuthoritySummary
    decision_dependencies: tuple[str, ...] = PRODUCT_UI_DECISION_DEPENDENCIES
    secret_values_exposed: bool = False


def response(data: object) -> UiResponse:
    return UiResponse(data=data)


class UiAuthoritySnapshot(SchemaModel):
    contract_version: str = PRODUCT_UI_CONTRACT_VERSION
    schema_version: str = PRODUCT_UI_SCHEMA_VERSION
    authority_mode: Literal["facade_only"] = "facade_only"
    read_only: bool = True
    grants_control_plane_authority: bool = False
    gate_approval_authority: bool = False
    verifier_bypass_authority: bool = False
    commit_boundary_bypass_authority: bool = False
    scheduling_authority: bool = False
    runtime_execution_authority: bool = False
    repair_execution_authority: bool = False
    policy_weakening_authority: bool = False
    human_gate_policy: Literal["explicit_control_plane_gate_required"] = (
        "explicit_control_plane_gate_required"
    )
    implicit_human_gate_approval: bool = False
    independent_verification_required: bool = True
    mandatory_verification_commands: tuple[str, ...]
    decision_dependencies: tuple[str, ...] = PRODUCT_API_DECISION_DEPENDENCIES
    allowed_operations: tuple[str, ...] = (
        "inspect_task_dag",
        "inspect_task_details",
        "inspect_execution_attempts",
        "inspect_repair_history",
        "inspect_verifier_output",
        "inspect_commits",
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
        "schedule_execution",
        "weaken_policy",
    )
    secret_values_exposed: bool = False


class HumanGateView(SchemaModel):
    gate_id: str
    task_id: str
    status: str
    reason: str
    risk_level: str
    approval_boundary: str
    explicit_control_plane_gate_required: bool = True
    ui_can_approve: bool = False


class TaskDagEdge(SchemaModel):
    dependency_task_id: str
    task_id: str


class TaskDagNode(SchemaModel):
    task_id: str
    title: str
    status: str
    readiness_status: str
    readiness_reasons: tuple[str, ...]
    upstream_dependencies: tuple[str, ...]
    downstream_dependents: tuple[str, ...]
    human_gate_ids: tuple[str, ...]
    required_decisions: tuple[str, ...]
    latest_attempt: int | None
    latest_execution_status: str | None
    latest_commit_hash: str | None
    verifier_status: str | None
    blocked_by: tuple[str, ...]


class VerifierOutputView(SchemaModel):
    checkpoint_id: str
    execution_id: str
    task_id: str
    status: str | None
    ok: bool | None
    findings: tuple[str, ...]
    commands: tuple[str, ...]
    sanitized_payload: Any


class CommitReference(SchemaModel):
    source: Literal["execution", "checkpoint"]
    hash: str
    task_id: str
    execution_id: str | None
    checkpoint_id: str | None = None
    candidate_tree_hash: str | None = None
    tree_hash: str | None = None


class ExecutionAttemptView(SchemaModel):
    execution_id: str
    task_id: str
    executor_type: str
    status: str
    attempt: int
    terminal_state: str | None
    error_classification: str | None
    candidate_tree_hash: str | None
    active_lease: bool
    checkpoint_count: int
    verifier_outputs: tuple[VerifierOutputView, ...]
    commits: tuple[CommitReference, ...]


class RepairHistoryView(SchemaModel):
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


class TaskDetailView(SchemaModel):
    task_id: str
    title: str
    objective: str
    status: str
    readiness_status: str
    readiness_reasons: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    required_decisions: tuple[str, ...]
    allowed_write_scope: tuple[str, ...]
    prohibited_paths: tuple[str, ...]
    human_gates: tuple[HumanGateView, ...]
    upstream_dependencies: tuple[str, ...]
    downstream_dependents: tuple[str, ...]
    execution_attempts: tuple[ExecutionAttemptView, ...]
    repair_history: tuple[RepairHistoryView, ...]
    verifier_outputs: tuple[VerifierOutputView, ...]
    commits: tuple[CommitReference, ...]


class ExecutionMonitoringDashboard(SchemaModel):
    contract_version: str = PRODUCT_UI_CONTRACT_VERSION
    schema_version: str = PRODUCT_UI_SCHEMA_VERSION
    product_plan_id: str
    plan_version: str
    task_dag_nodes: tuple[TaskDagNode, ...]
    task_dag_edges: tuple[TaskDagEdge, ...]
    task_details: tuple[TaskDetailView, ...]
    execution_attempts: tuple[ExecutionAttemptView, ...]
    repair_history: tuple[RepairHistoryView, ...]
    verifier_outputs: tuple[VerifierOutputView, ...]
    commits: tuple[CommitReference, ...]
    human_gates: tuple[HumanGateView, ...]
    authority: UiAuthoritySnapshot


class ProductUiAuthoritySnapshot(SchemaModel):
    authority_mode: Literal["browser_facade_only"] = "browser_facade_only"
    control_plane_authority: str = CONTROL_PLANE_AUTHORITY
    grants_control_plane_authority: bool = False
    gate_approval_authority: bool = False
    verifier_bypass_authority: bool = False
    commit_boundary_bypass_authority: bool = False
    scheduling_authority: bool = False
    runtime_execution_authority: bool = False
    artifact_write_authority: bool = False
    policy_weakening_authority: bool = False
    human_gate_policy: Literal["explicit_control_plane_gate_required"] = (
        "explicit_control_plane_gate_required"
    )
    implicit_human_gate_approval: bool = False
    independent_verification_required: bool = True
    mandatory_verification_commands: tuple[str, ...] = MANDATORY_VERIFICATION_COMMANDS
    decision_dependencies: tuple[str, ...] = PRODUCT_UI_DECISION_DEPENDENCIES
    allowed_operations: tuple[str, ...] = (
        "browse_artifact_metadata",
        "browse_evidence_metadata",
        "view_provenance_path",
    )
    denied_operations: tuple[str, ...] = (
        "approve_gate",
        "bypass_commit_boundary",
        "bypass_verifier",
        "claim_task",
        "commit",
        "download_secret",
        "execute_runtime",
        "mutate_artifact",
        "push",
        "schedule_execution",
        "weaken_policy",
    )
    prohibited_paths: tuple[str, ...] = DEFAULT_PROHIBITED_PATHS
    secret_values_exposed: bool = False


class ArtifactRecord(SchemaModel):
    artifact_id: str
    artifact_type: str
    kind: ArtifactKind
    title: str
    relative_path: str
    content_hash: str
    producer: str
    authority_state: ArtifactAuthorityState = "NON_AUTHORITATIVE"
    evidence_status: EvidenceStatus = "NON_AUTHORITATIVE"
    contains_secret: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceRecord(SchemaModel):
    evidence_id: str
    title: str
    summary: str
    evidence_status: EvidenceStatus = "NON_AUTHORITATIVE"
    authority_state: Literal["NON_AUTHORITATIVE"] = "NON_AUTHORITATIVE"
    source_refs: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    human_gate_ids: tuple[str, ...] = ()
    independent_verification_required: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProvenanceEdge(SchemaModel):
    source_id: str
    target_id: str
    relationship: str


class ProvenanceNode(SchemaModel):
    node_id: str
    node_type: Literal["artifact", "evidence"]
    label: str
    status: str


class ArtifactBrowserView(SchemaModel):
    ui_version: str = PRODUCT_UI_VERSION
    contract_version: str = PRODUCT_UI_CONTRACT_VERSION
    schema_version: str = PRODUCT_UI_SCHEMA_VERSION
    route_id: Literal["artifact_browser"] = "artifact_browser"
    query: str | None = None
    total_artifacts: int
    artifacts: tuple[ArtifactRecord, ...]
    selected_artifact: ArtifactRecord | None = None
    evidence_status: EvidenceStatus = "NON_AUTHORITATIVE"
    authority: ProductUiAuthoritySnapshot


class ProvenancePathView(SchemaModel):
    ui_version: str = PRODUCT_UI_VERSION
    contract_version: str = PRODUCT_UI_CONTRACT_VERSION
    schema_version: str = PRODUCT_UI_SCHEMA_VERSION
    route_id: Literal["provenance_path"] = "provenance_path"
    artifact_id: str | None = None
    evidence_id: str | None = None
    evidence_status: EvidenceStatus = "NON_AUTHORITATIVE"
    nodes: tuple[ProvenanceNode, ...]
    edges: tuple[ProvenanceEdge, ...]
    evidence_records: tuple[EvidenceRecord, ...]
    authority: ProductUiAuthoritySnapshot


class EvidenceBrowserView(SchemaModel):
    ui_version: str = PRODUCT_UI_VERSION
    contract_version: str = PRODUCT_UI_CONTRACT_VERSION
    schema_version: str = PRODUCT_UI_SCHEMA_VERSION
    route_id: Literal["evidence_browser"] = "evidence_browser"
    total_evidence_records: int
    evidence_records: tuple[EvidenceRecord, ...]
    evidence_status: EvidenceStatus = "NON_AUTHORITATIVE"
    authority: ProductUiAuthoritySnapshot
