from __future__ import annotations

from dataclasses import asdict, dataclass
from html import escape
from typing import Any, Literal

from ai_ent_product_api import PRODUCT_API_PREFIX, ProductApiApp, create_app

Decision = Literal["approve", "reject"]
ConditionKind = Literal["expected_evidence", "mandatory_verification", "scope"]


@dataclass(frozen=True)
class ApprovalCondition:
    condition_id: str
    kind: ConditionKind
    label: str
    gate_id: str
    scope_task_id: str | None = None
    required: bool = True
    satisfied: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ApprovalDecisionAction:
    decision: Decision
    label: str
    submit_path: str
    requires_operator_rationale: bool = True
    requires_explicit_human_review: bool = True
    requires_independent_verification: bool = True
    applies_runtime_gate_mutation: bool = False
    grants_gate_approval_authority: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ApprovalGateCard:
    gate_id: str
    task_id: str
    status: str
    reason: str
    risk_level: str
    approval_boundary: str
    resume_semantics: str
    scope_task_ids: tuple[str, ...]
    expected_evidence: tuple[str, ...]
    conditions: tuple[ApprovalCondition, ...]
    actions: tuple[ApprovalDecisionAction, ...]
    explicit_human_review_required: bool = True
    independent_verification_required: bool = True
    applies_runtime_gate_mutation: bool = False
    grants_gate_approval_authority: bool = False
    verifier_bypass_granted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ApprovalConsoleState:
    screen_id: str
    product_plan_id: str
    plan_version: str
    decision_dependencies: tuple[str, ...]
    control_plane_authority: str
    authority_mode: str
    allowed_operations: tuple[str, ...]
    denied_operations: tuple[str, ...]
    gates: tuple[ApprovalGateCard, ...]
    pending_count: int
    approved_count: int
    rejected_count: int
    mandatory_verification_commands: tuple[str, ...]
    explicit_human_review_required: bool = True
    independent_verification_required: bool = True
    grants_control_plane_authority: bool = False
    gate_approval_authority: bool = False
    verifier_bypass_authority: bool = False
    scheduling_authority: bool = False
    implicit_human_gate_approval: bool = False
    secret_values_exposed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ApprovalConsoleDecisionRequest:
    gate_id: str
    actor_id: str
    decision: Decision
    rationale: str
    evidence_refs: tuple[str, ...]
    verification_commands: tuple[str, ...]
    scope_task_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApprovalConsoleDecisionPreview:
    gate_id: str
    task_id: str
    requested_decision: Decision
    scope_task_ids: tuple[str, ...]
    can_submit_to_control_plane: bool
    blockers: tuple[str, ...]
    evidence_valid: bool
    missing_expected_evidence: tuple[str, ...]
    missing_mandatory_verification_commands: tuple[str, ...]
    unknown_scope_task_ids: tuple[str, ...]
    conditions: tuple[ApprovalCondition, ...]
    explicit_human_review_required: bool = True
    independent_verification_required: bool = True
    applies_runtime_gate_mutation: bool = False
    grants_gate_approval_authority: bool = False
    verifier_bypass_granted: bool = False
    secret_values_exposed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ApprovalDecisionSubmission:
    gate_id: str
    task_id: str
    requested_decision: Decision
    request_accepted: bool
    decision_status: str
    decision_request_hash: str
    scope_task_ids: tuple[str, ...]
    evidence_valid: bool
    missing_expected_evidence: tuple[str, ...]
    missing_mandatory_verification_commands: tuple[str, ...]
    unknown_scope_task_ids: tuple[str, ...]
    runtime_gate_status_before: str
    runtime_gate_status_after: str
    applied_to_runtime: bool
    explicit_human_review_required: bool
    independent_verification_required: bool
    grants_gate_approval_authority: bool
    verifier_bypass_granted: bool
    scheduling_authority_granted: bool
    decision_dependencies: tuple[str, ...]
    secret_values_exposed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ApprovalConsole:
    def __init__(self, api: ProductApiApp | None = None) -> None:
        self._api = api or create_app()

    def state(self) -> ApprovalConsoleState:
        boundary = _data(self._api.get(f"{PRODUCT_API_PREFIX}/boundary"))
        reviews = _data(self._api.get(f"{PRODUCT_API_PREFIX}/human-gates/reviews"))
        mandatory_commands = _tuple(boundary["mandatory_verification_commands"])
        return ApprovalConsoleState(
            screen_id="human-approval-console",
            product_plan_id=str(reviews["product_plan_id"]),
            plan_version=str(reviews["plan_version"]),
            decision_dependencies=_tuple(boundary["decision_dependencies"]),
            control_plane_authority=str(boundary["control_plane_authority"]),
            authority_mode=str(boundary["authority_mode"]),
            allowed_operations=_tuple(boundary["allowed_operations"]),
            denied_operations=_tuple(boundary["denied_operations"]),
            gates=tuple(
                _gate_card(review, mandatory_commands) for review in _dicts(reviews["reviews"])
            ),
            pending_count=int(reviews["pending_count"]),
            approved_count=int(reviews["approved_count"]),
            rejected_count=int(reviews["rejected_count"]),
            mandatory_verification_commands=mandatory_commands,
            independent_verification_required=bool(boundary["independent_verification_required"]),
            grants_control_plane_authority=bool(boundary["grants_control_plane_authority"]),
            gate_approval_authority=bool(boundary["gate_approval_authority"]),
            verifier_bypass_authority=bool(boundary["verifier_bypass_authority"]),
            scheduling_authority=bool(boundary["scheduling_authority"]),
            implicit_human_gate_approval=bool(boundary["implicit_human_gate_approval"]),
            secret_values_exposed=bool(boundary["secret_values_exposed"]),
        )

    def preview_decision(
        self,
        request: ApprovalConsoleDecisionRequest,
    ) -> ApprovalConsoleDecisionPreview:
        gate = self.gate(request.gate_id)
        scope_task_ids = request.scope_task_ids or (gate.task_id,)
        validation = _data(
            self._api.post(
                f"{PRODUCT_API_PREFIX}/human-gates/{request.gate_id}/evidence/validate",
                json_payload={
                    "actor_id": request.actor_id,
                    "evidence_refs": list(request.evidence_refs),
                    "verification_commands": list(request.verification_commands),
                    "scope_task_ids": list(scope_task_ids),
                },
            )
        )
        missing_evidence = _tuple(validation["missing_expected_evidence"])
        missing_commands = _tuple(validation["missing_mandatory_verification_commands"])
        unknown_scope = _tuple(validation["unknown_scope_task_ids"])
        blockers = _preview_blockers(
            request.decision,
            missing_evidence,
            missing_commands,
            unknown_scope,
        )
        return ApprovalConsoleDecisionPreview(
            gate_id=gate.gate_id,
            task_id=gate.task_id,
            requested_decision=request.decision,
            scope_task_ids=scope_task_ids,
            can_submit_to_control_plane=not blockers,
            blockers=blockers,
            evidence_valid=bool(validation["valid"]),
            missing_expected_evidence=missing_evidence,
            missing_mandatory_verification_commands=missing_commands,
            unknown_scope_task_ids=unknown_scope,
            conditions=_decision_conditions(
                gate=gate,
                scope_task_ids=scope_task_ids,
                missing_expected_evidence=missing_evidence,
                missing_mandatory_verification_commands=missing_commands,
                unknown_scope_task_ids=unknown_scope,
            ),
            explicit_human_review_required=bool(validation["explicit_human_review_required"]),
            independent_verification_required=bool(validation["independent_verification_required"]),
            applies_runtime_gate_mutation=bool(validation["applies_runtime_gate_mutation"]),
            grants_gate_approval_authority=bool(validation["grants_gate_approval_authority"]),
            verifier_bypass_granted=bool(validation["verifier_bypass_granted"]),
        )

    def submit_decision(
        self,
        request: ApprovalConsoleDecisionRequest,
    ) -> ApprovalDecisionSubmission:
        path = f"{PRODUCT_API_PREFIX}/human-gates/{request.gate_id}/{request.decision}"
        payload = {
            "actor_id": request.actor_id,
            "rationale": request.rationale,
            "evidence_refs": list(request.evidence_refs),
            "verification_commands": list(request.verification_commands),
            "scope_task_ids": list(request.scope_task_ids),
        }
        response = _data(self._api.post(path, json_payload=payload))
        return ApprovalDecisionSubmission(
            gate_id=str(response["gate_id"]),
            task_id=str(response["task_id"]),
            requested_decision=response["requested_decision"],
            request_accepted=bool(response["request_accepted"]),
            decision_status=str(response["decision_status"]),
            decision_request_hash=str(response["decision_request_hash"]),
            scope_task_ids=_tuple(response["scope_task_ids"]),
            evidence_valid=bool(response["evidence_valid"]),
            missing_expected_evidence=_tuple(response["missing_expected_evidence"]),
            missing_mandatory_verification_commands=_tuple(
                response["missing_mandatory_verification_commands"]
            ),
            unknown_scope_task_ids=_tuple(response["unknown_scope_task_ids"]),
            runtime_gate_status_before=str(response["runtime_gate_status_before"]),
            runtime_gate_status_after=str(response["runtime_gate_status_after"]),
            applied_to_runtime=bool(response["applied_to_runtime"]),
            explicit_human_review_required=bool(response["explicit_human_review_required"]),
            independent_verification_required=bool(response["independent_verification_required"]),
            grants_gate_approval_authority=bool(response["grants_gate_approval_authority"]),
            verifier_bypass_granted=bool(response["verifier_bypass_granted"]),
            scheduling_authority_granted=bool(response["scheduling_authority_granted"]),
            decision_dependencies=_tuple(response["decision_dependencies"]),
        )

    def gate(self, gate_id: str) -> ApprovalGateCard:
        for gate in self.state().gates:
            if gate.gate_id == gate_id:
                return gate
        raise KeyError(f"unknown human gate: {gate_id}")


