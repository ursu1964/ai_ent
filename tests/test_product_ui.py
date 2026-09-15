from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from ai_ent.external_project import CONTROL_PLANE_AUTHORITY, MANDATORY_VERIFICATION_COMMANDS
from ai_ent.persistence.models import Checkpoint, Execution
from ai_ent.product_plan_final_acceptance import PRODUCT_PLAN_ID
from ai_ent_product_api.services import (
    GovernanceValidationService,
    ProductApiServices,
    ProductBoundaryService,
    ProductRuntimeSnapshotService,
)
from ai_ent_product_ui import (
    PRODUCT_UI_PREFIX,
    ProductExecutionMonitor,
    ProductIntakeClient,
    ProjectIntakeDraft,
    ProjectListItem,
    ProjectUiController,
    ValidationView,
    create_app,
    render_new_project,
    render_project_list,
    render_project_overview,
)
from ai_ent_product_ui.schemas import ArtifactRecord, EvidenceRecord, ProvenanceEdge, TaskDagNode
from ai_ent_product_ui.services import (
    DashboardProjection,
    OperatorAccount,
    OperatorDirectory,
    OperatorSessionStore,
    ProductEvidenceBrowserService,
    ProductUiServices,
    StaticEvidenceArtifactSource,
    hash_password,
)
from tests import test_product_api as product_api_tests
from tests import test_product_runtime_handoff as runtime_handoff_tests


def services() -> ProductUiServices:
    return ProductUiServices(
        api_services=ProductUiServices.defaults().api_services,
        operator_directory=OperatorDirectory.with_accounts(
            (
                OperatorAccount(
                    username="operator",
                    display_name="Operator",
                    password_hash=hash_password("operator-password"),
                    roles=("authenticated_user", "project_operator"),
                ),
            )
        ),
        session_store=OperatorSessionStore.empty(),
        dashboard_projection=DashboardProjection(
            project_count=2,
            pending_gate_count=1,
            failed_execution_count=0,
            system_health_issue_count=0,
        ),
    )


def test_ui_shell_exposes_only_non_authorizing_operator_routes() -> None:
    app = create_app(services())
    route_pairs = {(route.method, route.path) for route in app.routes}

    assert {
        ("GET", f"{PRODUCT_UI_PREFIX}/dashboard"),
        ("GET", f"{PRODUCT_UI_PREFIX}/health"),
        ("GET", f"{PRODUCT_UI_PREFIX}/login"),
        ("POST", f"{PRODUCT_UI_PREFIX}/login"),
        ("GET", f"{PRODUCT_UI_PREFIX}/shell"),
    }.issubset(route_pairs)
    assert {
        ("GET", f"{PRODUCT_UI_PREFIX}/artifacts"),
        ("GET", f"{PRODUCT_UI_PREFIX}/evidence"),
        ("GET", f"{PRODUCT_UI_PREFIX}/provenance"),
    }.issubset(route_pairs)
    assert all(path.startswith(PRODUCT_UI_PREFIX) for _, path in route_pairs)
    assert not any(
        authority in path
        for _, path in route_pairs
        for authority in ("approve", "commit", "execute", "push", "schedule")
    )


def test_shell_preserves_ui_facade_boundary_and_explicit_human_gates() -> None:
    result = create_app(services()).get(f"{PRODUCT_UI_PREFIX}/shell")
    payload = result.json()

    assert result.status_code == 200
    assert payload["ui_version"] == "v1"
    assert payload["schema_version"] == "ai-ent-product-ui-v1.0"
    assert payload["data"]["authority_mode"] == "facade_only"
    assert payload["data"]["control_plane_access"] == "product_api_only"
    assert payload["data"]["secret_values_exposed"] is False
    assert "human gate approval" in payload["data"]["forbidden_implicit_actions"]
    assert "verification approval" in payload["data"]["forbidden_implicit_actions"]
    assert payload["data"]["decision_dependencies"] == ["DECISION_REQUIRED:PRD-DEC-001"]


