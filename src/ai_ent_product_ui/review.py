from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from typing import Any

from ai_ent.external_project import (
    CONTROL_PLANE_AUTHORITY,
    MANDATORY_VERIFICATION_COMMANDS,
    SECRET_FIELD_MARKERS,
)
from ai_ent_product_api.services import ProductApiServices

PRODUCT_UI_SCHEMA_VERSION = "ai-ent-product-ui-v1.0"
PRODUCT_UI_CONTRACT_VERSION = "prd-task-014.1"
PRODUCT_UI_DECISION_DEPENDENCIES = ("DECISION_REQUIRED:PRD-DEC-001",)

REVIEW_PAGE_IDS = (
    "requirements",
    "architecture",
    "capability_matrix",
    "unresolved_decisions",
)

_EXPLICITLY_DENIED_UI_ACTIONS = (
    "approve_gate",
    "approve_decision",
    "bypass_commit_boundary",
    "bypass_verifier",
    "commit",
    "execute_runtime",
    "mark_requirement_accepted",
    "mutate_control_plane_state",
    "push",
    "resolve_decision",
    "schedule_execution",
    "start_guarded_implementation",
    "weaken_policy",
)
_SAFE_REVIEW_ACTIONS = ("navigate_review_pages", "filter_review_rows", "render_review_snapshot")
_SENSITIVE_MARKERS = frozenset(
    marker.lower()
    for marker in (
        *SECRET_FIELD_MARKERS,
        "authorization",
        "cookie",
        "passwd",
        "session",
    )
)

_DECISION_REVIEW_DETAILS: dict[str, dict[str, Any]] = {
    "PRD-DEC-001": {
        "question": "Accept FastAPI/Pydantic plus React/TypeScript/Vite as the product stack?",
        "resolution_timing": "MUST_RESOLVE_BEFORE_PLAN_FREEZE",
        "implementation_can_begin_before_resolution": False,
        "affected_tasks": (
            "PRD-TASK-005",
            "PRD-TASK-007",
            "PRD-TASK-008",
            "PRD-TASK-009",
            "PRD-TASK-010",
            "PRD-TASK-011",
            "PRD-TASK-012",
            "PRD-TASK-013",
            "PRD-TASK-014",
            "PRD-TASK-015",
            "PRD-TASK-016",
            "PRD-TASK-017",
        ),
        "required_gate": "explicit Product Plan Acceptance decision",
    },
    "PRD-DEC-002": {
        "question": "When should non-loopback LAN access be enabled?",
        "resolution_timing": "MUST_RESOLVE_BEFORE_AFFECTED_TASK",
        "implementation_can_begin_before_resolution": True,
        "affected_tasks": ("PRD-TASK-019", "PRD-TASK-023", "PRD-TASK-025"),
        "required_gate": "explicit LAN exposure approval before affected work",
    },
    "PRD-DEC-003": {
        "question": "What secrets backend is required before internet exposure?",
        "resolution_timing": "MUST_RESOLVE_BEFORE_AFFECTED_TASK",
        "implementation_can_begin_before_resolution": True,
        "affected_tasks": ("PRD-TASK-020", "PRD-TASK-023", "PRD-TASK-025"),
        "required_gate": "explicit external-access/secrets decision before affected work",
    },
}

_PAGE_TO_API_DOMAIN = {
    "Login": "auth",
    "Dashboard": "runtime-status",
    "Projects": "projects",
    "New Project": "intake",
    "Project Overview": "projects",
    "Requirements": "requirements",
    "Architecture": "architecture",
    "Capability Matrix": "capabilities",
    "Implementation Plan / DAG": "plans",
    "Tasks": "tasks",
    "Executions": "executions",
    "Human Approvals": "gates",
    "Evidence": "evidence",
    "Artifacts": "artifacts",
    "Generated Application": "generated-applications",
    "System / Runtime Status": "system-health",
}

