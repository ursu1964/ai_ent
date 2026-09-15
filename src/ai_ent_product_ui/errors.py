from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ai_ent_product_ui.schemas import PRODUCT_UI_SCHEMA_VERSION, PRODUCT_UI_VERSION

SENSITIVE_FIELD_MARKERS = (
    "authorization",
    "cookie",
    "credential",
    "key",
    "password",
    "private",
    "secret",
    "session",
    "token",
)


class ErrorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProductUiErrorPayload(ErrorModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ProductUiErrorResponse(ErrorModel):
    ui_version: str = PRODUCT_UI_VERSION
    schema_version: str = PRODUCT_UI_SCHEMA_VERSION
    error: ProductUiErrorPayload


class ProductUiError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}

    def as_response(self) -> ProductUiErrorResponse:
        return ProductUiErrorResponse(
            error=ProductUiErrorPayload(
                code=self.code,
                message=self.message,
                details=_redact(self.details),
            )
        )


def validation_error_response(errors: object) -> ProductUiErrorResponse:
    safe_errors = []
    error_items = errors if isinstance(errors, list) else [errors]
    for error in error_items:
        if not isinstance(error, Mapping):
            safe_errors.append(_redact(error))
            continue
        item = dict(error)
        if "ctx" in item:
            item["ctx"] = _redact(item["ctx"])
        if "input" in item:
            item["input"] = _redact_for_location(item.get("loc", ()), item["input"])
        safe_errors.append(item)
    return ProductUiErrorResponse(
        error=ProductUiErrorPayload(
            code="REQUEST_VALIDATION_FAILED",
            message="request did not match the product UI schema",
            details={"errors": safe_errors},
        )
    )


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if _sensitive(str(key)) else _redact(inner)
            for key, inner in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    return value


def _redact_for_location(location: object, value: Any) -> Any:
    if isinstance(location, (tuple, list)):
        rendered = ".".join(str(part) for part in location)
    else:
        rendered = str(location)
    if _sensitive(rendered):
        return "<redacted>"
    return _redact(value)


def _sensitive(name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    return any(marker in normalized for marker in SENSITIVE_FIELD_MARKERS)