def test_login_authenticates_operator_without_echoing_secret_values() -> None:
    result = create_app(services()).post(
        f"{PRODUCT_UI_PREFIX}/login",
        json_payload={"username": "operator", "password": "operator-password"},
    )
    payload = result.json()

    assert result.status_code == 200
    assert payload["data"]["operator"] == {
        "display_name": "Operator",
        "roles": ["authenticated_user", "project_operator"],
        "username": "operator",
    }
    assert payload["data"]["session_owner"] == "server"
    assert payload["data"]["browser_secret_material_exposed"] is False
    assert payload["data"]["grants_control_plane_authority"] is False
    assert payload["data"]["gate_approval_authority"] is False
    assert "operator-password" not in str(payload)


def test_login_failure_and_validation_errors_redact_secret_material() -> None:
    app = create_app(services())
    failed = app.post(
        f"{PRODUCT_UI_PREFIX}/login",
        json_payload={"username": "operator", "password": "wrong-password"},
    )
    invalid = app.post(
        f"{PRODUCT_UI_PREFIX}/login",
        json_payload={"username": "operator", "password": "", "client_secret": "value"},
    )

    assert failed.status_code == 401
    assert "wrong-password" not in str(failed.json())
    assert invalid.status_code == 422
    assert "value" not in str(invalid.json())
    assert "<redacted>" in str(invalid.json())


def test_dashboard_requires_authenticated_session() -> None:
    result = create_app(services()).get(f"{PRODUCT_UI_PREFIX}/dashboard", session_id="missing")

    assert result.status_code == 401
    assert result.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert "missing" not in str(result.json())


def test_authenticated_dashboard_preserves_control_plane_authority_and_verification() -> None:
    app = create_app(services())
    login = app.post(
        f"{PRODUCT_UI_PREFIX}/login",
        json_payload={"username": "operator", "password": "operator-password"},
    )
    session_id = str(login.json()["data"]["session_id"])
    result = app.get(f"{PRODUCT_UI_PREFIX}/dashboard", session_id=session_id)
    payload = result.json()

    assert result.status_code == 200
    assert payload["data"]["page"] == "Dashboard"
    assert payload["data"]["authority"]["control_plane_authority"] == CONTROL_PLANE_AUTHORITY
    assert payload["data"]["authority"]["authority_mode"] == "facade_only"
    assert payload["data"]["authority"]["grants_control_plane_authority"] is False
    assert payload["data"]["authority"]["gate_approval_authority"] is False
    assert payload["data"]["authority"]["scheduling_authority"] is False
    assert payload["data"]["authority"]["runtime_execution_authority"] is False
    assert payload["data"]["authority"]["policy_weakening_authority"] is False
    assert "approve_gate" in payload["data"]["authority"]["denied_operations"]
    assert "schedule_execution" in payload["data"]["authority"]["denied_operations"]
    assert payload["data"]["gates"] == {
        "approval_requires_explicit_gate_action": True,
        "approved_by_ui_navigation": 0,
        "pending": 1,
    }
    assert payload["data"]["verification"] == {
        "independent_verification_required": True,
        "mandatory_verification_commands": list(MANDATORY_VERIFICATION_COMMANDS),
        "verifier_bypass_authority": False,
    }
    assert payload["data"]["decision_dependencies"] == ["DECISION_REQUIRED:PRD-DEC-001"]
    assert payload["data"]["secret_values_exposed"] is False


def test_project_list_is_sorted_and_keeps_boundary_explicit() -> None:
    controller = ProjectUiController.with_projects(
        (
            ProjectListItem(
                project_id="zeta",
                name="Zeta",
                summary="Second",
                validation=ValidationView.draft(),
            ),
            ProjectListItem(
                project_id="alpha",
                name="Alpha",
                summary="First",
                validation=ValidationView.draft(),
            ),
        )
    )

    view = controller.project_list(selected_project_id="alpha")
    payload = view.as_dict()

    assert [project["project_id"] for project in payload["projects"]] == ["alpha", "zeta"]
    assert payload["selected_project_id"] == "alpha"
    assert payload["boundary"]["control_plane_authority"] == CONTROL_PLANE_AUTHORITY
    assert payload["boundary"]["grants_control_plane_authority"] is False
    assert payload["boundary"]["human_gate_policy"] == "explicit_control_plane_gate_required"
    assert payload["boundary"]["implicit_human_gate_approval"] is False
    assert payload["boundary"]["independent_verification_required"] is True
    assert payload["boundary"]["mandatory_verification_commands"] == list(
        MANDATORY_VERIFICATION_COMMANDS
    )
    assert payload["boundary"]["decision_dependencies"] == ["DECISION_REQUIRED:PRD-DEC-001"]
    assert payload["boundary"]["secret_values_exposed"] is False


