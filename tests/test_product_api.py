from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ai_ent.external_project import CONTROL_PLANE_AUTHORITY, MANDATORY_VERIFICATION_COMMANDS
from ai_ent.persistence.models import (
    Checkpoint,
    Execution,
    RuntimeHumanGate,
    Task,
    TaskDependency,
    TaskLease,
    utc_now,
)
from ai_ent.product_plan_final_acceptance import PRODUCT_PLAN_ID
from ai_ent.product_runtime_handoff import ProductRuntimePlanImporter
from ai_ent.project_manifest import PROJECT_MANIFEST_ROOT as MANIFEST_ROOT
from ai_ent_product_api import PRODUCT_API_PREFIX, create_app
from ai_ent_product_api.errors import (
    ProductApiError,
    validation_error_response,
)
from ai_ent_product_api.schemas import (
    PRODUCT_API_ACCESS_DECISION_DEPENDENCIES,
    PRODUCT_API_DECISION_DEPENDENCIES,
    GovernanceValidationRequest,
    RuntimeComponentStatus,
)
from ai_ent_product_api.services import (
    GovernanceValidationService,
    ProductApiServices,
    ProductBoundaryService,
    ProductHumanApprovalService,
    ProductRuntimeSnapshotService,
    RuntimeProbeProvider,
    RuntimeStatusService,
)
from tests import test_product_runtime_handoff as runtime_handoff_tests

type ComponentName = Literal["readiness", "recovery", "docker", "codex", "postgresql"]
type ComponentState = Literal["available", "unavailable", "unknown"]
type ComponentProbe = Callable[[], RuntimeComponentStatus]


def test_boundary_snapshot_preserves_control_plane_authority_and_gates() -> None:
    services = ProductApiServices.defaults()
    snapshot = services.boundary()

    assert snapshot.control_plane_authority == CONTROL_PLANE_AUTHORITY
    assert snapshot.authority_mode == "facade_only"
    assert snapshot.operation_mode == "read_only_projection"
    assert snapshot.grants_control_plane_authority is False
    assert snapshot.gate_approval_authority is False
    assert snapshot.verifier_bypass_authority is False
    assert snapshot.commit_boundary_bypass_authority is False
    assert snapshot.scheduling_authority is False
    assert snapshot.runtime_execution_authority is False
    assert snapshot.repair_execution_authority is False
    assert snapshot.artifact_authority is False
    assert snapshot.policy_weakening_authority is False
    assert snapshot.mutating_operations_exposed is False
    assert snapshot.implicit_human_gate_approval is False
    assert snapshot.independent_verification_required is True
    assert snapshot.mandatory_verification_commands == MANDATORY_VERIFICATION_COMMANDS
    assert snapshot.decision_dependencies == PRODUCT_API_DECISION_DEPENDENCIES
    assert "DECISION_REQUIRED:PRD-DEC-001" in snapshot.decision_dependencies
    assert "read_artifact_metadata" in snapshot.allowed_operations
    assert "query_provenance" in snapshot.allowed_operations
    assert "read_human_gate_review" in snapshot.allowed_operations
    assert "validate_human_gate_evidence" in snapshot.allowed_operations
    assert "submit_human_gate_decision" in snapshot.allowed_operations
    assert "approve_gate" in snapshot.denied_operations
    assert "claim_task" in snapshot.denied_operations
    assert "execute_repair" in snapshot.denied_operations
    assert "record_evidence" in snapshot.denied_operations
    assert "schedule_execution" in snapshot.denied_operations
    assert snapshot.secret_values_exposed is False


def test_access_profile_is_explicit_local_first_and_decision_gated() -> None:
    services = ProductApiServices.defaults()
    snapshot = services.access_profile()

    assert snapshot.profile_id == "ACCESS-LOCAL-LAN-001"
    assert snapshot.active_profile == "local_loopback"
    assert snapshot.configured_profiles == ("local_loopback", "lan_gated")
    assert snapshot.binding.bind_address == "127.0.0.1"
    assert snapshot.binding.port == 8000
    assert snapshot.binding.lan_bind_address == "explicit_private_interface_required"
    assert snapshot.binding.effective_bind_address == "127.0.0.1"
    assert snapshot.binding.network_scope == "local_loopback"
    assert snapshot.binding.lan_access_enabled is False
    assert snapshot.binding.blocked_by_decisions == PRODUCT_API_ACCESS_DECISION_DEPENDENCIES
    assert snapshot.reverse_proxy.enabled is False
    assert snapshot.reverse_proxy.provider == "none"
    assert snapshot.reverse_proxy.allowed_providers == ("caddy", "nginx")
    assert snapshot.reverse_proxy.decision_dependency == "DECISION_REQUIRED:PRD-DEC-002"
    assert snapshot.tls.enabled is False
    assert snapshot.tls.mode == "disabled_for_loopback"
    assert snapshot.tls.required_for_lan is True
    assert snapshot.firewall_assumptions.host_firewall_managed_externally is True
    assert snapshot.firewall_assumptions.inbound_lan_ports_allowed == ()
    assert snapshot.firewall_assumptions.operator_confirmation_required_for_changes is True
    assert snapshot.explicit_human_review_required is True
    assert snapshot.human_gate_id == "GATE-PRD-LAN-ACCESS"
    assert snapshot.independent_verification_required is True
    assert snapshot.mandatory_verification_commands == MANDATORY_VERIFICATION_COMMANDS
    assert snapshot.decision_dependencies == PRODUCT_API_ACCESS_DECISION_DEPENDENCIES
    assert snapshot.grants_control_plane_authority is False
    assert snapshot.gate_approval_authority is False
    assert snapshot.verifier_bypass_authority is False
    assert snapshot.scheduling_authority is False
    assert snapshot.runtime_execution_authority is False
    assert snapshot.credential_values_exposed is False
    assert snapshot.authority.grants_control_plane_authority is False


def test_governance_validation_service_reuses_existing_contract_boundaries() -> None:
    result = ProductApiServices.defaults().validate_governance_request(
        GovernanceValidationRequest(
            request_id="PRODUCT-API-GOV-001",
            actor_id="api-client",
            domain="evolution",
            evolution_type="technical",
            summary="Check that the API facade cannot approve gates or bypass verification.",
            target_manifest_paths=("manifest/project/ai-ent/capabilities.yaml",),
            requested_authorities=(
                "recommend",
                "approve_gate",
                "bypass_verifier",
                "schedule_execution",
            ),
            learning_sources=("approved_manifests",),
            evidence_refs=("PRD-TASK-005:product-api-shell",),
        )
    )

    assert result.status == "REJECTED"
    assert result.accepted_authorities == ()
    assert set(result.blockers) >= {
        "prohibited authority requested:approve_gate",
        "prohibited authority requested:bypass_verifier",
        "prohibited authority requested:schedule_execution",
    }
    assert result.summary.gate_approval_granted is False
    assert result.summary.verifier_bypass_granted is False
    assert result.summary.self_scheduling_granted is False


