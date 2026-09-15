from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from ai_ent.persistence.models import Checkpoint, Task
from ai_ent_product_api.errors import REDACTED, redact_secret_details
from ai_ent_product_api.schemas import (
    ExecutionSnapshot,
    RepairSnapshot,
    RuntimeAuthoritySnapshot,
    TaskSnapshot,
)
from ai_ent_product_api.services import ProductApiServices
from ai_ent_product_ui.schemas import (
    CommitReference,
    ExecutionAttemptView,
    ExecutionMonitoringDashboard,
    HumanGateView,
    RepairHistoryView,
    TaskDagEdge,
    TaskDagNode,
    TaskDetailView,
    UiAuthoritySnapshot,
    VerifierOutputView,
)

_SECRET_TEXT_PATTERN = re.compile(
    r"(?i)\b(access_token|api_key|api_token|authorization|client_secret|cookie|"
    r"credential|credentials|passwd|password|private_key|refresh_token|secret|session|"
    r"token)\b\s*[:=]\s*[^,\s;]+"
)
_TERMINAL_DEPENDENCY_STATUSES = frozenset({"passed", "succeeded", "complete", "completed"})


@dataclass(frozen=True)
class ProductExecutionMonitor:
    services: ProductApiServices = field(default_factory=ProductApiServices.defaults)

    @classmethod
    def defaults(cls) -> ProductExecutionMonitor:
        return cls()

    def dashboard(self) -> ExecutionMonitoringDashboard:
        generated_dag = self.services.generated_dag()
        tasks = self.services.tasks()
        executions = self.services.executions()
        repairs = self.services.repairs()
        runtime_state = self.services.runtime_state()
        checkpoints = self._checkpoints_by_execution()

        gates = _human_gates(runtime_state.runtime_human_gates, generated_dag.human_gates)
        edges = tuple(
            TaskDagEdge(
                dependency_task_id=edge.dependency_task_id,
                task_id=edge.task_id,
            )
            for edge in generated_dag.dependency_edges
        )
        downstream = _downstream(edges)
        attempts = tuple(
            _attempt_view(execution, checkpoints.get(execution.execution_id, ()))
            for execution in executions.executions
        )
        attempts_by_task = _group_by_task(attempts)
        repairs_by_task = _repairs_by_task(repairs.repairs)

        task_details = tuple(
            _task_detail(
                task=task,
                task_gates=tuple(gate for gate in gates if gate.task_id == task.task_id),
                downstream_dependents=downstream.get(task.task_id, ()),
                execution_attempts=attempts_by_task.get(task.task_id, ()),
                repair_history=repairs_by_task.get(task.task_id, ()),
            )
            for task in tasks.tasks
        )
        dependencies_by_task = {
            detail.task_id: detail.upstream_dependencies for detail in task_details
        }
        task_statuses = {detail.task_id: detail.status for detail in task_details}
        nodes = tuple(
            _dag_node(
                detail,
                attempts_by_task.get(detail.task_id, ()),
                dependencies_by_task=dependencies_by_task,
                task_statuses=task_statuses,
            )
            for detail in task_details
        )
        all_verifier_outputs = tuple(
            output
            for attempt in attempts
            for output in attempt.verifier_outputs
        )
        all_commits = tuple(commit for attempt in attempts for commit in attempt.commits)

        return ExecutionMonitoringDashboard(
            product_plan_id=tasks.product_plan_id,
            plan_version=tasks.plan_version,
            task_dag_nodes=nodes,
            task_dag_edges=edges,
            task_details=task_details,
            execution_attempts=attempts,
            repair_history=tuple(_repair_view(repair) for repair in repairs.repairs),
            verifier_outputs=all_verifier_outputs,
            commits=all_commits,
            human_gates=gates,
            authority=_ui_authority(tasks.authority),
        )

    def task_detail(self, task_id: str) -> TaskDetailView | None:
        for detail in self.dashboard().task_details:
            if detail.task_id == task_id:
                return detail
        return None

    def _checkpoints_by_execution(self) -> dict[str, tuple[Checkpoint, ...]]:
        runtime_service = self.services.runtime_service
        if runtime_service.session_factory is None:
            return {}

        grouped: dict[str, list[Checkpoint]] = {}
        with runtime_service.session_factory() as session:
            for checkpoint in session.scalars(
                select(Checkpoint)
                .join(Task, Checkpoint.task_id == Task.id)
                .where(Task.id.like("PRD-TASK-%"), Checkpoint.execution_id.is_not(None))
                .order_by(Checkpoint.task_id, Checkpoint.created_at, Checkpoint.id)
            ):
                if checkpoint.execution_id is not None:
                    grouped.setdefault(checkpoint.execution_id, []).append(checkpoint)
        return {execution_id: tuple(items) for execution_id, items in grouped.items()}


