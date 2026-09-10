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

    async def __call__(self, scope: Mapping[str, Any], receive: AsgiReceive, send: AsgiSend) -> None:
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
            f"{PRODUCT_API_PREFIX}/governance/requests/validate": {
                "post": {"operationId": "validateGovernanceRequest"}
            },
        },
    }