def test_api_shell_exposes_only_versioned_non_authorizing_routes() -> None:
    app = create_app()
    route_paths = {route.path for route in app.routes}

    assert route_paths == {
        f"{PRODUCT_API_PREFIX}/access-profile",
        f"{PRODUCT_API_PREFIX}/artifacts/metadata",
        f"{PRODUCT_API_PREFIX}/boundary",
        f"{PRODUCT_API_PREFIX}/governance/requests/validate",
        f"{PRODUCT_API_PREFIX}/health",
        f"{PRODUCT_API_PREFIX}/human-gates/{{gate_id}}/approve",
        f"{PRODUCT_API_PREFIX}/human-gates/{{gate_id}}/decisions",
        f"{PRODUCT_API_PREFIX}/human-gates/{{gate_id}}/evidence/validate",
        f"{PRODUCT_API_PREFIX}/human-gates/{{gate_id}}/reject",
        f"{PRODUCT_API_PREFIX}/human-gates/{{gate_id}}/review",
        f"{PRODUCT_API_PREFIX}/human-gates/reviews",
        f"{PRODUCT_API_PREFIX}/openapi.json",
        f"{PRODUCT_API_PREFIX}/plans/generated",
        f"{PRODUCT_API_PREFIX}/plans/generated/dag",
        f"{PRODUCT_API_PREFIX}/provenance/requirements/{{requirement_id}}",
        f"{PRODUCT_API_PREFIX}/projects/current",
        f"{PRODUCT_API_PREFIX}/projects/current/architecture",
        f"{PRODUCT_API_PREFIX}/projects/current/capabilities/review",
        f"{PRODUCT_API_PREFIX}/projects/current/intake",
        f"{PRODUCT_API_PREFIX}/projects/current/requirements",
        f"{PRODUCT_API_PREFIX}/runtime/executions",
        f"{PRODUCT_API_PREFIX}/runtime/codex",
        f"{PRODUCT_API_PREFIX}/runtime/docker",
        f"{PRODUCT_API_PREFIX}/runtime/postgresql",
        f"{PRODUCT_API_PREFIX}/runtime/readiness",
        f"{PRODUCT_API_PREFIX}/runtime/recovery",
        f"{PRODUCT_API_PREFIX}/runtime/repairs",
        f"{PRODUCT_API_PREFIX}/runtime/state",
        f"{PRODUCT_API_PREFIX}/runtime/status",
        f"{PRODUCT_API_PREFIX}/runtime/tasks",
    }
    assert all(path.startswith(PRODUCT_API_PREFIX) for path in route_paths)
    assert not any(
        authority in path
        for path in route_paths
        for authority in ("commit", "execute/", "push", "schedule")
    )
    boundary = create_app().get(f"{PRODUCT_API_PREFIX}/boundary").json()["data"]
    assert "approve_gate" in boundary["denied_operations"]


def test_access_profile_route_uses_versioned_schema_envelope() -> None:
    result = create_app().get(f"{PRODUCT_API_PREFIX}/access-profile")

    assert result.status_code == 200
    payload = result.json()
    assert payload["api_version"] == "v1"
    assert payload["schema_version"] == "ai-ent-product-api-v1.0"
    assert payload["data"]["contract_version"] == "prd-task-011.1"
    assert payload["data"]["binding"] == {
        "bind_address": "127.0.0.1",
        "blocked_by_decisions": ["DECISION_REQUIRED:PRD-DEC-002"],
        "effective_bind_address": "127.0.0.1",
        "lan_bind_address": "explicit_private_interface_required",
        "lan_access_enabled": False,
        "network_scope": "local_loopback",
        "port": 8000,
    }
    assert payload["data"]["reverse_proxy"]["enabled"] is False
    assert payload["data"]["reverse_proxy"]["provider"] == "none"
    assert payload["data"]["tls"]["required_for_lan"] is True
    assert payload["data"]["firewall_assumptions"]["inbound_lan_ports_allowed"] == []
    assert payload["data"]["decision_dependencies"] == ["DECISION_REQUIRED:PRD-DEC-002"]
    assert payload["data"]["authority"]["gate_approval_authority"] is False
    assert payload["data"]["authority"]["independent_verification_required"] is True


def test_boundary_response_uses_versioned_schema_envelope() -> None:
    result = create_app().get(f"{PRODUCT_API_PREFIX}/boundary")

    assert result.status_code == 200
    payload = result.json()
    assert payload["api_version"] == "v1"
    assert payload["schema_version"] == "ai-ent-product-api-v1.0"
    assert payload["data"]["contract_version"] == "prd-task-011.1"
    assert payload["data"]["control_plane_authority"] == CONTROL_PLANE_AUTHORITY
    assert payload["data"]["mandatory_verification_commands"] == list(
        MANDATORY_VERIFICATION_COMMANDS
    )
    assert payload["data"]["decision_dependencies"] == ["DECISION_REQUIRED:PRD-DEC-001"]


def test_project_review_endpoints_expose_bounded_manifest_sections() -> None:
    app = create_app()
    project = app.get(f"{PRODUCT_API_PREFIX}/projects/current").json()["data"]
    intake = app.get(f"{PRODUCT_API_PREFIX}/projects/current/intake").json()["data"]
    requirements = app.get(
        f"{PRODUCT_API_PREFIX}/projects/current/requirements"
    ).json()["data"]
    architecture = app.get(
        f"{PRODUCT_API_PREFIX}/projects/current/architecture"
    ).json()["data"]

    assert project["project_id"] == "PRJ-AI-ENT"
    assert project["project"]["id"] == "PRJ-AI-ENT"
    assert project["project"]["source_manifest_file"] == "project.yaml"
    assert len(project["project_hash"]) == 64
    assert len(project["source_compiled_hash"]) == 64
    assert intake["project_id"] == "PRJ-AI-ENT"
    assert intake["intake"]["source_manifest_file"] == "intake.yaml"
    assert len(intake["intake_hash"]) == 64
    assert requirements["requirements_count"] == len(requirements["requirements"])
    assert any(item["id"] == "FR-002" for item in requirements["requirements"])
    assert len(requirements["requirements_hash"]) == 64
    assert architecture["component_count"] == len(architecture["architecture"]["components"])
    assert architecture["interface_count"] == len(architecture["architecture"]["interfaces"])
    assert architecture["data_object_count"] == len(architecture["architecture"]["data_objects"])
    assert len(architecture["architecture_hash"]) == 64
    for payload in (project, intake, requirements, architecture):
        _assert_bounded_review(payload["review_boundary"])


