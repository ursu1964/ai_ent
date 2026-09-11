from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

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
from ai_ent_product_api import PRODUCT_API_PREFIX, create_app
from ai_ent_product_api.errors import (
    ProductApiError,
    validation_error_response,
)
from ai_ent_product_api.schemas import (
    PRODUCT_API_DECISION_DEPENDENCIES,
    GovernanceValidationRequest,
)
from ai_ent_product_api.services import (
    GovernanceValidationService,
    ProductApiServices,
    ProductBoundaryService,
    ProductRuntimeSnapshotService,
)
from tests import test_product_runtime_handoff as runtime_handoff_tests


def test_boundary_snapshot_preserves_control_plane_authority_and_gates() -> None:
    services = ProductApiServices.defaults()
    snapshot = services.boundary()

    assert snapshot.control_plane_authority == CONTROL_PLANE_AUTHORITY
    assert snapshot.authority_mode == "facade_only"
    assert snapshot.grants_control_plane_authority is False
    assert snapshot.gate_approval_authority is False
    assert snapshot.verifier_bypass_authority is False
    assert snapshot.commit_boundary_bypass_authority is False
    assert snapshot.scheduling_authority is False
    assert snapshot.runtime_execution_authority is False
    assert snapshot.policy_weakening_authority is False
    assert snapshot.implicit_human_gate_approval is False
    assert snapshot.independent_verification_required is True
    assert snapshot.mandatory_verification_commands == MANDATORY_VERIFICATION_COMMANDS
    assert snapshot.decision_dependencies == PRODUCT_API_DECISION_DEPENDENCIES
    assert "DECISION_REQUIRED:PRD-DEC-001" in snapshot.decision_dependencies
    assert "approve_gate" in snapshot.denied_operations
    assert "schedule_execution" in snapshot.denied_operations
    assert snapshot.secret_values_exposed is False


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
        f"{PRODUCT_API_PREFIX}/boundary",
        f"{PRODUCT_API_PREFIX}/governance/requests/validate",
        f"{PRODUCT_API_PREFIX}/health",
        f"{PRODUCT_API_PREFIX}/openapi.json",
        f"{PRODUCT_API_PREFIX}/plans/generated",
        f"{PRODUCT_API_PREFIX}/plans/generated/dag",
        f"{PRODUCT_API_PREFIX}/runtime/executions",
        f"{PRODUCT_API_PREFIX}/runtime/repairs",
        f"{PRODUCT_API_PREFIX}/runtime/state",
        f"{PRODUCT_API_PREFIX}/runtime/tasks",
    }
    assert all(path.startswith(PRODUCT_API_PREFIX) for path in route_paths)
    assert not any(
        authority in path
        for path in route_paths
        for authority in ("approve", "commit", "execute/", "push", "schedule")
    )


def test_boundary_response_uses_versioned_schema_envelope() -> None:
    result = create_app().get(f"{PRODUCT_API_PREFIX}/boundary")

    assert result.status_code == 200
    payload = result.json()
    assert payload["api_version"] == "v1"
    assert payload["schema_version"] == "ai-ent-product-api-v1.0"
    assert payload["data"]["contract_version"] == "prd-task-005.1"
    assert payload["data"]["control_plane_authority"] == CONTROL_PLANE_AUTHORITY
    assert payload["data"]["mandatory_verification_commands"] == list(
        MANDATORY_VERIFICATION_COMMANDS
    )
    assert payload["data"]["decision_dependencies"] == ["DECISION_REQUIRED:PRD-DEC-001"]


def test_generated_plan_and_dag_routes_expose_read_only_product_artifacts() -> None:
    app = create_app()

    plan = app.get(f"{PRODUCT_API_PREFIX}/plans/generated").json()["data"]
    dag = app.get(f"{PRODUCT_API_PREFIX}/plans/generated/dag").json()["data"]

    assert plan["product_plan_id"] == PRODUCT_PLAN_ID
    assert plan["plan_state"] == "FROZEN"
    assert plan["task_count"] == 25
    assert plan["dependency_edge_count"] == 51
    assert plan["human_gate_count"] == 8
    assert plan["authority"]["grants_control_plane_authority"] is False
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
    return create_app(
        ProductApiServices(
            boundary_service=ProductBoundaryService(),
            governance_service=GovernanceValidationService(),
            runtime_service=ProductRuntimeSnapshotService(
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


def _validation_errors(payload: dict[str, Any]) -> list[Any]:
    try:
        GovernanceValidationRequest.model_validate(payload)
    except ValidationError as exc:
        return exc.errors()
    raise AssertionError("expected validation error")