def test_new_project_workflow_posts_to_intake_validation_api_without_granting_authority() -> None:
    controller = ProjectUiController()
    draft = ProjectIntakeDraft(
        project_id="customer-portal",
        name="Customer Portal",
        summary="Create a managed customer onboarding surface.",
        actor_id="operator",
        evidence_refs=("PRD-TASK-013",),
        metadata={"api_token": "secret-token", "safe": "visible"},
    )

    new_view = controller.new_project(draft)
    overview = controller.submit_new_project(draft)

    assert new_view.validation.status == "DRAFT"
    assert overview.project.validation.status == "ACCEPTED"
    assert overview.project.validation.accepted_authorities == ("recommend", "record_evidence")
    assert overview.project.validation.blockers == ()
    assert overview.project.validation.gate_approval_granted is False
    assert overview.project.validation.verifier_bypass_granted is False
    assert overview.project.validation.commit_boundary_bypass_granted is False
    assert overview.project.validation.self_scheduling_granted is False
    assert overview.project.validation.runtime_execution_granted is False
    assert overview.project.validation.policy_weakening_granted is False
    assert overview.control_plane_created is False
    assert overview.human_gate_status == "explicit_control_plane_gate_required"
    assert overview.independent_verification_status == "mandatory"
    assert overview.project.metadata["api_token"] == "<redacted>"
    assert overview.project.metadata["safe"] == "visible"
    assert "secret-token" not in str(overview.as_dict())


def test_rejected_project_intake_surfaces_blockers_without_hidden_gate_bypass() -> None:
    client = ProductIntakeClient()
    draft = ProjectIntakeDraft(
        project_id="unsafe-request",
        name="Unsafe Request",
        summary="Try to make the UI schedule execution.",
        actor_id="operator",
        requested_authorities=(
            "recommend",
            "approve_gate",
            "bypass_verifier",
            "schedule_execution",
        ),
    )

    project = client.create_project_intake(draft)

    assert project.validation.status == "REJECTED"
    assert project.validation.accepted_authorities == ()
    assert set(project.validation.blockers) >= {
        "prohibited authority requested:approve_gate",
        "prohibited authority requested:bypass_verifier",
        "prohibited authority requested:schedule_execution",
    }
    assert project.validation.gate_approval_granted is False
    assert project.validation.verifier_bypass_granted is False
    assert project.validation.self_scheduling_granted is False


def test_project_overview_round_trip_uses_submitted_intake_record() -> None:
    controller = ProjectUiController()
    draft = ProjectIntakeDraft(
        project_id="overview",
        name="Overview",
        summary="Show lifecycle and readiness after intake validation.",
        actor_id="operator",
    )

    submitted = controller.submit_new_project(draft)
    reloaded = controller.project_overview("overview")

    assert reloaded.as_dict() == submitted.as_dict()
    assert reloaded.project.validation.required_evolution_sequence == (
        "proposal",
        "impact_analysis",
        "simulation",
        "deterministic_validation",
        "human_approval",
        "generation",
        "deployment",
        "observation",
    )


def test_rendered_project_views_are_deterministic_and_do_not_expose_secrets() -> None:
    controller = ProjectUiController()
    draft = ProjectIntakeDraft(
        project_id="rendered",
        name="Rendered",
        summary="Render UI states.",
        actor_id="operator",
        metadata={"password": "secret-password"},
    )
    list_view = controller.project_list()
    list_html = render_project_list(list_view)
    new_html = render_new_project(controller.new_project(draft))
    overview_html = render_project_overview(controller.submit_new_project(draft))

    assert list_html == render_project_list(list_view)
    assert 'aria-label="Control-plane boundary"' in new_html
    assert "DECISION_REQUIRED:PRD-DEC-001" in overview_html
    assert "explicit_control_plane_gate_required" in overview_html
    assert "Independent verification: mandatory" in overview_html
    assert "Control-plane project created: false" in overview_html
    assert "secret-password" not in overview_html