def test_capability_review_requires_independent_verification() -> None:
    result = create_app().get(
        f"{PRODUCT_API_PREFIX}/projects/current/capabilities/review"
    )

    assert result.status_code == 200
    payload = result.json()["data"]
    _assert_bounded_review(payload["review_boundary"])
    assert payload["project_id"] == "PRJ-AI-ENT"
    assert payload["independent_verification_required"] is True
    assert payload["mandatory_verification_commands"] == list(MANDATORY_VERIFICATION_COMMANDS)
    assert payload["capability_count"] == len(payload["capabilities"])
    assert len(payload["capability_resolution_hash"]) == 64
    assert set(payload["blocked_capabilities"]).issubset(
        {item["capability_id"] for item in payload["capabilities"]}
    )


def test_generated_plan_and_dag_routes_expose_read_only_product_artifacts() -> None:
    app = create_app()

    plan = app.get(f"{PRODUCT_API_PREFIX}/plans/generated").json()["data"]
    dag = app.get(f"{PRODUCT_API_PREFIX}/plans/generated/dag").json()["data"]

    assert plan["product_plan_id"] == PRODUCT_PLAN_ID
    assert plan["plan_state"] == "FROZEN"
    assert plan["task_count"] == 25
    assert plan["dependency_edge_count"] == 51
    assert plan["human_gate_count"] == 8
    assert plan["authority"]["operation_mode"] == "read_only_projection"
    assert plan["authority"]["control_plane_authority"] == CONTROL_PLANE_AUTHORITY
    assert plan["authority"]["grants_control_plane_authority"] is False
    assert plan["authority"]["mutating_operations_exposed"] is False
    assert plan["authority"]["runtime_execution_authority"] is False
    assert plan["authority"]["repair_execution_authority"] is False
    assert plan["authority"]["implicit_human_gate_approval"] is False
    assert plan["authority"]["independent_verification_required"] is True
    assert plan["authority"]["mandatory_verification_commands"] == list(
        MANDATORY_VERIFICATION_COMMANDS
    )
    assert "approve_gate" in plan["authority"]["denied_operations"]
    assert "execute_repair" in plan["authority"]["denied_operations"]
    assert dag["dependency_edges"][0] == {
        "dependency_task_id": "PRD-TASK-001",
        "task_id": "PRD-TASK-002",
    }
    assert dag["human_gates"][0]["status"] == "PENDING_NOT_APPROVED"


def test_health_response_includes_runtime_status_dependency() -> None:
    result = create_app(_services_with_runtime_probes()).get(f"{PRODUCT_API_PREFIX}/health")

    assert result.status_code == 200
    dependencies = result.json()["data"]["dependencies"]
    assert {
        "name": "runtime_status",
        "boundary": "read-only runtime component status facade",
        "status": "wired",
    } in dependencies


def test_runtime_status_preserves_authority_gates_and_verification_requirements() -> None:
    result = create_app(_services_with_runtime_probes()).get(f"{PRODUCT_API_PREFIX}/runtime/status")

    assert result.status_code == 200
    data = result.json()["data"]
    assert data["control_plane_authority"] == CONTROL_PLANE_AUTHORITY
    assert data["authority_mode"] == "facade_only"
    assert data["operation_mode"] == "read_only_projection"
    assert data["grants_control_plane_authority"] is False
    assert data["gate_approval_authority"] is False
    assert data["verifier_bypass_authority"] is False
    assert data["commit_boundary_bypass_authority"] is False
    assert data["scheduling_authority"] is False
    assert data["runtime_execution_authority"] is False
    assert data["repair_execution_authority"] is False
    assert data["artifact_authority"] is False
    assert data["policy_weakening_authority"] is False
    assert data["mutating_operations_exposed"] is False
    assert data["implicit_human_gate_approval"] is False
    assert data["independent_verification_required"] is True
    assert data["mandatory_verification_commands"] == list(MANDATORY_VERIFICATION_COMMANDS)
    assert data["decision_dependencies"] == ["DECISION_REQUIRED:PRD-DEC-001"]
    assert data["secret_values_exposed"] is False

    for component_name in ("readiness", "recovery", "docker", "codex", "postgresql"):
        component = data[component_name]
        assert component["passive_probe"] is True
        assert component["actions_performed"] is False
        assert component["grants_control_plane_authority"] is False
        assert component["gate_approval_authority"] is False
        assert component["verifier_bypass_authority"] is False
        assert component["runtime_execution_authority"] is False
        assert component["secret_values_exposed"] is False


def test_runtime_component_routes_return_deterministic_component_statuses() -> None:
    app = create_app(_services_with_runtime_probes())

    readiness = app.get(f"{PRODUCT_API_PREFIX}/runtime/readiness").json()["data"]
    recovery = app.get(f"{PRODUCT_API_PREFIX}/runtime/recovery").json()["data"]

    assert readiness["status"] == "available"
    assert recovery["status"] == "available"
    assert app.get(f"{PRODUCT_API_PREFIX}/runtime/docker").json()["data"]["details"] == {
        "docker_command_available": True,
        "docker_compose_command_available": False,
    }
    assert app.get(f"{PRODUCT_API_PREFIX}/runtime/codex").json()["data"]["details"] == {
        "codex_command_available": True
    }
    assert app.get(f"{PRODUCT_API_PREFIX}/runtime/postgresql").json()["data"]["details"] == {
        "configured": True,
        "health": "ok",
    }


