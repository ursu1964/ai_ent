from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ai_ent_product_api import ProductApiApp
from ai_ent_product_api import create_app as create_api_app
from ai_ent_product_deployment.config import LocalDeploymentConfig
from ai_ent_product_ui.app import ProductUiApp
from ai_ent_product_ui.app import create_app as create_ui_app
from ai_ent_product_ui.services import (
    OperatorAccount,
    OperatorDirectory,
    OperatorSessionStore,
    ProductUiServices,
)


class LocalProductServer:
    def __init__(self, config: LocalDeploymentConfig) -> None:
        self.config = config
        self.api = create_api_app()
        self.ui = create_ui_app(_ui_services(config))
        self._server = ThreadingHTTPServer(
            (config.bind_host, config.port),
            _handler_factory(self.api, self.ui, config),
        )

    @property
    def server_address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _ui_services(config: LocalDeploymentConfig) -> ProductUiServices:
    services = ProductUiServices.defaults()
    services.operator_directory = OperatorDirectory.with_accounts(
        (
            OperatorAccount(
                username=config.operator.username,
                display_name=config.operator.display_name,
                password_hash=config.operator.password_hash,
                roles=config.operator.roles,
            ),
        )
    )
    services.session_store = OperatorSessionStore.empty()
    return services


def _handler_factory(
    api: ProductApiApp,
    ui: ProductUiApp,
    config: LocalDeploymentConfig,
) -> type[BaseHTTPRequestHandler]:
    class LocalProductHandler(BaseHTTPRequestHandler):
        server_version = "AIEntLocal/1"

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_OPTIONS(self) -> None:
            origin = self.headers.get("origin")
            if not self._origin_allowed(origin):
                self._send(403, json.dumps({"error": "origin not allowed"}), "application/json")
                return
            self._send(204, "", "text/plain", origin=origin)

        def log_message(self, fmt: str, *args: Any) -> None:
            safe_path = self.path.split("?", 1)[0]
            print(f"access {self.address_string()} {self.command} {safe_path} {args[1] if len(args) > 1 else '-'}")

        def _dispatch(self, method: str) -> None:
            origin = self.headers.get("origin")
            if not self._origin_allowed(origin):
                self._send(403, json.dumps({"error": "origin not allowed"}), "application/json")
                return
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                result = api.request(method, parsed.path, json_payload=self._json_body())
                self._send(
                    result.status_code,
                    json.dumps(result.json(), sort_keys=True),
                    "application/json",
                    origin=origin,
                )
                return
            if parsed.path.startswith("/ui/"):
                query = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
                result = ui.request(
                    method,
                    parsed.path,
                    json_payload=self._json_body(),
                    query=query,
                    session_id=query.get("session_id"),
                )
                self._send(result.status_code, result.text(), result.content_type, origin=origin)
                return
            self._send(
                404,
                json.dumps({"error": "route not exposed"}, sort_keys=True),
                "application/json",
                origin=origin,
            )

        def _json_body(self) -> dict[str, Any] | None:
            length = int(self.headers.get("content-length", "0") or "0")
            if not length:
                return None
            try:
                decoded = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                return None
            return decoded if isinstance(decoded, dict) else None

        def _origin_allowed(self, origin: str | None) -> bool:
            if origin is None:
                return True
            if not config.allowed_origins:
                return False
            return origin in set(config.allowed_origins)

        def _send(
            self,
            status: int,
            body: str,
            content_type: str,
            *,
            origin: str | None = None,
        ) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(payload)))
            if origin is not None and origin in set(config.allowed_origins):
                self.send_header("access-control-allow-origin", origin)
                self.send_header("access-control-allow-credentials", "true")
                self.send_header("vary", "origin")
                self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
                self.send_header("access-control-allow-headers", "content-type")
            self.end_headers()
            if payload:
                self.wfile.write(payload)

    return LocalProductHandler