def test_execution_monitoring_dashboard_projects_dag_details_attempts_repairs_and_commits() -> None:
    monitor = _seeded_monitor(_seed_successful_repair_attempt)

    dashboard = monitor.dashboard()
    detail = monitor.task_detail("PRD-TASK-008")
    assert detail is not None
    node = _node(dashboard.task_dag_nodes, "PRD-TASK-008")

    assert dashboard.product_plan_id == PRODUCT_PLAN_ID
    assert len(dashboard.task_dag_nodes) == 25
    assert len(dashboard.task_dag_edges) == 51
    assert node.human_gate_ids == ("GATE-PRD-RUNTIME-CONTROL",)
    assert node.latest_attempt == 2
    assert node.latest_execution_status == "succeeded"
    assert node.verifier_status == "verification_passed"
    assert "dependency:PRD-TASK-006" in node.blocked_by
    assert detail.acceptance_criteria
    assert detail.allowed_write_scope
    assert ".env" in detail.prohibited_paths
    assert [attempt.attempt for attempt in detail.execution_attempts] == [1, 2]
    assert detail.repair_history[0].category == "VERIFICATION_FAILURE"
    assert detail.repair_history[0].recommended_action == "REPAIR_WITH_NEW_EXECUTION"
    assert detail.repair_history[0].creates_execution is False
    assert detail.repair_history[0].grants_repair_authority is False
    assert {output.status for output in detail.verifier_outputs} == {
        "verification_failed",
        "verification_passed",
    }
    assert {commit.hash for commit in detail.commits} == {
        "d" * 40,
        "e" * 40,
    }


def test_execution_monitoring_ui_preserves_authority_gates_verification_and_redaction() -> None:
    monitor = _seeded_monitor(_seed_successful_repair_attempt)

    payload = monitor.dashboard().model_dump(mode="json")
    authority = payload["authority"]
    gate = _gate(payload["human_gates"], "GATE-PRD-RUNTIME-CONTROL")
    serialized = json.dumps(payload, sort_keys=True)

    assert authority["read_only"] is True
    assert authority["grants_control_plane_authority"] is False
    assert authority["gate_approval_authority"] is False
    assert authority["scheduling_authority"] is False
    assert authority["runtime_execution_authority"] is False
    assert authority["repair_execution_authority"] is False
    assert authority["policy_weakening_authority"] is False
    assert authority["verifier_bypass_authority"] is False
    assert authority["commit_boundary_bypass_authority"] is False
    assert authority["human_gate_policy"] == "explicit_control_plane_gate_required"
    assert authority["implicit_human_gate_approval"] is False
    assert authority["independent_verification_required"] is True
    assert authority["mandatory_verification_commands"] == list(MANDATORY_VERIFICATION_COMMANDS)
    assert authority["decision_dependencies"] == ["DECISION_REQUIRED:PRD-DEC-001"]
    assert "approve_gate" in authority["denied_operations"]
    assert "execute_repair" in authority["denied_operations"]
    assert gate["explicit_control_plane_gate_required"] is True
    assert gate["ui_can_approve"] is False
    assert "token-value" not in serialized
    assert "password-value" not in serialized
    assert "abc123" not in serialized
    assert "<redacted>" in serialized


def test_task_detail_returns_none_for_unknown_product_task() -> None:
    monitor = _seeded_monitor()

    assert monitor.task_detail("PRD-TASK-999") is None


def _seeded_monitor(mutator: Callable[[Session], None] | None = None) -> ProductExecutionMonitor:
    factory = product_api_tests._seeded_product_runtime_session_factory()
    if mutator is not None:
        with factory() as session:
            mutator(session)
            session.commit()
    return ProductExecutionMonitor(
        ProductApiServices(
            boundary_service=ProductBoundaryService(),
            governance_service=GovernanceValidationService(),
            runtime_service=ProductRuntimeSnapshotService(
                artifacts_loader=runtime_handoff_tests.artifacts,
                session_factory=factory,
            ),
        )
    )