def test_runtime_status_redacts_probe_details_and_suppresses_probe_exceptions() -> None:
    def docker_with_secret_details() -> RuntimeComponentStatus:
        return _component(
            "docker",
            "available",
            details={
                "api_token": "token-value",
                "message": "Bearer nested-secret-token",
                "nested": {"password": "password-value", "safe": "visible"},
            },
        )

    def codex_raises_with_secret() -> RuntimeComponentStatus:
        raise RuntimeError("Bearer secret-token")

    services = _services_with_runtime_probes(
        docker=docker_with_secret_details,
        codex=codex_raises_with_secret,
    )

    payload = create_app(services).get(f"{PRODUCT_API_PREFIX}/runtime/status").json()

    assert "token-value" not in str(payload)
    assert "password-value" not in str(payload)
    assert "nested-secret-token" not in str(payload)
    assert "Bearer secret-token" not in str(payload)
    assert payload["data"]["docker"]["details"]["api_token"] == "<redacted>"
    assert payload["data"]["docker"]["details"]["message"] == "<redacted>"
    assert payload["data"]["docker"]["details"]["nested"]["password"] == "<redacted>"
    assert payload["data"]["docker"]["details"]["nested"]["safe"] == "visible"
    assert payload["data"]["codex"]["status"] == "unknown"
    assert payload["data"]["codex"]["details"] == {"error": "status probe failed"}


def test_runtime_task_execution_repair_and_state_routes_are_observational() -> None:
    app = _seeded_product_runtime_app()

    tasks = app.get(f"{PRODUCT_API_PREFIX}/runtime/tasks").json()["data"]
    executions = app.get(f"{PRODUCT_API_PREFIX}/runtime/executions").json()["data"]
    repairs = app.get(f"{PRODUCT_API_PREFIX}/runtime/repairs").json()["data"]
    state = app.get(f"{PRODUCT_API_PREFIX}/runtime/state").json()["data"]

    task = _by_id(tasks["tasks"], "task_id", "PRD-TASK-008")
    assert task["status"] == "pending"
    assert task["schedulable"] is True
    assert task["readiness_status"] == "NOT_SCHEDULABLE"
    assert task["readiness_reasons"] == ["pending_human_gate"]
    assert "DECISION_REQUIRED:PRD-DEC-002" not in task["required_decisions"]
    assert "DECISION_REQUIRED:PRD-DEC-003" not in task["required_decisions"]
    assert "tests/**" in task["allowed_write_scope"]
    assert ".env" in task["prohibited_paths"]

    execution = _by_id(executions["executions"], "execution_id", "execution-product-api-1")
    assert execution["task_id"] == "PRD-TASK-008"
    assert execution["status"] == "failed"
    assert execution["error_classification"] == "verification_failed"
    assert execution["checkpoint_count"] == 1
    assert execution["active_lease"] is False
    assert "token-value" not in str(executions)
    assert "password-value" not in str(executions)

    repair = _by_id(repairs["repairs"], "failed_execution_id", "execution-product-api-1")
    assert repair["category"] == "VERIFICATION_FAILURE"
    assert repair["stage"] == "VERIFICATION"
    assert repair["retryability"] == "REPAIRABLE"
    assert repair["recommended_action"] == "REPAIR_WITH_NEW_EXECUTION"
    assert repair["creates_execution"] is False
    assert repair["grants_repair_authority"] is False
    assert repairs["authority"]["repair_execution_authority"] is False

    assert state["import_status"] == "imported"
    assert state["imported_tasks"] == 25
    assert state["dependency_edges"] == 51
    assert state["human_gates"] == 8
    assert state["pending_human_gates"] == 8
    assert state["executions"] == 2
    assert state["failed_executions"] == 1
    assert state["active_leases"] == 1
    assert state["authority"]["gate_approval_authority"] is False
    assert state["authority"]["scheduling_authority"] is False
    for collection in (tasks, executions, repairs, state):
        assert collection["authority"]["operation_mode"] == "read_only_projection"
        assert collection["authority"]["control_plane_authority"] == CONTROL_PLANE_AUTHORITY
        assert collection["authority"]["mutating_operations_exposed"] is False
        assert collection["authority"]["independent_verification_required"] is True
        assert collection["authority"]["secret_values_exposed"] is False
        assert "claim_task" in collection["authority"]["denied_operations"]
        assert "execute_runtime" in collection["authority"]["denied_operations"]
        assert "execute_repair" in collection["authority"]["denied_operations"]


def test_artifact_metadata_and_provenance_routes_are_redacted_and_non_authoritative() -> None:
    app = _seeded_product_runtime_app()

    metadata = app.get(f"{PRODUCT_API_PREFIX}/artifacts/metadata").json()["data"]
    provenance = app.get(
        f"{PRODUCT_API_PREFIX}/provenance/requirements/NFR-002"
    ).json()["data"]

    checkpoint_artifact = next(
        artifact
        for artifact in metadata["artifacts"]
        if artifact["source"] == "execution-checkpoint"
        and artifact["task_id"] == "PRD-TASK-008"
    )
    service_boundaries = {
        boundary["service"]: boundary["interface_id"]
        for boundary in metadata["service_boundaries"]
    }

    assert metadata["contract_version"] == "prd-task-011.1"
    assert metadata["graph_available"] is True
    assert metadata["output_authority_state"] == "NON_AUTHORITATIVE"
    assert metadata["payloads_redacted"] is True
    assert metadata["secret_values_exposed"] is False
    assert metadata["summary"]["output_authority_state"] == "NON_AUTHORITATIVE"
    assert metadata["summary"]["artifact_record_count"] == len(metadata["artifacts"])
    assert checkpoint_artifact["kind"] == "execution_evidence"
    assert checkpoint_artifact["execution_id"] == "execution-product-api-1"
    assert checkpoint_artifact["payload_redacted"] is True
    assert "payload" not in checkpoint_artifact
    assert len(checkpoint_artifact["payload_hash"]) == 64
    assert {
        "artifact-evidence-graph",
        "deterministic-validator",
        "project-memory",
        "runtime-kernel",
    } <= set(service_boundaries)
    assert metadata["authority"]["artifact_authority"] is False
    assert metadata["authority"]["gate_approval_authority"] is False
    assert metadata["authority"]["independent_verification_required"] is True
    assert metadata["authority"]["mandatory_verification_commands"] == list(
        MANDATORY_VERIFICATION_COMMANDS
    )
    assert "record_evidence" in metadata["authority"]["denied_operations"]

    assert provenance["query_kind"] == "requirement"
    assert provenance["requirement_id"] == "NFR-002"
    assert provenance["requirement_node_id"] == "requirement:NFR-002"
    assert provenance["output_authority_state"] == "NON_AUTHORITATIVE"
    assert provenance["payloads_redacted"] is True
    assert provenance["secret_values_exposed"] is False
    assert provenance["authority"]["artifact_authority"] is False
    assert provenance["authority"]["verifier_bypass_authority"] is False
    assert "record_evidence" in provenance["authority"]["denied_operations"]
    assert provenance["summary"]["node_count"] == len(provenance["nodes"])
    assert provenance["summary"]["edge_count"] == len(provenance["edges"])
    assert provenance["summary"]["edge_count"] > 0
    assert any(
        node["node_id"] == "requirement:NFR-002" and node["payload_redacted"] is True
        for node in provenance["nodes"]
    )
    assert all("payload" not in node for node in provenance["nodes"])
    assert "token-value" not in str(metadata)
    assert "password-value" not in str(metadata)
    assert "owner-token-value" not in str(metadata)
    assert "token-value" not in str(provenance)
    assert "password-value" not in str(provenance)


