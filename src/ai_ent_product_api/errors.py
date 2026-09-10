from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ai_ent.external_project import SECRET_FIELD_MARKERS
from ai_ent_product_api.schemas import ErrorPayload, ErrorResponse

REDACTED = "<redacted>"
_CONTEXT_VALUE_KEYS = frozenset({"ctx", "input", "value"})
_SAFE_VALIDATION_ERROR_KEYS = frozenset({"code", "ctx", "input", "loc", "msg", "type", "url"})
_PRODUCT_SECRET_MARKERS = frozenset(
    {
        "access_token",
        "api_key",
        "api_token",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "passwd",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "session",
        "token",
        *SECRET_FIELD_MARKERS,
    }
)


@dataclass(frozen=True)
class ProductApiError(Exception):
    status_code: int
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_response(self) -> ErrorResponse:
        return ErrorResponse(
            error=ErrorPayload(
                code=self.code,
                message=self.message,
                details=redact_secret_details(dict(self.details)),
            )
        )


def validation_error_response(details: object) -> ErrorResponse:
    return ErrorResponse(
        error=ErrorPayload(
            code="REQUEST_VALIDATION_FAILED",
            message="request body does not match the product API schema",
            details={"errors": sanitize_validation_errors(details)},
        )
    )


def sanitize_validation_errors(details: object) -> Any:
    if isinstance(details, list):
        return [_sanitize_validation_error(item) for item in details]
    return redact_secret_details(details)


def redact_secret_details(value: object) -> Any:
    return _redact_value(value, path=(), sensitive_context=False)


def is_secret_field_name(value: object) -> bool:
    normalized = _normalize_field_name(value)
    return any(marker in normalized for marker in _PRODUCT_SECRET_MARKERS)


def _sanitize_validation_error(error: object) -> Any:
    if not isinstance(error, Mapping):
        return redact_secret_details(error)

    loc = _loc_path(error.get("loc"))
    sensitive_loc = any(is_secret_field_name(segment) for segment in loc)
    sanitized: dict[str, Any] = {}
    for key in sorted(error):
        item_key = str(key)
        if item_key not in _SAFE_VALIDATION_ERROR_KEYS:
            continue
        raw_item = error[key]
        if item_key in _CONTEXT_VALUE_KEYS:
            sanitized[item_key] = _redact_value(raw_item, path=loc, sensitive_context=sensitive_loc)
        elif item_key == "loc":
            sanitized[item_key] = list(loc)
        else:
            sanitized[item_key] = _redact_value(
                raw_item,
                path=loc + (item_key,),
                sensitive_context=False,
            )
    return sanitized


def _redact_value(value: object, *, path: tuple[str, ...], sensitive_context: bool) -> Any:
    if sensitive_context:
        return REDACTED
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key in sorted(value, key=str):
            item_key = str(key)
            item_path = path + _split_path_segment(item_key)
            redacted[item_key] = _redact_value(
                value[key],
                path=item_path,
                sensitive_context=any(is_secret_field_name(segment) for segment in item_path),
            )
        return redacted
    if isinstance(value, list):
        return [_redact_value(item, path=path, sensitive_context=False) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item, path=path, sensitive_context=False) for item in value]
    return value


def _loc_path(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return _split_path_segment(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        segments: list[str] = []
        for item in value:
            segments.extend(_split_path_segment(str(item)))
        return tuple(segments)
    return ()


def _split_path_segment(value: str) -> tuple[str, ...]:
    normalized = value.replace("[", ".").replace("]", "")
    return tuple(segment for segment in normalized.split(".") if segment)


def _normalize_field_name(value: object) -> str:
    return str(value).strip().lower().replace("-", "_")