def _ui_authority(authority: RuntimeAuthoritySnapshot) -> UiAuthoritySnapshot:
    return UiAuthoritySnapshot(
        grants_control_plane_authority=authority.grants_control_plane_authority,
        gate_approval_authority=authority.gate_approval_authority,
        verifier_bypass_authority=authority.verifier_bypass_authority,
        commit_boundary_bypass_authority=authority.commit_boundary_bypass_authority,
        scheduling_authority=authority.scheduling_authority,
        runtime_execution_authority=authority.runtime_execution_authority,
        repair_execution_authority=authority.repair_execution_authority,
        policy_weakening_authority=authority.policy_weakening_authority,
        human_gate_policy=authority.human_gate_policy,
        implicit_human_gate_approval=authority.implicit_human_gate_approval,
        independent_verification_required=authority.independent_verification_required,
        mandatory_verification_commands=authority.mandatory_verification_commands,
    )


def _human_gates(
    runtime_gates: tuple[Any, ...],
    generated_gates: tuple[Any, ...],
) -> tuple[HumanGateView, ...]:
    generated_by_id = {gate.gate_id: gate for gate in generated_gates}
    views = [
        HumanGateView(
            gate_id=gate.gate_id,
            task_id=gate.task_id,
            status=gate.status,
            reason=gate.reason,
            risk_level=getattr(generated_by_id.get(gate.gate_id), "risk_level", ""),
            approval_boundary=gate.approval_boundary,
        )
        for gate in runtime_gates
    ]
    runtime_ids = {gate.gate_id for gate in views}
    views.extend(
        HumanGateView(
            gate_id=gate.gate_id,
            task_id=gate.task_id,
            status=gate.status,
            reason=gate.reason,
            risk_level=gate.risk_level,
            approval_boundary=gate.approval_boundary,
        )
        for gate in generated_gates
        if gate.gate_id not in runtime_ids
    )
    return tuple(sorted(views, key=lambda gate: (gate.task_id, gate.gate_id)))


def _attempt_view(
    execution: ExecutionSnapshot,
    checkpoints: tuple[Checkpoint, ...],
) -> ExecutionAttemptView:
    verifier_outputs = tuple(_verifier_output(checkpoint) for checkpoint in checkpoints)
    commits = _commit_references(execution, checkpoints)
    return ExecutionAttemptView(
        execution_id=execution.execution_id,
        task_id=execution.task_id,
        executor_type=execution.executor_type,
        status=execution.status,
        attempt=execution.attempt,
        terminal_state=execution.terminal_state,
        error_classification=execution.error_classification,
        candidate_tree_hash=execution.candidate_tree_hash,
        active_lease=execution.active_lease,
        checkpoint_count=execution.checkpoint_count,
        verifier_outputs=verifier_outputs,
        commits=commits,
    )