def render_approval_console(state: ApprovalConsoleState) -> str:
    gate_markup = "\n".join(_render_gate(gate) for gate in state.gates)
    return (
        '<section data-ui="human-approval-console" '
        f'data-authority-mode="{escape(state.authority_mode)}" '
        f'data-control-plane-authority="{escape(state.control_plane_authority)}" '
        f'data-independent-verification="{str(state.independent_verification_required).lower()}">'
        f'<header><h1>{escape("Human approvals")}</h1>'
        f"<p>{escape(state.product_plan_id)} {escape(state.plan_version)}</p></header>"
        f"{gate_markup}</section>"
    )


def _gate_card(
    review: dict[str, Any],
    mandatory_verification_commands: tuple[str, ...],
) -> ApprovalGateCard:
    gate_id = str(review["gate_id"])
    task_id = str(review["task_id"])
    downstream = _tuple(review["downstream_task_ids"])
    scope_task_ids = (task_id, *downstream)
    expected_evidence = _tuple(review["expected_evidence"])
    return ApprovalGateCard(
        gate_id=gate_id,
        task_id=task_id,
        status=str(review["status"]),
        reason=str(review["reason"]),
        risk_level=str(review["risk_level"]),
        approval_boundary=str(review["approval_boundary"]),
        resume_semantics=str(review["resume_semantics"]),
        scope_task_ids=scope_task_ids,
        expected_evidence=expected_evidence,
        conditions=(
            *_expected_evidence_conditions(gate_id, expected_evidence),
            *_verification_conditions(gate_id, mandatory_verification_commands),
            *_scope_conditions(gate_id, scope_task_ids, satisfied_task_ids=()),
        ),
        actions=(
            _action(gate_id, "approve"),
            _action(gate_id, "reject"),
        ),
        explicit_human_review_required=bool(review["explicit_human_review_required"]),
        independent_verification_required=bool(review["independent_verification_required"]),
        grants_gate_approval_authority=bool(review["authority"]["gate_approval_authority"]),
        verifier_bypass_granted=bool(review["authority"]["verifier_bypass_authority"]),
    )


