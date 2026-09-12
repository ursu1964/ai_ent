from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from ai_ent_product_api.errors import ProductApiError, validation_error_response
from ai_ent_product_api.schemas import (
    PRODUCT_API_CONTRACT_VERSION,
    PRODUCT_API_DECISION_DEPENDENCIES,
    PRODUCT_API_PREFIX,
    PRODUCT_API_SCHEMA_VERSION,
    PRODUCT_API_VERSION,
    GovernanceValidationRequest,
    HumanGateApprovalRequest,
    HumanGateEvidenceValidationRequest,
    HumanGateRejectionRequest,
    HumanGateScopedDecisionRequest,
    response,
)
from ai_ent_product_api.services import ProductApiServices

HttpMethod = Literal["GET", "POST"]
AsgiMessage = dict[str, Any]
AsgiReceive = Callable[[], Awaitable[AsgiMessage]]
AsgiSend = Callable[[AsgiMessage], Awaitable[None]]


@dataclass(frozen=True)
class ProductApiRoute:
    path: str
    method: HttpMethod
    name: str


@dataclass(frozen=True)
class ProductApiResult:
    status_code: int
    payload: Mapping[str, Any]

    def json(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.payload, sort_keys=True))


class ProductApiApp:
    def __init__(self, services: ProductApiServices) -> None:
        self._services = services
        self.routes = (
            ProductApiRoute(f"{PRODUCT_API_PREFIX}/health", "GET", "health"),
            ProductApiRoute(f"{PRODUCT_API_PREFIX}/boundary", "GET", "boundary"),
            ProductApiRoute(f"{PRODUCT_API_PREFIX}/plans/generated", "GET", "generated_plan"),
            ProductApiRoute(f"{PRODUCT_API_PREFIX}/plans/generated/dag", "GET", "generated_dag"),
            ProductApiRoute(f"{PRODUCT_API_PREFIX}/runtime/tasks", "GET", "tasks"),
            ProductApiRoute(f"{PRODUCT_API_PREFIX}/runtime/executions", "GET", "executions"),
            ProductApiRoute(f"{PRODUCT_API_PREFIX}/runtime/repairs", "GET", "repairs"),
            ProductApiRoute(f"{PRODUCT_API_PREFIX}/runtime/state", "GET", "runtime_state"),
            ProductApiRoute(f"{PRODUCT_API_PREFIX}/human-gates/reviews", "GET", "gate_reviews"),
            ProductApiRoute(
                f"{PRODUCT_API_PREFIX}/human-gates/{{gate_id}}/review",
                "GET",
                "gate_review",
            ),
            ProductApiRoute(
                f"{PRODUCT_API_PREFIX}/human-gates/{{gate_id}}/evidence/validate",
                "POST",
                "validate_gate_evidence",
            ),
            ProductApiRoute(
                f"{PRODUCT_API_PREFIX}/human-gates/{{gate_id}}/approve",
                "POST",
                "submit_gate_approval",
            ),
            ProductApiRoute(
                f"{PRODUCT_API_PREFIX}/human-gates/{{gate_id}}/reject",
                "POST",
                "submit_gate_rejection",
            ),
            ProductApiRoute(
                f"{PRODUCT_API_PREFIX}/human-gates/{{gate_id}}/decisions",
                "POST",
                "scoped_gate_decision",
            ),
            ProductApiRoute(
                f"{PRODUCT_API_PREFIX}/governance/requests/validate",
                "POST",
                "validate_governance_request",
            ),
            ProductApiRoute(f"{PRODUCT_API_PREFIX}/openapi.json", "GET", "contract_document"),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json_payload: Mapping[str, Any] | None = None,
    ) -> ProductApiResult:
        try:
            return ProductApiResult(
                status_code=200,
                payload=self._route(method=method.upper(), path=path, json_payload=json_payload),
            )
        except ProductApiError as exc:
            return ProductApiResult(
                status_code=exc.status_code,
                payload=exc.as_response().model_dump(mode="json"),
            )
        except ValidationError as exc:
            return ProductApiResult(
                status_code=422,
                payload=validation_error_response(exc.errors()).model_dump(mode="json"),
            )

    def get(self, path: str) -> ProductApiResult:
        return self.request("GET", path)

    def post(self, path: str, *, json_payload: Mapping[str, Any] | None = None) -> ProductApiResult:
        return self.request("POST", path, json_payload=json_payload)

    async def __call__(
        self,
        scope: Mapping[str, Any],
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        if scope.get("type") != "http":
            raise ProductApiError(
                status_code=500,
                code="UNSUPPORTED_SCOPE",
                message="product API shell only supports HTTP scopes",
            )

        try:
            json_payload = await _request_json(receive)
            result = self.request(
                method=str(scope.get("method", "")),
                path=str(scope.get("path", "")),
                json_payload=json_payload,
            )
        except ProductApiError as exc:
            result = ProductApiResult(
                status_code=exc.status_code,
                payload=exc.as_response().model_dump(mode="json"),
            )
        body = json.dumps(result.payload, sort_keys=True).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": result.status_code,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _route(
        self,
        *,
        method: str,
        path: str,
        json_payload: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if method == "GET" and path == f"{PRODUCT_API_PREFIX}/health":
            return response(self._services.health()).model_dump(mode="json")
        if method == "GET" and path == f"{PRODUCT_API_PREFIX}/boundary":
            return response(self._services.boundary()).model_dump(mode="json")
        if method == "GET" and path == f"{PRODUCT_API_PREFIX}/plans/generated":
            return response(self._services.generated_plan()).model_dump(mode="json")
        if method == "GET" and path == f"{PRODUCT_API_PREFIX}/plans/generated/dag":
            return response(self._services.generated_dag()).model_dump(mode="json")
        if method == "GET" and path == f"{PRODUCT_API_PREFIX}/runtime/tasks":
            return response(self._services.tasks()).model_dump(mode="json")
        if method == "GET" and path == f"{PRODUCT_API_PREFIX}/runtime/executions":
            return response(self._services.executions()).model_dump(mode="json")
        if method == "GET" and path == f"{PRODUCT_API_PREFIX}/runtime/repairs":
            return response(self._services.repairs()).model_dump(mode="json")
        if method == "GET" and path == f"{PRODUCT_API_PREFIX}/runtime/state":
            return response(self._services.runtime_state()).model_dump(mode="json")
        if method == "GET" and path == f"{PRODUCT_API_PREFIX}/human-gates/reviews":
            return response(self._services.human_gate_reviews()).model_dump(mode="json")
        if (gate_id := _gate_route(path, "review")) and method == "GET":
            return response(self._services.human_gate_review(gate_id)).model_dump(mode="json")
        if (gate_id := _gate_route(path, "evidence/validate")) and method == "POST":
            request = HumanGateEvidenceValidationRequest.model_validate(json_payload or {})
            return response(
                self._services.validate_human_gate_evidence(gate_id, request)
            ).model_dump(mode="json")
        if (gate_id := _gate_route(path, "approve")) and method == "POST":
            request = HumanGateApprovalRequest.model_validate(json_payload or {})
            return response(self._services.approve_human_gate(gate_id, request)).model_dump(
                mode="json"
            )
        if (gate_id := _gate_route(path, "reject")) and method == "POST":
            request = HumanGateRejectionRequest.model_validate(json_payload or {})
            return response(self._services.reject_human_gate(gate_id, request)).model_dump(
                mode="json"
            )
        if (gate_id := _gate_route(path, "decisions")) and method == "POST":
            request = HumanGateScopedDecisionRequest.model_validate(json_payload or {})
            return response(
                self._services.scoped_human_gate_decision(gate_id, request)
            ).model_dump(mode="json")
        if method == "GET" and path == f"{PRODUCT_API_PREFIX}/openapi.json":
            return _contract_document()
        if method == "POST" and path == f"{PRODUCT_API_PREFIX}/governance/requests/validate":
            request = GovernanceValidationRequest.model_validate(json_payload or {})
            return response(self._services.validate_governance_request(request)).model_dump(
                mode="json"
            )
        raise ProductApiError(
            status_code=404,
            code="ROUTE_NOT_FOUND",
            message="route is not exposed by the product API boundary",
            details={"method": method},
        )


def create_app(services: ProductApiServices | None = None) -> ProductApiApp:
    return ProductApiApp(services or ProductApiServices.defaults())


async def _request_json(receive: AsgiReceive) -> Mapping[str, Any] | None:
    body_parts: list[bytes] = []
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            break
        body = message.get("body", b"")
        if isinstance(body, bytes):
            body_parts.append(body)
        if not bool(message.get("more_body", False)):
            break

    if not body_parts:
        return None

    try:
        decoded = json.loads(b"".join(body_parts).decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ProductApiError(
            status_code=400,
            code="INVALID_JSON",
            message="request body must be valid JSON",
        ) from exc
    if not isinstance(decoded, Mapping):
        raise ProductApiError(
            status_code=422,
            code="REQUEST_VALIDATION_FAILED",
            message="request body must be a JSON object",
        )
    return decoded


def _gate_route(path: str, action: str) -> str | None:
    prefix = f"{PRODUCT_API_PREFIX}/human-gates/"
    suffix = f"/{action}"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    gate_id = path[len(prefix) : -len(suffix)]
    if not gate_id or "/" in gate_id:
        return None
    return gate_id


def _contract_document() -> dict[str, Any]:
    return {
        "api_version": PRODUCT_API_VERSION,
        "contract_version": PRODUCT_API_CONTRACT_VERSION,
        "decision_dependencies": list(PRODUCT_API_DECISION_DEPENDENCIES),
        "openapi": "3.1.0",
        "schema_version": PRODUCT_API_SCHEMA_VERSION,
        "info": {
            "title": "AI Enterprise Product API",
            "version": PRODUCT_API_CONTRACT_VERSION,
        },
        "paths": {
            f"{PRODUCT_API_PREFIX}/health": {"get": {"operationId": "health"}},
            f"{PRODUCT_API_PREFIX}/boundary": {"get": {"operationId": "boundary"}},
            f"{PRODUCT_API_PREFIX}/plans/generated": {
                "get": {"operationId": "generatedPlan"}
            },
            f"{PRODUCT_API_PREFIX}/plans/generated/dag": {
                "get": {"operationId": "generatedDag"}
            },
            f"{PRODUCT_API_PREFIX}/runtime/tasks": {"get": {"operationId": "runtimeTasks"}},
            f"{PRODUCT_API_PREFIX}/runtime/executions": {
                "get": {"operationId": "runtimeExecutions"}
            },
            f"{PRODUCT_API_PREFIX}/runtime/repairs": {
                "get": {"operationId": "runtimeRepairs"}
            },
            f"{PRODUCT_API_PREFIX}/runtime/state": {"get": {"operationId": "runtimeState"}},
            f"{PRODUCT_API_PREFIX}/human-gates/reviews": {
                "get": {"operationId": "humanGateReviews"}
            },
            f"{PRODUCT_API_PREFIX}/human-gates/{{gate_id}}/review": {
                "get": {"operationId": "humanGateReview"}
            },
            f"{PRODUCT_API_PREFIX}/human-gates/{{gate_id}}/evidence/validate": {
                "post": {"operationId": "validateHumanGateEvidence"}
            },
            f"{PRODUCT_API_PREFIX}/human-gates/{{gate_id}}/approve": {
                "post": {"operationId": "submitHumanGateApproval"}
            },
            f"{PRODUCT_API_PREFIX}/human-gates/{{gate_id}}/reject": {
                "post": {"operationId": "submitHumanGateRejection"}
            },
            f"{PRODUCT_API_PREFIX}/human-gates/{{gate_id}}/decisions": {
                "post": {"operationId": "scopedHumanGateDecision"}
            },
            f"{PRODUCT_API_PREFIX}/governance/requests/validate": {
                "post": {"operationId": "validateGovernanceRequest"}
            },
        },
    }