def _seed_successful_repair_attempt(session: Session) -> None:
    session.add(
        Execution(
            id="execution-product-ui-2",
            task_id="PRD-TASK-008",
            executor_type="codex",
            status="succeeded",
            attempt=2,
            terminal_state="success",
            error_classification=None,
            candidate_tree_hash="c" * 64,
            commit_hash="d" * 40,
        )
    )
    session.add(
        Checkpoint(
            id="checkpoint-product-ui-2",
            task_id="PRD-TASK-008",
            execution_id="execution-product-ui-2",
            checkpoint_type="execution",
            state=json.dumps(
                {
                    "status": "verification_passed",
                    "ok": True,
                    "findings": ["all required checks passed", "api_token=abc123"],
                    "commands": ["pytest -q"],
                    "password": "password-value",
                },
                sort_keys=True,
            ),
            commit_hash="e" * 40,
            tree_hash="f" * 40,
        )
    )


def _node(items: tuple[TaskDagNode, ...], task_id: str) -> TaskDagNode:
    for item in items:
        if item.task_id == task_id:
            return item
    raise AssertionError(f"missing task node: {task_id}")


def _gate(items: list[dict[str, Any]], gate_id: str) -> dict[str, Any]:
    for item in items:
        if item["gate_id"] == gate_id:
            return item
    raise AssertionError(f"missing gate: {gate_id}")


def test_artifact_browser_preserves_authority_boundaries_and_redacts_secrets() -> None:
    result = create_app(_artifact_services()).get(
        f"{PRODUCT_UI_PREFIX}/artifacts",
        query={"artifact_id": "artifact-safe-1"},
    )

    payload = result.json()

    assert result.status_code == 200
    assert payload["authority"]["control_plane_authority"] == CONTROL_PLANE_AUTHORITY
    assert payload["authority"]["grants_control_plane_authority"] is False
    assert payload["authority"]["gate_approval_authority"] is False
    assert payload["authority"]["verifier_bypass_authority"] is False
    assert payload["authority"]["runtime_execution_authority"] is False
    assert payload["authority"]["artifact_write_authority"] is False
    assert payload["authority"]["independent_verification_required"] is True
    assert payload["authority"]["mandatory_verification_commands"] == list(
        MANDATORY_VERIFICATION_COMMANDS
    )
    assert payload["authority"]["decision_dependencies"] == ["DECISION_REQUIRED:PRD-DEC-001"]
    assert "approve_gate" in payload["authority"]["denied_operations"]
    assert "mutate_artifact" in payload["authority"]["denied_operations"]
    assert payload["authority"]["secret_values_exposed"] is False
    assert payload["evidence_status"] == "NON_AUTHORITATIVE"
    assert payload["selected_artifact"]["evidence_status"] == "NON_AUTHORITATIVE"
    assert "token-value" not in str(payload)
    assert "password-value" not in str(payload)
    assert "<redacted>" in str(payload)


def test_artifact_browser_filters_artifacts_deterministically() -> None:
    payload = create_app(_artifact_services()).get(
        f"{PRODUCT_UI_PREFIX}/artifacts",
        query={"q": "runtime_log"},
    ).json()

    assert payload["total_artifacts"] == 1
    assert [artifact["artifact_id"] for artifact in payload["artifacts"]] == [
        "artifact-secret-2"
    ]
    assert payload["artifacts"][0]["evidence_status"] == "NON_AUTHORITATIVE"
    assert "token-value" not in str(payload)


def test_evidence_browser_keeps_evidence_non_authoritative_and_gates_explicit() -> None:
    payload = create_app(_artifact_services()).get(f"{PRODUCT_UI_PREFIX}/evidence").json()

    evidence = payload["evidence_records"][0]

    assert payload["evidence_status"] == "NON_AUTHORITATIVE"
    assert evidence["evidence_status"] == "NON_AUTHORITATIVE"
    assert evidence["authority_state"] == "NON_AUTHORITATIVE"
    assert evidence["independent_verification_required"] is True
    assert evidence["human_gate_ids"] == ["GATE-PRD-HITL-UI"]
    assert payload["authority"]["implicit_human_gate_approval"] is False
    assert payload["authority"]["human_gate_policy"] == "explicit_control_plane_gate_required"