_PAGE_TO_TASKS = {
    "Login": ("PRD-TASK-012", "PRD-TASK-006"),
    "Dashboard": ("PRD-TASK-012", "PRD-TASK-011"),
    "Projects": ("PRD-TASK-013", "PRD-TASK-007"),
    "New Project": ("PRD-TASK-013", "PRD-TASK-007"),
    "Project Overview": ("PRD-TASK-013", "PRD-TASK-007"),
    "Requirements": ("PRD-TASK-014", "PRD-TASK-007"),
    "Architecture": ("PRD-TASK-014", "PRD-TASK-007"),
    "Capability Matrix": ("PRD-TASK-014", "PRD-TASK-007"),
    "Implementation Plan / DAG": ("PRD-TASK-015", "PRD-TASK-008"),
    "Tasks": ("PRD-TASK-015", "PRD-TASK-008"),
    "Executions": ("PRD-TASK-015", "PRD-TASK-008"),
    "Human Approvals": ("PRD-TASK-016", "PRD-TASK-009"),
    "Evidence": ("PRD-TASK-017", "PRD-TASK-010"),
    "Artifacts": ("PRD-TASK-017", "PRD-TASK-010"),
    "Generated Application": ("PRD-TASK-018",),
    "System / Runtime Status": ("PRD-TASK-011",),
}


@dataclass(frozen=True)
class ReviewActionPolicy:
    control_plane_authority: str
    authority_mode: str
    grants_control_plane_authority: bool
    human_gate_policy: str
    implicit_human_gate_approval: bool
    independent_verification_required: bool
    mandatory_verification_commands: tuple[str, ...]
    decision_dependencies: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    denied_actions: tuple[str, ...]
    secret_values_exposed: bool

    @classmethod
    def from_boundary(cls, boundary: Mapping[str, Any] | None = None) -> ReviewActionPolicy:
        payload = _boundary_payload(boundary)
        boundary_denied = tuple(str(item) for item in payload.get("denied_operations", ()))
        denied = tuple(sorted({*boundary_denied, *_EXPLICITLY_DENIED_UI_ACTIONS}))
        dependencies = tuple(
            sorted(
                {
                    *PRODUCT_UI_DECISION_DEPENDENCIES,
                    *(str(item) for item in payload.get("decision_dependencies", ())),
                }
            )
        )
        return cls(
            control_plane_authority=str(
                payload.get("control_plane_authority", CONTROL_PLANE_AUTHORITY)
            ),
            authority_mode="review_only_facade",
            grants_control_plane_authority=False,
            human_gate_policy="explicit_control_plane_gate_required",
            implicit_human_gate_approval=False,
            independent_verification_required=True,
            mandatory_verification_commands=tuple(
                str(command)
                for command in payload.get(
                    "mandatory_verification_commands",
                    MANDATORY_VERIFICATION_COMMANDS,
                )
            ),
            decision_dependencies=dependencies,
            allowed_actions=_SAFE_REVIEW_ACTIONS,
            denied_actions=denied,
            secret_values_exposed=False,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "control_plane_authority": self.control_plane_authority,
            "authority_mode": self.authority_mode,
            "grants_control_plane_authority": self.grants_control_plane_authority,
            "human_gate_policy": self.human_gate_policy,
            "implicit_human_gate_approval": self.implicit_human_gate_approval,
            "independent_verification_required": self.independent_verification_required,
            "mandatory_verification_commands": list(self.mandatory_verification_commands),
            "decision_dependencies": list(self.decision_dependencies),
            "allowed_actions": list(self.allowed_actions),
            "denied_actions": list(self.denied_actions),
            "secret_values_exposed": self.secret_values_exposed,
        }


@dataclass(frozen=True)
class ReviewSection:
    title: str
    rows: tuple[dict[str, Any], ...]
    empty_state: str = "No review rows available."

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "rows": [_redact(row) for row in self.rows],
            "empty_state": self.empty_state,
        }