def _verifier_output(checkpoint: Checkpoint) -> VerifierOutputView:
    parsed = _parse_checkpoint_state(checkpoint.state)
    safe_payload = _sanitize_ui_payload(redact_secret_details(parsed))
    return VerifierOutputView(
        checkpoint_id=checkpoint.id,
        execution_id=checkpoint.execution_id or "",
        task_id=checkpoint.task_id,
        status=_optional_string(_mapping_get(parsed, "status", "verification_status")),
        ok=_optional_bool(_mapping_get(parsed, "ok")),
        findings=_redacted_strings(_mapping_get(parsed, "findings", "errors", "messages")),
        commands=_redacted_strings(_mapping_get(parsed, "commands", "verification_commands")),
        sanitized_payload=safe_payload,
    )


def _parse_checkpoint_state(state: str) -> Any:
    try:
        parsed = json.loads(state)
    except json.JSONDecodeError:
        return {"unparsed_checkpoint_state": True}
    if isinstance(parsed, (Mapping, list)):
        return parsed
    return {"value": parsed}


def _mapping_get(value: Any, *keys: str) -> Any:
    if not isinstance(value, Mapping):
        return None
    for key in keys:
        if key in value:
            return value[key]
    return None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return _redact_text(str(value))


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _redacted_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (_redact_text(value),)
    if isinstance(value, list | tuple):
        return tuple(_redact_text(str(item)) for item in value)
    return (_redact_text(str(value)),)


def _redact_text(value: str) -> str:
    return _SECRET_TEXT_PATTERN.sub(lambda match: f"{match.group(1)}={REDACTED}", value)


def _sanitize_ui_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _sanitize_ui_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_ui_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_ui_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _commit_references(
    execution: ExecutionSnapshot,
    checkpoints: tuple[Checkpoint, ...],
) -> tuple[CommitReference, ...]:
    commits: list[CommitReference] = []
    if execution.commit_hash:
        commits.append(
            CommitReference(
                source="execution",
                hash=execution.commit_hash,
                task_id=execution.task_id,
                execution_id=execution.execution_id,
                candidate_tree_hash=execution.candidate_tree_hash,
            )
        )
    for checkpoint in checkpoints:
        if checkpoint.commit_hash:
            commits.append(
                CommitReference(
                    source="checkpoint",
                    hash=checkpoint.commit_hash,
                    task_id=checkpoint.task_id,
                    execution_id=checkpoint.execution_id,
                    checkpoint_id=checkpoint.id,
                    tree_hash=checkpoint.tree_hash,
                )
            )
    return tuple(commits)


def _group_by_task(
    attempts: tuple[ExecutionAttemptView, ...],
) -> dict[str, tuple[ExecutionAttemptView, ...]]:
    grouped: dict[str, list[ExecutionAttemptView]] = {}
    for attempt in attempts:
        grouped.setdefault(attempt.task_id, []).append(attempt)
    return {task_id: tuple(items) for task_id, items in grouped.items()}


def _repairs_by_task(
    repairs: tuple[RepairSnapshot, ...],
) -> dict[str, tuple[RepairHistoryView, ...]]:
    grouped: dict[str, list[RepairHistoryView]] = {}
    for repair in repairs:
        grouped.setdefault(repair.task_id, []).append(_repair_view(repair))
    return {task_id: tuple(items) for task_id, items in grouped.items()}


def _repair_view(repair: RepairSnapshot) -> RepairHistoryView:
    return RepairHistoryView(
        failed_execution_id=repair.failed_execution_id,
        task_id=repair.task_id,
        category=repair.category,
        stage=repair.stage,
        retryability=repair.retryability,
        reason=repair.reason,
        recommended_action=repair.recommended_action,
        repair_attempt_count=repair.repair_attempt_count,
        max_autonomous_repair_attempts=repair.max_autonomous_repair_attempts,
        human_gate_required=repair.human_gate_required,
        creates_execution=repair.creates_execution,
        grants_repair_authority=repair.grants_repair_authority,
    )


