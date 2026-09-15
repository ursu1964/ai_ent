from __future__ import annotations

import json
from pathlib import Path

from ai_ent.external_project import CONTROL_PLANE_AUTHORITY, MANDATORY_VERIFICATION_COMMANDS
from ai_ent.productization_plan import write_productization_plan
from ai_ent_product_ui import build_review_ui, render_review_page_html, render_review_ui_html
from tests.test_productization_plan import write_accepted_evidence


def _productization_plan(tmp_path: Path) -> dict[str, object]:
    artifacts = tmp_path / "artifacts"
    output = tmp_path / "compiled"
    write_accepted_evidence(artifacts)
    write_productization_plan(artifacts_dir=artifacts, output_dir=output)
    return json.loads((output / "productization-plan.json").read_text(encoding="utf-8"))


def test_review_ui_exposes_only_the_four_prd_task_014_review_pages(tmp_path: Path) -> None:
    bundle = build_review_ui(_productization_plan(tmp_path))

    assert [page.page_id for page in bundle.pages] == [
        "requirements",
        "architecture",
        "capability_matrix",
        "unresolved_decisions",
    ]
    assert [item["route"] for item in bundle.navigation] == [
        "/review/requirements",
        "/review/architecture",
        "/review/capabilities",
        "/review/decisions",
    ]
    assert all(page.status == "REVIEW_ONLY" for page in bundle.pages)
    assert all(page.review_only is True for page in bundle.pages)
    assert all(page.secret_values_exposed is False for page in bundle.pages)


def test_review_ui_preserves_control_plane_authority_and_mandatory_verification(
    tmp_path: Path,
) -> None:
    bundle = build_review_ui(_productization_plan(tmp_path))
    policy = bundle.policy

    assert policy.control_plane_authority == CONTROL_PLANE_AUTHORITY
    assert policy.authority_mode == "review_only_facade"
    assert policy.grants_control_plane_authority is False
    assert policy.human_gate_policy == "explicit_control_plane_gate_required"
    assert policy.implicit_human_gate_approval is False
    assert policy.independent_verification_required is True
    assert policy.mandatory_verification_commands == MANDATORY_VERIFICATION_COMMANDS
    assert "DECISION_REQUIRED:PRD-DEC-001" in policy.decision_dependencies
    assert "approve_gate" in policy.denied_actions
    assert "schedule_execution" in policy.denied_actions
    assert "bypass_verifier" in policy.denied_actions
    assert "approve_gate" not in policy.allowed_actions


def test_requirements_review_projects_promoted_requirements_without_secret_leaks() -> None:
    bundle = build_review_ui(_sample_plan_with_secret_values())
    page = bundle.page("requirements")
    payload = page.as_dict()

    rows = payload["sections"][0]["rows"]
    assert [row["requirement_id"] for row in rows] == ["PRD-FH-001", "PRD-FH-002"]
    assert rows[0]["coverage_state"] == "VISIBLE_FOR_OPERATOR_REVIEW"
    assert rows[0]["authority"] == "NON_AUTHORIZING_REVIEW"
    assert "token-value" not in str(bundle.as_dict())
    assert "password-value" not in str(bundle.as_dict())
    assert "<redacted>" in str(bundle.as_dict())


def test_capability_matrix_maps_review_surfaces_to_prd_task_014(tmp_path: Path) -> None:
    bundle = build_review_ui(_productization_plan(tmp_path))
    page = bundle.page("capability_matrix")
    current = page.as_dict()["sections"][0]["rows"]
    by_capability = {row["capability_id"]: row for row in current}

    assert by_capability["requirements_review"]["implementation_tasks"] == ["PRD-TASK-014"]
    assert by_capability["architecture_review"]["api_dependency"] == "architecture"
    assert by_capability["capability_matrix_review"]["coverage_state"] == "REVIEW_PAGE_AVAILABLE"
    assert by_capability["unresolved_decisions_review"]["review_page"] == "Unresolved Decisions"
    assert all(row["authority"] == "NON_AUTHORIZING_REVIEW" for row in current)


def test_unresolved_decisions_remain_explicit_and_unapproved(tmp_path: Path) -> None:
    bundle = build_review_ui(_productization_plan(tmp_path))
    page = bundle.page("unresolved_decisions")
    decisions = {row["decision_id"]: row for row in page.as_dict()["sections"][0]["rows"]}

    assert set(decisions) == {"PRD-DEC-001", "PRD-DEC-002", "PRD-DEC-003"}
    assert decisions["PRD-DEC-001"]["dependency"] == "DECISION_REQUIRED:PRD-DEC-001"
    assert decisions["PRD-DEC-001"]["resolution_timing"] == "MUST_RESOLVE_BEFORE_PLAN_FREEZE"
    assert decisions["PRD-DEC-001"]["human_decision_required"] is True
    assert decisions["PRD-DEC-001"]["review_page_can_resolve"] is False
    assert "PRD-TASK-014" in decisions["PRD-DEC-001"]["affected_tasks"]


def test_review_html_is_deterministic_review_only_and_escapes_values(tmp_path: Path) -> None:
    bundle = build_review_ui(_productization_plan(tmp_path))
    first = render_review_ui_html(bundle)
    second = render_review_ui_html(bundle)
    decision_page = render_review_page_html(
        bundle.page("unresolved_decisions"),
        policy=bundle.policy,
    )

    assert first == second
    assert 'data-review-only="true"' in first
    assert "<button" not in first
    assert "<form" not in first
    assert "<input" not in first
    assert MANDATORY_VERIFICATION_COMMANDS[0] in first
    assert "DECISION_REQUIRED:PRD-DEC-001" in decision_page


def _sample_plan_with_secret_values() -> dict[str, object]:
    return {
        "promoted_requirements": [
            {
                "id": "PRD-FH-002",
                "source": "RCG-001/PIR-002",
                "requirement": "Create runtime import receipt artifacts.",
                "classification": "PRODUCTIZATION_REQUIRED",
                "api_token": "token-value",
            },
            {
                "id": "PRD-FH-001",
                "source": "E2E-001",
                "requirement": "Promote the target-app E2E harness.",
                "classification": "PRODUCTIZATION_REQUIRED",
            },
        ],
        "architecture": {
            "layers": ["Browser", "Operator Web UI"],
            "authority_rule": "UI and API cannot bypass gates or verification.",
            "service_boundaries": [
                {"id": "PRODUCT-API", "responsibility": "Facade", "password": "password-value"}
            ],
        },
        "api_domains": [
            {"name": "requirements"},
            {"name": "architecture"},
            {"name": "capabilities"},
        ],
        "product_pages": [
            {"page": "Requirements", "purpose": "Review interpreted requirements."},
            {"page": "Architecture", "purpose": "Inspect architecture."},
            {"page": "Capability Matrix", "purpose": "Show capability coverage."},
        ],
        "threat_boundaries": [],
        "unresolved_human_decisions": [
            {
                "id": "PRD-DEC-001",
                "decision": "Choose FastAPI/React product stack.",
                "default": "accept unless operator requires a different stack.",
            }
        ],
    }