@dataclass(frozen=True)
class ReviewPage:
    page_id: str
    route: str
    title: str
    summary: str
    sections: tuple[ReviewSection, ...]
    status: str = "REVIEW_ONLY"
    review_only: bool = True
    human_gate_state: str = "EXPLICIT_CONTROL_PLANE_GATE_REQUIRED"
    independent_verification_required: bool = True
    secret_values_exposed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_id": self.page_id,
            "route": self.route,
            "title": self.title,
            "summary": self.summary,
            "status": self.status,
            "review_only": self.review_only,
            "human_gate_state": self.human_gate_state,
            "independent_verification_required": self.independent_verification_required,
            "secret_values_exposed": self.secret_values_exposed,
            "sections": [section.as_dict() for section in self.sections],
        }


@dataclass(frozen=True)
class ReviewUiBundle:
    pages: tuple[ReviewPage, ...]
    policy: ReviewActionPolicy
    schema_version: str = PRODUCT_UI_SCHEMA_VERSION
    contract_version: str = PRODUCT_UI_CONTRACT_VERSION

    @property
    def navigation(self) -> tuple[dict[str, str], ...]:
        return tuple(
            {"page_id": page.page_id, "route": page.route, "title": page.title}
            for page in self.pages
        )

    def page(self, page_id: str) -> ReviewPage:
        for page in self.pages:
            if page.page_id == page_id:
                return page
        raise KeyError(f"review page is not exposed: {page_id}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_version": self.contract_version,
            "navigation": list(self.navigation),
            "policy": self.policy.as_dict(),
            "pages": [page.as_dict() for page in self.pages],
        }


def build_review_ui(
    plan: Mapping[str, Any],
    *,
    boundary: Mapping[str, Any] | None = None,
) -> ReviewUiBundle:
    safe_plan = _redact(plan)
    policy = ReviewActionPolicy.from_boundary(boundary)
    pages = (
        _requirements_page(safe_plan),
        _architecture_page(safe_plan),
        _capability_matrix_page(safe_plan),
        _unresolved_decisions_page(safe_plan),
    )
    return ReviewUiBundle(pages=pages, policy=policy)


def render_review_ui_html(bundle: ReviewUiBundle) -> str:
    page_html = "\n".join(
        render_review_page_html(page, policy=bundle.policy) for page in bundle.pages
    )
    navigation = "\n".join(
        f'<a href="{escape(item["route"])}">{escape(item["title"])}</a>'
        for item in bundle.navigation
    )
    return (
        '<main class="ai-ent-review-ui" data-review-only="true">'
        f'<nav aria-label="Review pages">{navigation}</nav>'
        f"{page_html}"
        "</main>"
    )


def render_review_page_html(page: ReviewPage, *, policy: ReviewActionPolicy) -> str:
    sections = "\n".join(_render_section(section) for section in page.sections)
    commands = "".join(
        f"<li><code>{escape(command)}</code></li>"
        for command in policy.mandatory_verification_commands
    )
    decisions = ", ".join(escape(item) for item in policy.decision_dependencies)
    return (
        f'<section id="{escape(page.page_id)}" data-route="{escape(page.route)}" '
        'data-review-only="true">'
        f"<h1>{escape(page.title)}</h1>"
        f"<p>{escape(page.summary)}</p>"
        '<aside class="authority-banner">'
        f"<p>{escape(policy.control_plane_authority)}</p>"
        f"<p>{escape(policy.human_gate_policy)}</p>"
        f"<p>{escape(decisions)}</p>"
        f"<ul>{commands}</ul>"
        "</aside>"
        f"{sections}"
        "</section>"
    )


def _requirements_page(plan: Mapping[str, Any]) -> ReviewPage:
    requirements = _mapping_sequence(plan.get("promoted_requirements"))
    rows = tuple(
        _with_redactions(
            {
                "requirement_id": str(item.get("id", "")),
                "source": str(item.get("source", "")),
                "classification": str(item.get("classification", "")),
                "requirement": str(item.get("requirement", "")),
                "coverage_state": "VISIBLE_FOR_OPERATOR_REVIEW",
                "authority": "NON_AUTHORIZING_REVIEW",
            },
            item,
        )
        for item in sorted(requirements, key=lambda row: str(row.get("id", "")))
    )
    return ReviewPage(
        page_id="requirements",
        route="/review/requirements",
        title="Requirements Review",
        summary="Review interpreted and promoted requirements without accepting or mutating them.",
        sections=(ReviewSection("Promoted Requirements", rows),),
    )