def _task_detail(
    *,
    task: TaskSnapshot,
    task_gates: tuple[HumanGateView, ...],
    downstream_dependents: tuple[str, ...],
    execution_attempts: tuple[ExecutionAttemptView, ...],
    repair_history: tuple[RepairHistoryView, ...],
) -> TaskDetailView:
    verifier_outputs = tuple(
        output
        for attempt in execution_attempts
        for output in attempt.verifier_outputs
    )
    commits = tuple(commit for attempt in execution_attempts for commit in attempt.commits)
    return TaskDetailView(
        task_id=task.task_id,
        title=task.title,
        objective=task.objective,
        status=task.status,
        readiness_status=task.readiness_status,
        readiness_reasons=task.readiness_reasons,
        acceptance_criteria=task.acceptance_criteria,
        required_decisions=task.required_decisions,
        allowed_write_scope=task.allowed_write_scope,
        prohibited_paths=task.prohibited_paths,
        human_gates=task_gates,
        upstream_dependencies=task.dependencies,
        downstream_dependents=downstream_dependents,
        execution_attempts=execution_attempts,
        repair_history=repair_history,
        verifier_outputs=verifier_outputs,
        commits=commits,
    )


def _dag_node(
    detail: TaskDetailView,
    attempts: tuple[ExecutionAttemptView, ...],
    *,
    dependencies_by_task: Mapping[str, tuple[str, ...]],
    task_statuses: Mapping[str, str],
) -> TaskDagNode:
    latest = attempts[-1] if attempts else None
    latest_verifier = latest.verifier_outputs[-1] if latest and latest.verifier_outputs else None
    return TaskDagNode(
        task_id=detail.task_id,
        title=detail.title,
        status=detail.status,
        readiness_status=detail.readiness_status,
        readiness_reasons=detail.readiness_reasons,
        upstream_dependencies=detail.upstream_dependencies,
        downstream_dependents=detail.downstream_dependents,
        human_gate_ids=tuple(gate.gate_id for gate in detail.human_gates),
        required_decisions=detail.required_decisions,
        latest_attempt=latest.attempt if latest else None,
        latest_execution_status=latest.status if latest else None,
        latest_commit_hash=latest.commits[-1].hash if latest and latest.commits else None,
        verifier_status=latest_verifier.status if latest_verifier else None,
        blocked_by=_blocked_by(
            detail,
            dependencies_by_task=dependencies_by_task,
            task_statuses=task_statuses,
        ),
    )


def _blocked_by(
    detail: TaskDetailView,
    *,
    dependencies_by_task: Mapping[str, tuple[str, ...]],
    task_statuses: Mapping[str, str],
) -> tuple[str, ...]:
    blockers: list[str] = []
    blockers.extend(
        f"dependency:{task_id}"
        for task_id in _dependency_blockers(
            detail.task_id,
            dependencies_by_task=dependencies_by_task,
            task_statuses=task_statuses,
        )
    )
    blockers.extend(
        f"human_gate:{gate.gate_id}"
        for gate in detail.human_gates
        if gate.status not in {"approved", "APPROVED"}
    )
    blockers.extend(f"decision:{decision}" for decision in detail.required_decisions)
    blockers.extend(f"readiness:{reason}" for reason in detail.readiness_reasons)
    return tuple(dict.fromkeys(blockers))


def _dependency_blockers(
    task_id: str,
    *,
    dependencies_by_task: Mapping[str, tuple[str, ...]],
    task_statuses: Mapping[str, str],
) -> tuple[str, ...]:
    blockers: list[str] = []
    seen: set[str] = set()

    def visit(current_task_id: str) -> None:
        for dependency_task_id in dependencies_by_task.get(current_task_id, ()):
            if dependency_task_id in seen:
                continue
            seen.add(dependency_task_id)
            if task_statuses.get(dependency_task_id) not in _TERMINAL_DEPENDENCY_STATUSES:
                blockers.append(dependency_task_id)
            visit(dependency_task_id)

    visit(task_id)
    return tuple(blockers)


def _downstream(edges: tuple[TaskDagEdge, ...]) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for edge in edges:
        grouped.setdefault(edge.dependency_task_id, []).append(edge.task_id)
    return {task_id: tuple(sorted(dependents)) for task_id, dependents in grouped.items()}