def test_provenance_path_is_deterministic_and_non_authoritative() -> None:
    result = create_app(_artifact_services()).get(
        f"{PRODUCT_UI_PREFIX}/provenance",
        query={"evidence_id": "evidence-1"},
    )

    payload = result.json()

    assert payload["evidence_status"] == "NON_AUTHORITATIVE"
    assert [edge["target_id"] for edge in payload["edges"]] == [
        "artifact-safe-1",
        "artifact-secret-2",
    ]
    assert [node["node_id"] for node in payload["nodes"]] == [
        "artifact-safe-1",
        "artifact-secret-2",
        "evidence-1",
    ]
    assert payload["evidence_records"][0]["evidence_status"] == "NON_AUTHORITATIVE"
    assert payload["authority"]["scheduling_authority"] is False


def test_browser_rendered_html_carries_non_authoritative_status_without_secret_values() -> None:
    result = create_app(_artifact_services()).get(f"{PRODUCT_UI_PREFIX}/artifacts")

    body = result.text()

    assert result.content_type == "text/html; charset=utf-8"
    assert "NON_AUTHORITATIVE evidence" in body
    assert "explicit human gates" in body
    assert "independent verification required" in body
    assert "token-value" not in body
    assert "password-value" not in body


def test_default_ui_source_projects_product_handoff_artifacts() -> None:
    payload = create_app().get(
        f"{PRODUCT_UI_PREFIX}/provenance",
        query={"evidence_id": "PRD-EVID-PRODUCT-HANDOFF"},
    ).json()

    assert payload["evidence_status"] == "NON_AUTHORITATIVE"
    assert payload["evidence_records"][0]["authority_state"] == "NON_AUTHORITATIVE"
    assert "PRD-TASK-017" in payload["evidence_records"][0]["source_refs"]
    assert "prd-product-plan-lock" in {edge["target_id"] for edge in payload["edges"]}
    assert payload["authority"]["decision_dependencies"] == ["DECISION_REQUIRED:PRD-DEC-001"]


def _artifact_services() -> ProductUiServices:
    base = services()
    return ProductUiServices(
        api_services=base.api_services,
        operator_directory=base.operator_directory,
        session_store=base.session_store,
        dashboard_projection=base.dashboard_projection,
        evidence_browser=ProductEvidenceBrowserService(
            source=StaticEvidenceArtifactSource(
                artifacts=(
                    ArtifactRecord(
                        artifact_id="artifact-secret-2",
                        artifact_type="runtime_log",
                        kind="log",
                        title="Runtime log metadata",
                        relative_path="artifacts/runtime.log",
                        content_hash="b" * 64,
                        producer="artifact-registry",
                        contains_secret=True,
                        metadata={
                            "api_token": "token-value",
                            "nested": {"password": "password-value"},
                        },
                    ),
                    ArtifactRecord(
                        artifact_id="artifact-safe-1",
                        artifact_type="requirements_trace",
                        kind="evidence",
                        title="Requirements trace",
                        relative_path="artifacts/trace.json",
                        content_hash="a" * 64,
                        producer="artifact-registry",
                        authority_state="VALIDATED",
                        metadata={"safe": "visible"},
                    ),
                ),
                evidence=(
                    EvidenceRecord(
                        evidence_id="evidence-1",
                        title="Evidence packet",
                        summary="Browser evidence packet.",
                        source_refs=("PRD-TASK-010",),
                        artifact_ids=("artifact-safe-1", "artifact-secret-2"),
                        human_gate_ids=("GATE-PRD-HITL-UI",),
                        metadata={"client_secret": "secret-value"},
                    ),
                ),
                edges=(
                    ProvenanceEdge(
                        source_id="evidence-1",
                        target_id="artifact-secret-2",
                        relationship="supports_metadata_for",
                    ),
                    ProvenanceEdge(
                        source_id="evidence-1",
                        target_id="artifact-safe-1",
                        relationship="supports_metadata_for",
                    ),
                ),
            )
        ),
    )