def test_runtime_task_readiness_projection_distinguishes_pending_human_gate() -> None:
    app = _seeded_product_runtime_app()

    tasks = app.get(f"{PRODUCT_API_PREFIX}/runtime/tasks").json()["data"]
    task = _by_id(tasks["tasks"], "task_id", "PRD-TASK-008")

    assert task["status"] == "pending"
    assert task["schedulable"] is True
    assert task["readiness_status"] == "NOT_SCHEDULABLE"
    assert task["readiness_reasons"] == ["pending_human_gate"]
    assert task["human_gate_ids"] == ["GATE-PRD-RUNTIME-CONTROL"]


def test_runtime_task_readiness_projection_distinguishes_unmet_dependencies() -> None:
    app = _seeded_product_runtime_app()

    tasks = app.get(f"{PRODUCT_API_PREFIX}/runtime/tasks").json()["data"]
    task = _by_id(tasks["tasks"], "task_id", "PRD-TASK-010")

    assert task["status"] == "pending"
    assert task["human_gate_ids"] == []
    assert task["readiness_status"] == "WAITING_DEPENDENCIES"
    assert task["readiness_reasons"] == ["dependency_not_complete"]


def test_runtime_task_readiness_projection_distinguishes_decision_blockers() -> None:
    app = _seeded_product_runtime_app(
        _make_task_otherwise_eligible("PRD-TASK-019", "GATE-PRD-LAN-ACCESS")
    )

    tasks = app.get(f"{PRODUCT_API_PREFIX}/runtime/tasks").json()["data"]
    task = _by_id(tasks["tasks"], "task_id", "PRD-TASK-019")

    assert task["status"] == "pending"
    assert task["readiness_status"] == "DECISION_BLOCKED"
    assert task["readiness_reasons"] == ["unresolved_decision:PRD-DEC-002"]
    assert task["required_decisions"] == ["DECISION_REQUIRED:PRD-DEC-002"]


def test_runtime_task_readiness_projection_distinguishes_ready_tasks() -> None:
    app = _seeded_product_runtime_app(
        _make_task_otherwise_eligible("PRD-TASK-008", "GATE-PRD-RUNTIME-CONTROL")
    )

    tasks = app.get(f"{PRODUCT_API_PREFIX}/runtime/tasks").json()["data"]
    task = _by_id(tasks["tasks"], "task_id", "PRD-TASK-008")

    assert task["status"] == "pending"
    assert task["readiness_status"] == "READY"
    assert task["readiness_reasons"] == ["claimable"]


def test_runtime_task_readiness_projection_distinguishes_terminal_tasks() -> None:
    def mark_passed(session: Session) -> None:
        _make_task_otherwise_eligible("PRD-TASK-008", "GATE-PRD-RUNTIME-CONTROL")(session)
        task = session.get(Task, "PRD-TASK-008")
        assert task is not None
        task.status = "passed"

    app = _seeded_product_runtime_app(mark_passed)

    tasks = app.get(f"{PRODUCT_API_PREFIX}/runtime/tasks").json()["data"]
    task = _by_id(tasks["tasks"], "task_id", "PRD-TASK-008")

    assert task["status"] == "passed"
    assert task["readiness_status"] == "TERMINAL"
    assert task["readiness_reasons"] == ["task_passed"]


def test_human_gate_review_and_evidence_routes_are_explicit_and_non_authorizing() -> None:
    app = _seeded_product_runtime_app()

    reviews = app.get(f"{PRODUCT_API_PREFIX}/human-gates/reviews").json()["data"]
    review = app.get(
        f"{PRODUCT_API_PREFIX}/human-gates/GATE-PRD-RUNTIME-CONTROL/review"
    ).json()["data"]
    validation = app.post(
        f"{PRODUCT_API_PREFIX}/human-gates/GATE-PRD-RUNTIME-CONTROL/evidence/validate",
        json_payload={
            "actor_id": "approver-1",
            "evidence_refs": review["expected_evidence"],
            "verification_commands": list(MANDATORY_VERIFICATION_COMMANDS),
            "scope_task_ids": ["PRD-TASK-008"],
        },
    ).json()["data"]

    assert reviews["pending_count"] == 8
    assert reviews["approved_count"] == 0
    assert review["gate_id"] == "GATE-PRD-RUNTIME-CONTROL"
    assert review["task_id"] == "PRD-TASK-008"
    assert review["status"] == "pending"
    assert review["explicit_human_review_required"] is True
    assert review["independent_verification_required"] is True
    assert review["authority"]["gate_approval_authority"] is False
    assert validation["valid"] is True
    assert validation["missing_expected_evidence"] == []
    assert validation["missing_mandatory_verification_commands"] == []
    assert validation["allowed_scope_task_ids"][0] == "PRD-TASK-008"
    assert "PRD-TASK-008" in validation["allowed_scope_task_ids"]
    assert validation["applies_runtime_gate_mutation"] is False
    assert validation["grants_gate_approval_authority"] is False
    assert validation["verifier_bypass_granted"] is False


