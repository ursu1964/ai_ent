from __future__ import annotations

from ai_ent.external_project import CONTROL_PLANE_AUTHORITY, MANDATORY_VERIFICATION_COMMANDS
from ai_ent_product_api import create_app
from ai_ent_product_ui import (
    ApprovalConsole,
    ApprovalConsoleDecisionRequest,
    ApprovalGateCard,
    render_approval_console,
)


def test_approval_console_projects_gate_scoped_conditions_and_authority() -> None:
    state = ApprovalConsole(create_app()).state()
    gate = _gate(state.gates, "GATE-PRD-HITL-UI")

    assert state.screen_id == "human-approval-console"
    assert state.control_plane_authority == CONTROL_PLANE_AUTHORITY
    assert state.grants_control_plane_authority is False
    assert state.gate_approval_authority is False
    assert state.verifier_bypass_authority is False
    assert state.scheduling_authority is False
    assert state.implicit_human_gate_approval is False
    assert state.independent_verification_required is True
    assert state.mandatory_verification_commands == MANDATORY_VERIFICATION_COMMANDS
    assert state.decision_dependencies == ("DECISION_REQUIRED:PRD-DEC-001",)
    assert state.secret_values_exposed is False
    assert "submit_human_gate_decision" in state.allowed_operations
    assert "approve_gate" in state.denied_operations

    assert gate.task_id == "PRD-TASK-016"
    assert gate.explicit_human_review_required is True
    assert gate.independent_verification_required is True
    assert gate.applies_runtime_gate_mutation is False
    assert gate.grants_gate_approval_authority is False
    assert gate.verifier_bypass_granted is False
    assert gate.scope_task_ids[0] == "PRD-TASK-016"
    assert {condition.kind for condition in gate.conditions} == {
        "expected_evidence",
        "mandatory_verification",
        "scope",
    }
    assert all(not condition.satisfied for condition in gate.conditions)
    assert {action.decision for action in gate.actions} == {"approve", "reject"}
    assert all(not action.applies_runtime_gate_mutation for action in gate.actions)

    html = render_approval_console(state)
    assert 'data-ui="human-approval-console"' in html
    assert "GATE-PRD-HITL-UI" in html
    assert "PRD-TASK-016" in html


def test_approval_console_preview_requires_evidence_verification_and_valid_scope() -> None:
    console = ApprovalConsole(create_app())
    preview = console.preview_decision(
        ApprovalConsoleDecisionRequest(
            gate_id="GATE-PRD-HITL-UI",
            actor_id="operator-1",
            decision="approve",
            rationale="Review attempted before required checks are complete.",
            evidence_refs=(),
            verification_commands=(),
            scope_task_ids=("PRD-TASK-999",),
        )
    )

    assert preview.can_submit_to_control_plane is False
    assert preview.evidence_valid is False
    assert preview.missing_mandatory_verification_commands == MANDATORY_VERIFICATION_COMMANDS
    assert preview.unknown_scope_task_ids == ("PRD-TASK-999",)
    assert preview.missing_expected_evidence
    assert any(
        blocker.startswith("missing expected evidence:") for blocker in preview.blockers
    )
    assert any(
        blocker.startswith("missing mandatory verification:") for blocker in preview.blockers
    )
    assert "unknown scope task:PRD-TASK-999" in preview.blockers
    assert preview.explicit_human_review_required is True
    assert preview.independent_verification_required is True
    assert preview.applies_runtime_gate_mutation is False
    assert preview.grants_gate_approval_authority is False
    assert preview.verifier_bypass_granted is False


def test_approval_console_rejection_can_be_submitted_without_approval_evidence() -> None:
    console = ApprovalConsole(create_app())
    preview = console.preview_decision(
        ApprovalConsoleDecisionRequest(
            gate_id="GATE-PRD-HITL-UI",
            actor_id="operator-1",
            decision="reject",
            rationale="Rejecting because expected approval evidence is absent.",
            evidence_refs=(),
            verification_commands=MANDATORY_VERIFICATION_COMMANDS,
            scope_task_ids=("PRD-TASK-016",),
        )
    )

    assert preview.can_submit_to_control_plane is True
    assert preview.evidence_valid is False
    assert preview.missing_expected_evidence
    assert preview.missing_mandatory_verification_commands == ()
    assert preview.unknown_scope_task_ids == ()
    assert not any(
        blocker.startswith("missing expected evidence:") for blocker in preview.blockers
    )


def test_approval_console_submission_is_non_mutating_control_plane_review_request() -> None:
    console = ApprovalConsole(create_app())
    gate = console.gate("GATE-PRD-HITL-UI")
    submission = console.submit_decision(
        ApprovalConsoleDecisionRequest(
            gate_id=gate.gate_id,
            actor_id="operator-1",
            decision="approve",
            rationale="Explicit operator review completed.",
            evidence_refs=gate.expected_evidence,
            verification_commands=MANDATORY_VERIFICATION_COMMANDS,
            scope_task_ids=("PRD-TASK-016",),
        )
    )

    assert submission.requested_decision == "approve"
    assert submission.request_accepted is True
    assert submission.decision_status == "CONTROL_PLANE_REVIEW_REQUIRED"
    assert len(submission.decision_request_hash) == 64
    assert submission.scope_task_ids == ("PRD-TASK-016",)
    assert submission.evidence_valid is True
    assert submission.runtime_gate_status_before == "PENDING_NOT_APPROVED"
    assert submission.runtime_gate_status_after == "PENDING_NOT_APPROVED"
    assert submission.applied_to_runtime is False
    assert submission.explicit_human_review_required is True
    assert submission.independent_verification_required is True
    assert submission.grants_gate_approval_authority is False
    assert submission.verifier_bypass_granted is False
    assert submission.scheduling_authority_granted is False
    assert submission.decision_dependencies == ("DECISION_REQUIRED:PRD-DEC-001",)


def test_approval_console_outputs_do_not_echo_secret_values() -> None:
    console = ApprovalConsole(create_app())
    gate = console.gate("GATE-PRD-HITL-UI")
    request = ApprovalConsoleDecisionRequest(
        gate_id=gate.gate_id,
        actor_id="operator-1",
        decision="approve",
        rationale="token-value",
        evidence_refs=(*gate.expected_evidence, "password-value"),
        verification_commands=MANDATORY_VERIFICATION_COMMANDS,
        scope_task_ids=("PRD-TASK-016",),
    )

    preview = console.preview_decision(request).as_dict()
    submission = console.submit_decision(request).as_dict()
    rendered = render_approval_console(console.state())

    assert "token-value" not in str(preview)
    assert "password-value" not in str(preview)
    assert "token-value" not in str(submission)
    assert "password-value" not in str(submission)
    assert "token-value" not in rendered
    assert "password-value" not in rendered


def _gate(gates: tuple[ApprovalGateCard, ...], gate_id: str) -> ApprovalGateCard:
    for gate in gates:
        if gate.gate_id == gate_id:
            return gate
    raise AssertionError(f"missing gate {gate_id}")