def _architecture_page(plan: Mapping[str, Any]) -> ReviewPage:
    architecture = _mapping(plan.get("architecture"))
    authority = str(architecture.get("authority_rule", ""))
    layer_rows = tuple(
        {"position": index, "layer": str(layer), "coverage_state": "VISIBLE_FOR_REVIEW"}
        for index, layer in enumerate(_sequence(architecture.get("layers")), start=1)
    )
    service_rows = tuple(
        _with_redactions(
            {
                "boundary_id": str(item.get("id", "")),
                "responsibility": str(item.get("responsibility", "")),
                "coverage_state": "VISIBLE_FOR_REVIEW",
            },
            item,
        )
        for item in _mapping_sequence(architecture.get("service_boundaries"))
    )
    threat_rows = tuple(
        {
            "boundary": str(item.get("boundary", "")),
            "controls": [str(control) for control in _sequence(item.get("controls"))],
            "coverage_state": "VISIBLE_FOR_REVIEW",
        }
        for item in _mapping_sequence(plan.get("threat_boundaries"))
    )
    return ReviewPage(
        page_id="architecture",
        route="/review/architecture",
        title="Architecture Review",
        summary=(
            "Inspect architecture layers, service boundaries, and threats as read-only evidence."
        ),
        sections=(
            ReviewSection(
                "Authority Rule",
                ({"rule": authority, "control_plane_preserved": "cannot bypass" in authority},),
            ),
            ReviewSection("Layers", layer_rows),
            ReviewSection("Service Boundaries", service_rows),
            ReviewSection("Threat Boundaries", threat_rows),
        ),
    )


def _capability_matrix_page(plan: Mapping[str, Any]) -> ReviewPage:
    domains = {str(item.get("name", "")) for item in _mapping_sequence(plan.get("api_domains"))}
    product_pages = _mapping_sequence(plan.get("product_pages"))
    review_rows = (
        _review_surface_row("requirements_review", "Requirements", "requirements"),
        _review_surface_row("architecture_review", "Architecture", "architecture"),
        _review_surface_row("capability_matrix_review", "Capability Matrix", "capabilities"),
        _review_surface_row("unresolved_decisions_review", "Unresolved Decisions", "governance"),
    )
    page_rows = tuple(
        {
            "ui_page": str(page.get("page", "")),
            "purpose": str(page.get("purpose", "")),
            "api_domain": _PAGE_TO_API_DOMAIN.get(str(page.get("page", "")), ""),
            "api_domain_present": _PAGE_TO_API_DOMAIN.get(str(page.get("page", "")), "") in domains,
            "implementation_tasks": list(_PAGE_TO_TASKS.get(str(page.get("page", "")), ())),
            "coverage_state": _page_coverage_state(str(page.get("page", ""))),
            "authority": "NON_AUTHORIZING_REVIEW",
        }
        for page in product_pages
    )
    return ReviewPage(
        page_id="capability_matrix",
        route="/review/capabilities",
        title="Capability Matrix",
        summary=(
            "Map review surfaces to API domains and implementation tasks without starting work."
        ),
        sections=(
            ReviewSection("Current Review Surfaces", review_rows),
            ReviewSection("Product Page Coverage", page_rows),
        ),
    )


def _unresolved_decisions_page(plan: Mapping[str, Any]) -> ReviewPage:
    decisions = _mapping_sequence(plan.get("unresolved_human_decisions"))
    rows = tuple(
        _decision_row(item)
        for item in sorted(decisions, key=lambda decision: str(decision.get("id", "")))
    )
    return ReviewPage(
        page_id="unresolved_decisions",
        route="/review/decisions",
        title="Unresolved Decisions",
        summary=(
            "Show unresolved decisions that require explicit human resolution outside review pages."
        ),
        sections=(ReviewSection("Decision Dependencies", rows),),
    )