def test_human_gate_approval_rejection_and_scoped_decisions_do_not_mutate_runtime_gate() -> None:
    factory = _seeded_product_runtime_session_factory()
    app = _product_runtime_app(factory)
    review = app.get(
        f"{PRODUCT_API_PREFIX}/human-gates/GATE-PRD-RUNTIME-CONTROL/review"
    ).json()["data"]
    payload = {
        "actor_id": "approver-1",
        "rationale": "Explicit review completed.",
        "evidence_refs": review["expected_evidence"],
        "verification_commands": list(MANDATORY_VERIFICATION_COMMANDS),
        "scope_task_ids": ["PRD-TASK-008"],
    }

    approval = app.post(
        f"{PRODUCT_API_PREFIX}/human-gates/GATE-PRD-RUNTIME-CONTROL/approve",
        json_payload=payload,
    ).json()["data"]
    rejection = app.post(
        f"{PRODUCT_API_PREFIX}/human-gates/GATE-PRD-RUNTIME-CONTROL/reject",
        json_payload=payload,
    ).json()["data"]
    scoped = app.post(
        f"{PRODUCT_API_PREFIX}/human-gates/GATE-PRD-RUNTIME-CONTROL/decisions",
        json_payload={**payload, "decision": "approve"},
    ).json()["data"]

    assert approval["requested_decision"] == "approve"
    assert rejection["requested_decision"] == "reject"
    assert scoped["requested_decision"] == "approve"
    assert approval["request_accepted"] is True
    assert approval["decision_status"] == "CONTROL_PLANE_REVIEW_REQUIRED"
    assert len(approval["decision_request_hash"]) == 64
    assert approval["scope_task_ids"] == ["PRD-TASK-008"]
    assert approval["allowed_scope_task_ids"][0] == "PRD-TASK-008"
    assert approval["runtime_gate_status_before"] == "pending"
    assert approval["runtime_gate_status_after"] == "pending"
    assert approval["control_plane_authority"] == CONTROL_PLANE_AUTHORITY
    assert approval["control_plane_review_required"] is True
    assert approval["applied_to_runtime"] is False
    assert approval["runtime_mutation_allowed"] is False
    assert approval["grants_gate_approval_authority"] is False
    assert approval["verifier_bypass_granted"] is False
    assert approval["scheduling_authority_granted"] is False
    assert approval["independent_verification_required"] is True

    with factory() as session:
        gate = session.get(RuntimeHumanGate, "GATE-PRD-RUNTIME-CONTROL")
        assert gate is not None
        assert gate.status == "pending"


def test_human_gate_decision_requires_mandatory_verification_and_valid_scope() -> None:
    app = _seeded_product_runtime_app()
    review = app.get(
        f"{PRODUCT_API_PREFIX}/human-gates/GATE-PRD-RUNTIME-CONTROL/review"
    ).json()["data"]
    result = app.post(
        f"{PRODUCT_API_PREFIX}/human-gates/GATE-PRD-RUNTIME-CONTROL/approve",
        json_payload={
            "actor_id": "approver-1",
            "rationale": "Attempt approval without independent verification.",
            "evidence_refs": review["expected_evidence"],
            "verification_commands": [],
            "scope_task_ids": ["PRD-TASK-999"],
        },
    ).json()["data"]

    assert result["request_accepted"] is False
    assert result["decision_status"] == "EVIDENCE_INCOMPLETE"
    assert result["evidence_valid"] is False
    assert result["missing_mandatory_verification_commands"] == list(
        MANDATORY_VERIFICATION_COMMANDS
    )
    assert result["unknown_scope_task_ids"] == ["PRD-TASK-999"]
    assert result["allowed_scope_task_ids"][0] == "PRD-TASK-008"
    assert result["runtime_gate_status_after"] == "pending"

    rejection = app.post(
        f"{PRODUCT_API_PREFIX}/human-gates/GATE-PRD-RUNTIME-CONTROL/reject",
        json_payload={
            "actor_id": "approver-1",
            "rationale": "Reject because expected evidence is incomplete.",
            "evidence_refs": [],
            "verification_commands": list(MANDATORY_VERIFICATION_COMMANDS),
            "scope_task_ids": ["PRD-TASK-008"],
        },
    ).json()["data"]

    assert rejection["request_accepted"] is True
    assert rejection["decision_status"] == "CONTROL_PLANE_REVIEW_REQUIRED"
    assert rejection["evidence_valid"] is False
    assert rejection["missing_expected_evidence"] == review["expected_evidence"]


def test_human_gate_decision_response_does_not_echo_secret_values() -> None:
    app = _seeded_product_runtime_app()
    review = app.get(
        f"{PRODUCT_API_PREFIX}/human-gates/GATE-PRD-RUNTIME-CONTROL/review"
    ).json()["data"]
    result = app.post(
        f"{PRODUCT_API_PREFIX}/human-gates/GATE-PRD-RUNTIME-CONTROL/approve",
        json_payload={
            "actor_id": "approver-1",
            "rationale": "token-value",
            "evidence_refs": [*review["expected_evidence"], "password-value"],
            "verification_commands": list(MANDATORY_VERIFICATION_COMMANDS),
            "scope_task_ids": ["PRD-TASK-008"],
        },
    )

    payload = result.json()

    assert result.status_code == 200
    assert "token-value" not in str(payload)
    assert "password-value" not in str(payload)
    assert payload["data"]["request_accepted"] is True


def test_governance_validation_route_preserves_explicit_human_and_verifier_gates() -> None:
    result = create_app().post(
        f"{PRODUCT_API_PREFIX}/governance/requests/validate",
        json_payload={
            "request_id": "PRODUCT-API-GOV-002",
            "actor_id": "api-client",
            "domain": "evolution",
            "evolution_type": "technical",
            "summary": "Reject API attempts to approve gates and bypass verification.",
            "target_manifest_paths": ["manifest/project/ai-ent/capabilities.yaml"],
            "requested_authorities": ["approve_gate", "bypass_verifier"],
            "learning_sources": ["approved_manifests"],
            "evidence_refs": ["PRD-TASK-005:product-api-route"],
        },
    )

    assert result.status_code == 200
    payload = result.json()
    assert payload["data"]["status"] == "REJECTED"
    assert payload["data"]["accepted_authorities"] == []
    assert set(payload["data"]["blockers"]) >= {
        "prohibited authority requested:approve_gate",
        "prohibited authority requested:bypass_verifier",
    }
    assert payload["data"]["summary"]["gate_approval_granted"] is False
    assert payload["data"]["summary"]["verifier_bypass_granted"] is False


def test_error_response_redacts_secret_values_from_details() -> None:
    error = ProductApiError(
        status_code=400,
        code="BAD_REQUEST",
        message="rejected",
        details={
            "api_token": "token-value",
            "nested": {
                "password": "password-value",
                "safe": "visible",
            },
        },
    )

    payload = error.as_response().model_dump(mode="json")

    assert payload["error"]["details"]["api_token"] == "<redacted>"
    assert payload["error"]["details"]["nested"]["password"] == "<redacted>"
    assert payload["error"]["details"]["nested"]["safe"] == "visible"
    assert "token-value" not in str(payload)
    assert "password-value" not in str(payload)