def _action(gate_id: str, decision: Decision) -> ApprovalDecisionAction:
    return ApprovalDecisionAction(
        decision=decision,
        label=decision.title(),
        submit_path=f"{PRODUCT_API_PREFIX}/human-gates/{gate_id}/{decision}",
    )


def _decision_conditions(
    *,
    gate: ApprovalGateCard,
    scope_task_ids: tuple[str, ...],
    missing_expected_evidence: tuple[str, ...],
    missing_mandatory_verification_commands: tuple[str, ...],
    unknown_scope_task_ids: tuple[str, ...],
) -> tuple[ApprovalCondition, ...]:
    return (
        *_expected_evidence_conditions(
            gate.gate_id,
            gate.expected_evidence,
            missing=missing_expected_evidence,
        ),
        *_verification_conditions(
            gate.gate_id,
            _mandatory_verification_labels(gate.conditions),
            missing=missing_mandatory_verification_commands,
        ),
        *_scope_conditions(
            gate.gate_id,
            scope_task_ids,
            satisfied_task_ids=tuple(
                task_id for task_id in scope_task_ids if task_id not in unknown_scope_task_ids
            ),
        ),
    )


def _expected_evidence_conditions(
    gate_id: str,
    evidence_refs: tuple[str, ...],
    *,
    missing: tuple[str, ...] | None = None,
) -> tuple[ApprovalCondition, ...]:
    missing_set = set(evidence_refs if missing is None else missing)
    return tuple(
        ApprovalCondition(
            condition_id=f"{gate_id}:expected-evidence:{index}",
            kind="expected_evidence",
            label=evidence_ref,
            gate_id=gate_id,
            satisfied=evidence_ref not in missing_set,
        )
        for index, evidence_ref in enumerate(evidence_refs, start=1)
    )