def _review_surface_row(capability_id: str, page: str, api_domain: str) -> dict[str, Any]:
    return {
        "capability_id": capability_id,
        "review_page": page,
        "api_dependency": api_domain,
        "implementation_tasks": ["PRD-TASK-014"],
        "coverage_state": "REVIEW_PAGE_AVAILABLE",
        "human_gate_state": "EXPLICIT_CONTROL_PLANE_GATE_REQUIRED",
        "authority": "NON_AUTHORIZING_REVIEW",
    }


def _decision_row(decision: Mapping[str, Any]) -> dict[str, Any]:
    decision_id = str(decision.get("id", ""))
    details = _DECISION_REVIEW_DETAILS.get(decision_id, {})
    dependency = f"DECISION_REQUIRED:{decision_id}"
    return {
        "decision_id": decision_id,
        "decision": str(decision.get("decision", "")),
        "default": str(decision.get("default", "")),
        "question": str(details.get("question", decision.get("decision", ""))),
        "resolution_timing": str(details.get("resolution_timing", "EXPLICIT_REVIEW_REQUIRED")),
        "implementation_can_begin_before_resolution": bool(
            details.get("implementation_can_begin_before_resolution", False)
        ),
        "affected_tasks": list(_sequence(details.get("affected_tasks", ()))),
        "required_gate": str(details.get("required_gate", "explicit human decision required")),
        "dependency": dependency,
        "human_decision_required": True,
        "review_page_can_resolve": False,
    }


def _page_coverage_state(page_name: str) -> str:
    if page_name in {"Requirements", "Architecture", "Capability Matrix"}:
        return "AVAILABLE_IN_PRD_TASK_014"
    return "PLANNED_PRODUCT_PAGE"


def _with_redactions(row: dict[str, Any], source: Mapping[str, Any]) -> dict[str, Any]:
    redactions = {
        str(key): "<redacted>"
        for key in sorted(source, key=str)
        if _is_sensitive_key(str(key))
    }
    if not redactions:
        return row
    return {**row, "redactions": redactions}


def _render_section(section: ReviewSection) -> str:
    if not section.rows:
        rows = f"<p>{escape(section.empty_state)}</p>"
    else:
        rows = "".join(_render_row(row) for row in section.rows)
    return f"<section><h2>{escape(section.title)}</h2>{rows}</section>"


def _render_row(row: Mapping[str, Any]) -> str:
    items = "".join(
        f"<dt>{escape(str(key))}</dt><dd>{escape(_display_value(value))}</dd>"
        for key, value in _redact(row).items()
    )
    return f"<dl>{items}</dl>"


def _display_value(value: Any) -> str:
    if isinstance(value, list | tuple):
        return ", ".join(_display_value(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(f"{key}: {_display_value(item)}" for key, item in value.items())
    return str(value)


def _boundary_payload(boundary: Mapping[str, Any] | None) -> dict[str, Any]:
    if boundary is not None:
        return dict(_redact(boundary))
    snapshot = ProductApiServices.defaults().boundary()
    return dict(_redact(snapshot.model_dump(mode="python")))


def _redact(value: Any, key_path: tuple[str, ...] = ()) -> Any:
    if key_path and _is_sensitive_key(key_path[-1]):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(key): _redact(item, (*key_path, str(key)))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list | tuple):
        return [_redact(item, key_path) for item in value]
    if isinstance(value, set):
        return sorted(_redact(item, key_path) for item in value)
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.replace("-", "_").replace(".", "_").lower()
    return any(marker in normalized for marker in _SENSITIVE_MARKERS)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, str | bytes) else ()


def _mapping_sequence(value: Any) -> tuple[Mapping[str, Any], ...]:
    return tuple(item for item in _sequence(value) if isinstance(item, Mapping))