def test_unknown_routes_do_not_reflect_potentially_sensitive_paths() -> None:
    result = create_app().get(f"{PRODUCT_API_PREFIX}/token-value")
    payload = result.json()

    assert result.status_code == 404
    assert payload["error"]["code"] == "ROUTE_NOT_FOUND"
    assert "token-value" not in str(payload)


def test_unknown_provenance_requirement_does_not_reflect_query_text() -> None:
    result = _seeded_product_runtime_app().get(
        f"{PRODUCT_API_PREFIX}/provenance/requirements/token-value"
    )
    payload = result.json()

    assert result.status_code == 404
    assert payload["error"]["code"] == "PROVENANCE_REQUIREMENT_NOT_FOUND"
    assert "token-value" not in str(payload)


def test_request_validation_errors_redact_secret_values() -> None:
    result = create_app().post(
        f"{PRODUCT_API_PREFIX}/governance/requests/validate",
        json_payload={
            "request_id": "PRODUCT-API-GOV-003",
            "api_token": "token-value",
        },
    )

    payload = result.json()

    assert result.status_code == 422
    assert "token-value" not in str(payload)
    assert "<redacted>" in str(payload)


def test_human_gate_decision_submissions_require_explicit_actor_and_rationale() -> None:
    result = create_app().post(
        f"{PRODUCT_API_PREFIX}/human-gates/GATE-PRD-RUNTIME-CONTROL/approve",
        json_payload={
            "actor_id": "",
            "rationale": "",
            "evidence_refs": [],
            "verification_commands": list(MANDATORY_VERIFICATION_COMMANDS),
        },
    )
    payload = result.json()

    assert result.status_code == 422
    assert payload["error"]["code"] == "REQUEST_VALIDATION_FAILED"
    assert any(
        error["loc"] == ["actor_id"] and error["type"] == "string_too_short"
        for error in payload["error"]["details"]["errors"]
    )
    assert any(
        error["loc"] == ["rationale"] and error["type"] == "string_too_short"
        for error in payload["error"]["details"]["errors"]
    )


def test_validation_errors_redact_sensitive_extra_inputs_by_loc() -> None:
    errors = _validation_errors({"request_id": "REQ-001", "password": "password-value"})

    payload = validation_error_response(errors).model_dump(mode="json")

    assert "password-value" not in str(payload)
    assert {
        "input": "<redacted>",
        "loc": ["password"],
        "msg": "Extra inputs are not permitted",
        "type": "extra_forbidden",
        "url": "https://errors.pydantic.dev/2.13/v/extra_forbidden",
    } in payload["error"]["details"]["errors"]


def test_validation_errors_redact_access_and_refresh_tokens() -> None:
    errors = _validation_errors(
        {
            "request_id": "REQ-002",
            "access_token": "access-token-value",
            "refresh_token": "refresh-token-value",
        }
    )

    payload = validation_error_response(errors).model_dump(mode="json")

    assert "access-token-value" not in str(payload)
    assert "refresh-token-value" not in str(payload)
    assert str(payload).count("<redacted>") >= 2


def test_validation_errors_redact_client_secret_and_authorization_material() -> None:
    errors = _validation_errors(
        {
            "request_id": "REQ-003",
            "client_secret": "client-secret-value",
            "Authorization": "Bearer secret-token",
            "cookie": "sessionid=cookie-secret",
            "session": "session-secret",
        }
    )

    payload = validation_error_response(errors).model_dump(mode="json")

    assert "client-secret-value" not in str(payload)
    assert "Bearer secret-token" not in str(payload)
    assert "sessionid=cookie-secret" not in str(payload)
    assert "session-secret" not in str(payload)


def test_validation_errors_redact_nested_secret_fields_in_input() -> None:
    details = [
        {
            "type": "missing",
            "loc": ("actor_id",),
            "msg": "Field required",
            "input": {
                "safe": "visible",
                "nested": {
                    "api_key": "api-key-value",
                    "items": [{"private_key": "private-key-value"}],
                },
            },
        }
    ]

    payload = validation_error_response(details).model_dump(mode="json")

    assert payload["error"]["details"]["errors"][0]["input"]["safe"] == "visible"
    assert "api-key-value" not in str(payload)
    assert "private-key-value" not in str(payload)


def test_validation_errors_redact_mixed_case_and_alias_like_sensitive_fields() -> None:
    details = [
        {
            "type": "extra_forbidden",
            "loc": ("metadata.Client-Secret",),
            "msg": "Extra inputs are not permitted",
            "input": "alias-secret-value",
        },
        {
            "type": "extra_forbidden",
            "loc": ("PASSWD",),
            "msg": "Extra inputs are not permitted",
            "input": "passwd-value",
        },
    ]

    payload = validation_error_response(details).model_dump(mode="json")

    assert "alias-secret-value" not in str(payload)
    assert "passwd-value" not in str(payload)
    assert payload["error"]["details"]["errors"][0]["loc"] == ["metadata", "Client-Secret"]


def test_validation_errors_redact_sensitive_values_inside_ctx() -> None:
    details = [
        {
            "type": "value_error",
            "loc": ("api_key",),
            "msg": "Value error",
            "ctx": {"given": "api-key-value", "safe_code": "too_short"},
            "input": "api-key-value",
        }
    ]

    payload = validation_error_response(details).model_dump(mode="json")

    assert "api-key-value" not in str(payload)
    assert payload["error"]["details"]["errors"][0]["ctx"] == "<redacted>"
    assert payload["error"]["details"]["errors"][0]["input"] == "<redacted>"


def test_validation_errors_handle_multiple_errors_without_leaking_secret_values() -> None:
    errors = _validation_errors(
        {
            "request_id": "REQ-004",
            "api_token": "token-value",
            "safe_extra": "safe-value",
        }
    )

    payload = validation_error_response(errors).model_dump(mode="json")

    assert "token-value" not in str(payload)
    assert "safe-value" in str(payload)
    assert any(
        error.get("input") == "safe-value"
        for error in payload["error"]["details"]["errors"]
    )


def test_malformed_json_error_preserves_schema_without_request_echo() -> None:
    error = ProductApiError(
        status_code=400,
        code="INVALID_JSON",
        message="request body must be valid JSON",
    )

    payload = error.as_response().model_dump(mode="json")

    assert payload["error"]["code"] == "INVALID_JSON"
    assert payload["error"]["details"] == {}


