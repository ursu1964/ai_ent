from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qs

from pydantic import ValidationError

from ai_ent_product_ui.errors import ProductUiError, validation_error_response
from ai_ent_product_ui.rendering import render_artifacts, render_evidence, render_provenance
from ai_ent_product_ui.schemas import (
    PRODUCT_UI_PREFIX,
    DashboardRequest,
    LoginRequest,
    response,
)
from ai_ent_product_ui.services import ProductUiServices

HttpMethod = Literal["GET", "POST"]
AsgiMessage = dict[str, Any]
AsgiReceive = Callable[[], Awaitable[AsgiMessage]]
AsgiSend = Callable[[AsgiMessage], Awaitable[None]]


@dataclass(frozen=True)
class ProductUiRoute:
    path: str
    method: HttpMethod
    name: str


@dataclass(frozen=True)
class ProductUiResult:
    status_code: int
    payload: Mapping[str, Any]
    body: str | None = None
    content_type: str = "application/json"

    def json(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.payload, sort_keys=True))

    def text(self) -> str:
        if self.body is not None:
            return self.body
        return json.dumps(self.payload, sort_keys=True)


class ProductUiApp:
    def __init__(self, services: ProductUiServices) -> None:
        self._services = services
        self.routes = (
            ProductUiRoute(f"{PRODUCT_UI_PREFIX}/health", "GET", "health"),
            ProductUiRoute(f"{PRODUCT_UI_PREFIX}/shell", "GET", "shell"),
            ProductUiRoute(f"{PRODUCT_UI_PREFIX}/login", "GET", "login_view"),
            ProductUiRoute(f"{PRODUCT_UI_PREFIX}/login", "POST", "login"),
            ProductUiRoute(f"{PRODUCT_UI_PREFIX}/dashboard", "GET", "dashboard"),
            ProductUiRoute(f"{PRODUCT_UI_PREFIX}/artifacts", "GET", "artifact_browser"),
            ProductUiRoute(f"{PRODUCT_UI_PREFIX}/evidence", "GET", "evidence_browser"),
            ProductUiRoute(f"{PRODUCT_UI_PREFIX}/provenance", "GET", "provenance_path"),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json_payload: Mapping[str, Any] | None = None,
        query: Mapping[str, str] | None = None,
        session_id: str | None = None,
    ) -> ProductUiResult:
        try:
            return self._route(
                method=method.upper(),
                path=path,
                json_payload=json_payload,
                query=query,
                session_id=session_id,
            )
        except ProductUiError as exc:
            return ProductUiResult(
                status_code=exc.status_code,
                payload=exc.as_response().model_dump(mode="json"),
            )
        except ValidationError as exc:
            return ProductUiResult(
                status_code=422,
                payload=validation_error_response(exc.errors()).model_dump(mode="json"),
            )

    def get(
        self,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        session_id: str | None = None,
    ) -> ProductUiResult:
        return self.request("GET", path, query=query, session_id=session_id)

    def post(self, path: str, *, json_payload: Mapping[str, Any] | None = None) -> ProductUiResult:
        return self.request("POST", path, json_payload=json_payload)

    async def __call__(
        self,
        scope: Mapping[str, Any],
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        if scope.get("type") != "http":
            raise ProductUiError(
                status_code=500,
                code="UNSUPPORTED_SCOPE",
                message="product UI shell only supports HTTP scopes",
            )

        raw_query = scope.get("query_string", b"")
        query_string = raw_query.decode() if isinstance(raw_query, bytes) else str(raw_query)
        query = parse_qs(query_string)
        session_values = query.get("session_id", [])
        session_id = session_values[0] if session_values else None
        try:
            json_payload = await _request_json(receive)
            result = self.request(
                method=str(scope.get("method", "")),
                path=str(scope.get("path", "")),
                json_payload=json_payload,
                query={key: values[0] for key, values in query.items() if values},
                session_id=session_id,
            )
        except ProductUiError as exc:
            result = ProductUiResult(
                status_code=exc.status_code,
                payload=exc.as_response().model_dump(mode="json"),
            )
        body = json.dumps(result.payload, sort_keys=True).encode()
        await send(
            {
                "type": "http.response.start",
                "status": result.status_code,
                "headers": [(b"content-type", result.content_type.encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _route(
        self,
        *,
        method: str,
        path: str,
        json_payload: Mapping[str, Any] | None,
        query: Mapping[str, str] | None,
        session_id: str | None,
    ) -> ProductUiResult:
        if method == "GET" and path == f"{PRODUCT_UI_PREFIX}/health":
            return _json_result(response(self._services.health()).model_dump(mode="json"))
        if method == "GET" and path == f"{PRODUCT_UI_PREFIX}/shell":
            return _json_result(response(self._services.shell()).model_dump(mode="json"))
        if method == "GET" and path == f"{PRODUCT_UI_PREFIX}/login":
            return _json_result(response(self._services.login_view()).model_dump(mode="json"))
        if method == "POST" and path == f"{PRODUCT_UI_PREFIX}/login":
            request = LoginRequest.model_validate(json_payload or {})
            return _json_result(response(self._services.login(request)).model_dump(mode="json"))
        if method == "GET" and path == f"{PRODUCT_UI_PREFIX}/dashboard":
            request = DashboardRequest.model_validate({"session_id": session_id})
            return _json_result(response(self._services.dashboard(request)).model_dump(mode="json"))
        if method == "GET" and path == f"{PRODUCT_UI_PREFIX}/artifacts":
            view = self._services.artifacts(
                query=_query_value(query, "q"),
                selected_artifact_id=_query_value(query, "artifact_id"),
            )
            return _html_result(view.model_dump(mode="json"), render_artifacts(view))
        if method == "GET" and path == f"{PRODUCT_UI_PREFIX}/evidence":
            view = self._services.evidence()
            return _html_result(view.model_dump(mode="json"), render_evidence(view))
        if method == "GET" and path == f"{PRODUCT_UI_PREFIX}/provenance":
            view = self._services.provenance_path(
                artifact_id=_query_value(query, "artifact_id"),
                evidence_id=_query_value(query, "evidence_id"),
            )
            return _html_result(view.model_dump(mode="json"), render_provenance(view))
        raise ProductUiError(
            status_code=404,
            code="ROUTE_NOT_FOUND",
            message="route is not exposed by the product UI boundary",
            details={"method": method},
        )


def create_app(services: ProductUiServices | None = None) -> ProductUiApp:
    return ProductUiApp(services or ProductUiServices.defaults())


def _json_result(payload: Mapping[str, Any]) -> ProductUiResult:
    return ProductUiResult(status_code=200, payload=payload)


def _html_result(payload: Mapping[str, Any], body: str) -> ProductUiResult:
    return ProductUiResult(
        status_code=200,
        payload=payload,
        body=body,
        content_type="text/html; charset=utf-8",
    )


def _query_value(query: Mapping[str, str] | None, key: str) -> str | None:
    if query is None:
        return None
    value = query.get(key)
    return value if value else None


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
        decoded = json.loads(b"".join(body_parts).decode())
    except json.JSONDecodeError as exc:
        raise ProductUiError(
            status_code=400,
            code="INVALID_JSON",
            message="request body must be valid JSON",
        ) from exc
    if not isinstance(decoded, Mapping):
        raise ProductUiError(
            status_code=422,
            code="REQUEST_VALIDATION_FAILED",
            message="request body must be a JSON object",
        )
    return decoded