def _verification_conditions(
    gate_id: str,
    commands: tuple[str, ...],
    *,
    missing: tuple[str, ...] | None = None,
) -> tuple[ApprovalCondition, ...]:
    missing_set = set(commands if missing is None else missing)
    return tuple(
        ApprovalCondition(
            condition_id=f"{gate_id}:mandatory-verification:{index}",
            kind="mandatory_verification",
            label=command,
            gate_id=gate_id,
            satisfied=command not in missing_set,
        )
        for index, command in enumerate(commands, start=1)
    )


def _scope_conditions(
    gate_id: str,
    scope_task_ids: tuple[str, ...],
    *,
    satisfied_task_ids: tuple[str, ...],
) -> tuple[ApprovalCondition, ...]:
    satisfied_set = set(satisfied_task_ids)
    return tuple(
        ApprovalCondition(
            condition_id=f"{gate_id}:scope:{task_id}",
            kind="scope",
            label=f"Scoped task {task_id}",
            gate_id=gate_id,
            scope_task_id=task_id,
            satisfied=task_id in satisfied_set,
        )
        for task_id in scope_task_ids
    )


def _preview_blockers(
    decision: Decision,
    missing_expected_evidence: tuple[str, ...],
    missing_mandatory_verification_commands: tuple[str, ...],
    unknown_scope_task_ids: tuple[str, ...],
) -> tuple[str, ...]:
    blockers = [
        f"missing mandatory verification:{item}"
        for item in missing_mandatory_verification_commands
    ]
    blockers.extend(f"unknown scope task:{item}" for item in unknown_scope_task_ids)
    if decision == "approve":
        blockers.extend(f"missing expected evidence:{item}" for item in missing_expected_evidence)
    return tuple(blockers)


def _mandatory_verification_labels(
    conditions: tuple[ApprovalCondition, ...],
) -> tuple[str, ...]:
    return tuple(
        condition.label for condition in conditions if condition.kind == "mandatory_verification"
    )


def _render_gate(gate: ApprovalGateCard) -> str:
    conditions = "".join(
        "<li "
        f'data-kind="{escape(condition.kind)}" '
        f'data-satisfied="{str(condition.satisfied).lower()}">'
        f"{escape(condition.label)}</li>"
        for condition in gate.conditions
    )
    actions = "".join(
        "<button "
        f'data-decision="{escape(action.decision)}" '
        f'data-submit-path="{escape(action.submit_path)}">'
        f"{escape(action.label)}</button>"
        for action in gate.actions
    )
    return (
        f'<article data-gate-id="{escape(gate.gate_id)}" '
        f'data-task-id="{escape(gate.task_id)}">'
        f"<h2>{escape(gate.gate_id)}</h2>"
        f'<p data-status="{escape(gate.status)}">{escape(gate.reason)}</p>'
        f"<ul>{conditions}</ul>"
        f"<form>{actions}</form>"
        "</article>"
    )


def _data(result: Any) -> dict[str, Any]:
    payload = result.json()
    if result.status_code != 200:
        error = payload.get("error", {})
        raise RuntimeError(str(error.get("message", "product API request failed")))
    data = payload["data"]
    if not isinstance(data, dict):
        raise TypeError("product API response data must be an object")
    return data


def _tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    return (str(value),)


def _dicts(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(item for item in value if isinstance(item, dict))