def test_safe_non_sensitive_validation_input_remains_represented() -> None:
    errors = _validation_errors({"request_id": "REQ-005", "safe_extra": "safe-value"})

    payload = validation_error_response(errors).model_dump(mode="json")

    assert "safe-value" in str(payload)
    assert "<redacted>" not in str(payload)


def _seeded_product_runtime_app(
    mutator: Callable[[Session], None] | None = None,
) -> Any:
    factory = _seeded_product_runtime_session_factory()
    if mutator is not None:
        with factory() as session:
            mutator(session)
            session.commit()
    return _product_runtime_app(factory)


def _product_runtime_app(factory: sessionmaker[Session]) -> Any:
    return create_app(
        ProductApiServices(
            boundary_service=ProductBoundaryService(),
            governance_service=GovernanceValidationService(),
            runtime_service=ProductRuntimeSnapshotService(
                artifacts_loader=runtime_handoff_tests.artifacts,
                session_factory=factory,
            ),
            human_approval_service=ProductHumanApprovalService(
                artifacts_loader=runtime_handoff_tests.artifacts,
                session_factory=factory,
            ),
        )
    )


def _seeded_product_runtime_session_factory() -> sessionmaker[Session]:
    factory = runtime_handoff_tests.session_factory()
    artifacts = runtime_handoff_tests.artifacts()
    now = utc_now()
    with factory() as session:
        runtime_handoff_tests.seed_prior_and_residual_complete(session)
        result = ProductRuntimePlanImporter().import_product_plan(
            session,
            artifacts,
            require_clean_git=False,
        )
        assert result.status == "IMPORTED"
        session.add(
            Execution(
                id="execution-product-api-1",
                task_id="PRD-TASK-008",
                executor_type="codex",
                status="failed",
                attempt=1,
                terminal_state="failed",
                error_classification="verification_failed",
                candidate_tree_hash="c" * 64,
            )
        )
        session.add(
            Checkpoint(
                id="checkpoint-product-api-1",
                task_id="PRD-TASK-008",
                execution_id="execution-product-api-1",
                checkpoint_type="execution",
                state=(
                    '{"api_token":"token-value",'
                    '"password":"password-value",'
                    '"status":"verification_failed"}'
                ),
                tree_hash="t" * 64,
            )
        )
        session.add(
            Execution(
                id="execution-product-api-2",
                task_id="PRD-TASK-005",
                executor_type="codex",
                status="running",
                attempt=1,
            )
        )
        session.add(
            TaskLease(
                id="lease-product-api-1",
                task_id="PRD-TASK-005",
                execution_id="execution-product-api-2",
                owner_id="owner-token-value",
                status="active",
                acquired_at=now,
                renewed_at=now,
                expires_at=now + timedelta(minutes=30),
            )
        )
        session.commit()
    return factory


def _make_task_otherwise_eligible(
    task_id: str,
    gate_id: str | None = None,
) -> Callable[[Session], None]:
    def mutate(session: Session) -> None:
        for dependency in session.scalars(
            select(TaskDependency).where(TaskDependency.task_id == task_id)
        ).all():
            dependency_task = session.get(Task, dependency.depends_on_task_id)
            assert dependency_task is not None
            dependency_task.status = "passed"
        task = session.get(Task, task_id)
        assert task is not None
        task.status = "pending"
        if gate_id is not None:
            gate = session.get(RuntimeHumanGate, gate_id)
            assert gate is not None
            gate.status = "approved"

    return mutate


def _by_id(items: list[dict[str, Any]], key: str, value: str) -> dict[str, Any]:
    for item in items:
        if item[key] == value:
            return item
    raise AssertionError(f"missing {key}={value}")


def _services_with_runtime_probes(
    *,
    docker: ComponentProbe | None = None,
    codex: ComponentProbe | None = None,
    postgresql: ComponentProbe | None = None,
) -> ProductApiServices:
    probes = RuntimeProbeProvider(
        readiness=lambda: _component("readiness", "available"),
        recovery=lambda: _component("recovery", "available"),
        docker=docker
        or (
            lambda: _component(
                "docker",
                "available",
                details={
                    "docker_command_available": True,
                    "docker_compose_command_available": False,
                },
            )
        ),
        codex=codex
        or (
            lambda: _component(
                "codex",
                "available",
                details={"codex_command_available": True},
            )
        ),
        postgresql=postgresql
        or (
            lambda: _component(
                "postgresql",
                "available",
                details={"configured": True, "health": "ok"},
            )
        ),
    )
    return ProductApiServices(
        boundary_service=ProductBoundaryService(),
        governance_service=GovernanceValidationService(),
        runtime_service=ProductRuntimeSnapshotService(),
        status_service=RuntimeStatusService(probes=probes),
    )


def _component(
    name: ComponentName,
    status: ComponentState,
    *,
    details: dict[str, Any] | None = None,
) -> RuntimeComponentStatus:
    return RuntimeComponentStatus(
        name=name,
        boundary=f"{name} test probe",
        status=status,
        summary=f"{name} status",
        details=details or {},
    )


def _validation_errors(payload: dict[str, Any]) -> list[Any]:
    try:
        GovernanceValidationRequest.model_validate(payload)
    except ValidationError as exc:
        return exc.errors()
    raise AssertionError("expected validation error")


def _assert_bounded_review(boundary: Mapping[str, Any]) -> None:
    assert boundary["source_authority"] == str(MANIFEST_ROOT)
    assert boundary["control_plane_authority"] == CONTROL_PLANE_AUTHORITY
    assert boundary["authority_mode"] == "facade_only"
    assert boundary["grants_control_plane_authority"] is False
    assert boundary["gate_approval_authority"] is False
    assert boundary["verifier_bypass_authority"] is False
    assert boundary["commit_boundary_bypass_authority"] is False
    assert boundary["scheduling_authority"] is False
    assert boundary["runtime_execution_authority"] is False
    assert boundary["policy_weakening_authority"] is False
    assert boundary["implicit_human_gate_approval"] is False
    assert boundary["human_gate_policy"] == "explicit_control_plane_gate_required"
    assert boundary["independent_verification_required"] is True
    assert boundary["mandatory_verification_commands"] == list(MANDATORY_VERIFICATION_COMMANDS)
    assert boundary["decision_dependencies"] == ["DECISION_REQUIRED:PRD-DEC-001"]
    assert boundary["secret_values_exposed"] is False
    assert "approve_gate" in boundary["denied_operations"]
    assert "bypass_verifier" in boundary["denied_operations"]
    assert "schedule_execution" in boundary["denied_operations"]
