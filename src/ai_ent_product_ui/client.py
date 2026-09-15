from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ai_ent_product_api import PRODUCT_API_PREFIX, create_app
from ai_ent_product_api.app import ProductApiApp, ProductApiResult
from ai_ent_product_ui.models import (
    BoundaryView,
    ProjectIntakeDraft,
    ProjectListItem,
    ValidationView,
)


class ProductUiApiError(RuntimeError):
    pass


class ProductIntakeClient:
    def __init__(self, api: ProductApiApp | None = None) -> None:
        self._api = api or create_app()

    def boundary(self) -> BoundaryView:
        payload = self._response_data(
            self._api.get(f"{PRODUCT_API_PREFIX}/boundary"),
            operation="load boundary",
        )
        return BoundaryView.from_payload(payload)

    def validate_project_intake(self, draft: ProjectIntakeDraft) -> ValidationView:
        payload = self._response_data(
            self._api.post(
                f"{PRODUCT_API_PREFIX}/governance/requests/validate",
                json_payload=draft.governance_payload(),
            ),
            operation="validate project intake",
        )
        return ValidationView.from_payload(payload)

    def create_project_intake(self, draft: ProjectIntakeDraft) -> ProjectListItem:
        validation = self.validate_project_intake(draft)
        return ProjectListItem.from_draft(draft, validation=validation)

    def _response_data(self, result: ProductApiResult, *, operation: str) -> Mapping[str, Any]:
        payload = result.json()
        if result.status_code != 200:
            message = payload.get("error", {}).get("message", "product API request failed")
            raise ProductUiApiError(f"could not {operation}: {message}")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ProductUiApiError(f"could not {operation}: response data is missing")
        return data
