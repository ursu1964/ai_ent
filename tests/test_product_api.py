from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ai_ent.external_project import CONTROL_PLANE_AUTHORITY, MANDATORY_VERIFICATION_COMMANDS
from ai_ent_product_api import PRODUCT_API_PREFIX, create_app
from ai_ent_product_api.errors import (
    ProductApiError,
    validation_error_response,
)
from ai_ent_product_api.schemas import (
    PRODUCT_API_DECISION_DEPENDENCIES,
    GovernanceValidationRequest,
)
from ai_ent_product_api.services import ProductApiServices


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
    }
    assert all(path.startswith(PRODUCT_API_PREFIX) for path in route_paths)
    assert not any(
        authority in path
        for path in route_paths
        for authority in ("approve", "commit", "execute", "push", "schedule")
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
    assert {"input": "<redacted>", "loc": ["password"], "msg": "Extra inputs are not permitted", "type": "extra_forbidden", "url": "https://errors.pydantic.dev/2.13/v/extra_forbidden"} in payload["error"]["details"]["errors"]


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
    assert any(error.get("input") == "safe-value" for error in payload["error"]["details"]["errors"])


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


def _validation_errors(payload: dict[str, Any]) -> list[Any]:
    try:
        GovernanceValidationRequest.model_validate(payload)
    except ValidationError as exc:
        return exc.errors()
    raise AssertionError("expected validation error")
