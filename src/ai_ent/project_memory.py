from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_ent.persistence.models import (
    Checkpoint,
    RuntimeHumanGate,
    RuntimePlanImport,
    RuntimeTaskPlanBinding,
    Task,
)
from ai_ent.persistence.repositories import CheckpointRepository, ProjectRepository, TaskRepository
from ai_ent.persistence.repositories.errors import DuplicateCheckpointError
from ai_ent.persistence.repositories.executions import ExecutionRepository
from ai_ent.project_manifest import GENERATED_MARKER, canonical_bytes

PROJECT_MEMORY_CONTRACT_VERSION = "c19.1"
PROJECT_MEMORY_SCHEMA_VERSION = "project-memory-output-v0.1"
PROJECT_MEMORY_RECORD_SCHEMA_VERSION = "project-memory-record-v0.1"
PROJECT_MEMORY_SERVICE_NAME = "project-memory"
PROJECT_MEMORY_COMPONENT_ID = "CMP-011"
PROJECT_MEMORY_CAPABILITY_ID = "C19"
PROJECT_MEMORY_REQUIREMENT_ID = "FR-006"
WORKER_PACKAGE_INTERFACE_ID = "IF-004"
EVIDENCE_RECORDING_INTERFACE_ID = "IF-005"

ProjectMemoryKind = Literal["context", "decision", "execution_evidence"]


class ProjectMemoryError(RuntimeError):
    """Raised when project memory cannot be recorded through the service boundary."""


@dataclass(frozen=True)
class ProjectMemoryEntry:
    memory_id: str
    kind: ProjectMemoryKind
    project_id: str
    task_id: str | None
    execution_id: str | None
    source: str
    summary: str
    payload: dict[str, Any]
    commit_hash: str | None = None
    tree_hash: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "kind": self.kind,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "source": self.source,
            "summary": self.summary,
            "payload": self.payload,
            "commit_hash": self.commit_hash,
            "tree_hash": self.tree_hash,
        }


@dataclass(frozen=True)
class ProjectMemorySnapshot:
    project_id: str
    entries: tuple[ProjectMemoryEntry, ...]
    contract_version: str = PROJECT_MEMORY_CONTRACT_VERSION
    schema_version: str = PROJECT_MEMORY_SCHEMA_VERSION

    @property
    def memory_hash(self) -> str:
        return hashlib.sha256(canonical_bytes(self._payload())).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["memory_hash"] = self.memory_hash
        return payload

    def _payload(self) -> dict[str, Any]:
        return {
            "generated": GENERATED_MARKER,
            "component_id": PROJECT_MEMORY_COMPONENT_ID,
            "capability_id": PROJECT_MEMORY_CAPABILITY_ID,
            "requirements": [PROJECT_MEMORY_REQUIREMENT_ID],
            "contract_version": self.contract_version,
            "schema_version": self.schema_version,
            "service": PROJECT_MEMORY_SERVICE_NAME,
            "interfaces": [
                WORKER_PACKAGE_INTERFACE_ID,
                EVIDENCE_RECORDING_INTERFACE_ID,
            ],
            "project_id": self.project_id,
            "entries": [entry.as_dict() for entry in self.entries],
            "summary": {
                "entry_count": len(self.entries),
                "context_count": _kind_count(self.entries, "context"),
                "decision_count": _kind_count(self.entries, "decision"),
                "execution_evidence_count": _kind_count(self.entries, "execution_evidence"),
            },
        }


class ProjectMemoryService:
    """Service boundary for C19 project memory over the runtime persistence model."""

    def __init__(
        self,
        *,
        projects: ProjectRepository | None = None,
        tasks: TaskRepository | None = None,
        executions: ExecutionRepository | None = None,
        checkpoints: CheckpointRepository | None = None,
        contract_version: str = PROJECT_MEMORY_CONTRACT_VERSION,
        schema_version: str = PROJECT_MEMORY_SCHEMA_VERSION,
    ) -> None:
        self.projects = projects or ProjectRepository()
        self.tasks = tasks or TaskRepository()
        self.executions = executions or ExecutionRepository()
        self.checkpoints = checkpoints or CheckpointRepository()
        self.contract_version = contract_version
        self.schema_version = schema_version

    def remember_context(
        self,
        session: Session,
        *,
        project_id: str,
        task_id: str,
        summary: str,
        context: dict[str, Any],
        source: str = "runtime-context",
    ) -> ProjectMemoryEntry:
        return self._remember(
            session,
            kind="context",
            project_id=project_id,
            task_id=task_id,
            execution_id=None,
            source=source,
            summary=summary,
            payload={"context": context},
        )

    def remember_decision(
        self,
        session: Session,
        *,
        project_id: str,
        task_id: str,
        decision: str,
        rationale: str,
        decided_by: str,
        source: str = "runtime-decision",
        evidence: dict[str, Any] | None = None,
    ) -> ProjectMemoryEntry:
        payload: dict[str, Any] = {
            "decision": decision,
            "rationale": rationale,
            "decided_by": decided_by,
        }
        if evidence is not None:
            payload["evidence"] = evidence
        return self._remember(
            session,
            kind="decision",
            project_id=project_id,
            task_id=task_id,
            execution_id=None,
            source=source,
            summary=decision,
            payload=payload,
        )

    def remember_execution_evidence(
        self,
        session: Session,
        *,
        project_id: str,
        task_id: str,
        execution_id: str,
        summary: str,
        evidence: dict[str, Any],
        source: str = "execution-finalizer",
        commit_hash: str | None = None,
        tree_hash: str | None = None,
    ) -> ProjectMemoryEntry:
        return self._remember(
            session,
            kind="execution_evidence",
            project_id=project_id,
            task_id=task_id,
            execution_id=execution_id,
            source=source,
            summary=summary,
            payload={"evidence": evidence},
            commit_hash=commit_hash,
            tree_hash=tree_hash,
        )

    def list_records(
        self,
        session: Session,
        *,
        project_id: str,
        task_id: str | None = None,
        kinds: tuple[ProjectMemoryKind, ...] | None = None,
    ) -> tuple[ProjectMemoryEntry, ...]:
        self.projects.require(session, project_id)
        statement = (
            select(Checkpoint)
            .join(Task, Checkpoint.task_id == Task.id)
            .where(Task.project_id == project_id, Checkpoint.checkpoint_type == "runtime")
        )
        if task_id is not None:
            statement = statement.where(Checkpoint.task_id == task_id)
        records = [
            entry
            for checkpoint in session.scalars(statement).all()
            if (entry := _project_memory_checkpoint_entry(checkpoint, project_id)) is not None
        ]
        if kinds is not None:
            allowed = set(kinds)
            records = [entry for entry in records if entry.kind in allowed]
        return tuple(sorted(records, key=_entry_sort_key))

    def snapshot(
        self,
        session: Session,
        *,
        project_id: str,
        include_execution_evidence: bool = True,
    ) -> ProjectMemorySnapshot:
        self.projects.require(session, project_id)
        entries: list[ProjectMemoryEntry] = []
        entries.extend(_runtime_plan_entries(session, project_id))
        entries.extend(_runtime_human_gate_entries(session, project_id))
        entries.extend(self.list_records(session, project_id=project_id))
        if include_execution_evidence:
            entries.extend(_execution_checkpoint_entries(session, project_id))
        return ProjectMemorySnapshot(
            project_id=project_id,
            entries=tuple(sorted(entries, key=_entry_sort_key)),
            contract_version=self.contract_version,
            schema_version=self.schema_version,
        )

    def _remember(
        self,
        session: Session,
        *,
        kind: ProjectMemoryKind,
        project_id: str,
        task_id: str,
        execution_id: str | None,
        source: str,
        summary: str,
        payload: dict[str, Any],
        commit_hash: str | None = None,
        tree_hash: str | None = None,
    ) -> ProjectMemoryEntry:
        if not summary.strip():
            raise ProjectMemoryError("project memory summary is required")
        self.projects.require(session, project_id)
        task = self.tasks.require(session, task_id)
        if task.project_id != project_id:
            raise ProjectMemoryError(f"task is not part of project: {task_id}")
        if execution_id is not None:
            execution = self.executions.require(session, execution_id)
            if execution.task_id != task_id:
                raise ProjectMemoryError(f"execution is not part of task: {execution_id}")

        state = _record_state(
            kind=kind,
            project_id=project_id,
            task_id=task_id,
            execution_id=execution_id,
            source=source,
            summary=summary,
            payload=payload,
        )
        checkpoint_id = _memory_checkpoint_id(state)
        existing = self.checkpoints.get(session, checkpoint_id)
        if existing is not None:
            entry = _project_memory_checkpoint_entry(existing, project_id)
            if entry is None:
                raise ProjectMemoryError(f"checkpoint id is already used outside project memory: {checkpoint_id}")
            return entry

        try:
            checkpoint = self.checkpoints.create(
                session,
                checkpoint_id=checkpoint_id,
                task_id=task_id,
                execution_id=execution_id,
                checkpoint_type="runtime",
                state=_canonical_text(state),
                commit_hash=commit_hash,
                tree_hash=tree_hash,
            )
        except DuplicateCheckpointError as exc:
            raise ProjectMemoryError(f"project memory checkpoint conflict: {checkpoint_id}") from exc
        return _project_memory_checkpoint_entry(checkpoint, project_id) or _entry_from_state(
            checkpoint_id,
            state,
            commit_hash=commit_hash,
            tree_hash=tree_hash,
        )


ProjectMemory = ProjectMemoryService


def _runtime_plan_entries(session: Session, project_id: str) -> tuple[ProjectMemoryEntry, ...]:
    receipts = session.scalars(
        select(RuntimePlanImport)
        .where(RuntimePlanImport.project_id == project_id)
        .order_by(RuntimePlanImport.plan_id, RuntimePlanImport.plan_version, RuntimePlanImport.id)
    ).all()
    entries: list[ProjectMemoryEntry] = []
    for receipt in receipts:
        payload = {
            "plan_project_id": receipt.plan_project_id,
            "plan_id": receipt.plan_id,
            "plan_version": receipt.plan_version,
            "status": receipt.status,
            "compiled_project_hash": receipt.compiled_project_hash,
            "capability_resolution_hash": receipt.capability_resolution_hash,
            "trace_validation_hash": receipt.trace_validation_hash,
            "implementation_plan_hash": receipt.implementation_plan_hash,
            "feasibility_hash": receipt.feasibility_hash,
            "dry_run_hash": receipt.dry_run_hash,
            "task_fingerprint_hash": receipt.task_fingerprint_hash,
            "dependency_graph_hash": receipt.dependency_graph_hash,
            "importer_version": receipt.importer_version,
            "task_count": receipt.task_count,
            "dependency_count": receipt.dependency_count,
            "human_gate_count": receipt.human_gate_count,
            "effective_concurrency": receipt.effective_concurrency,
        }
        entries.append(
            ProjectMemoryEntry(
                memory_id=_derived_memory_id("runtime-plan-import", receipt.id, payload),
                kind="context",
                project_id=project_id,
                task_id=None,
                execution_id=None,
                source="runtime-plan-import",
                summary=f"Imported runtime plan {receipt.plan_id} v{receipt.plan_version}",
                payload=payload,
            )
        )
    bindings = session.scalars(
        select(RuntimeTaskPlanBinding)
        .join(Task, RuntimeTaskPlanBinding.task_id == Task.id)
        .where(Task.project_id == project_id)
        .order_by(RuntimeTaskPlanBinding.task_id)
    ).all()
    for binding in bindings:
        payload = {
            "plan_id": binding.plan_id,
            "plan_version": binding.plan_version,
            "fingerprint": binding.fingerprint,
            "risk_level": binding.risk_level,
            "agent_role": binding.agent_role,
            "model_profile": binding.model_profile,
            "executor": binding.executor,
            "verification_profile": binding.verification_profile,
            "feasibility_status": binding.feasibility_status,
            "policy_decision": binding.policy_decision,
            "implements": _json_or_raw(binding.implements_json),
            "write_scope": _json_or_raw(binding.write_scope_json),
            "acceptance": _json_or_raw(binding.acceptance_json),
        }
        entries.append(
            ProjectMemoryEntry(
                memory_id=_derived_memory_id("runtime-task-binding", binding.task_id, payload),
                kind="context",
                project_id=project_id,
                task_id=binding.task_id,
                execution_id=None,
                source="runtime-task-binding",
                summary=f"Imported task context for {binding.task_id}",
                payload=payload,
            )
        )
    return tuple(entries)


def _runtime_human_gate_entries(session: Session, project_id: str) -> tuple[ProjectMemoryEntry, ...]:
    gates = session.scalars(
        select(RuntimeHumanGate)
        .join(Task, RuntimeHumanGate.task_id == Task.id)
        .where(Task.project_id == project_id)
        .order_by(RuntimeHumanGate.task_id, RuntimeHumanGate.id)
    ).all()
    entries: list[ProjectMemoryEntry] = []
    for gate in gates:
        payload = {
            "gate_id": gate.id,
            "plan_id": gate.plan_id,
            "plan_version": gate.plan_version,
            "status": gate.status,
            "reason": gate.reason,
            "risk_level": gate.risk_level,
            "approval_boundary": gate.approval_boundary,
            "expected_evidence": _json_or_raw(gate.expected_evidence_json),
            "downstream_task_ids": _json_or_raw(gate.downstream_task_ids_json),
            "resume_semantics": gate.resume_semantics,
        }
        entries.append(
            ProjectMemoryEntry(
                memory_id=_derived_memory_id("runtime-human-gate", gate.id, payload),
                kind="decision",
                project_id=project_id,
                task_id=gate.task_id,
                execution_id=None,
                source="runtime-human-gate",
                summary=f"Human gate {gate.id} is {gate.status}",
                payload=payload,
            )
        )
    return tuple(entries)


def _execution_checkpoint_entries(session: Session, project_id: str) -> tuple[ProjectMemoryEntry, ...]:
    checkpoints = session.scalars(
        select(Checkpoint)
        .join(Task, Checkpoint.task_id == Task.id)
        .where(Task.project_id == project_id, Checkpoint.checkpoint_type == "execution")
        .order_by(Checkpoint.task_id, Checkpoint.execution_id, Checkpoint.id)
    ).all()
    entries: list[ProjectMemoryEntry] = []
    for checkpoint in checkpoints:
        state = _json_or_raw(checkpoint.state)
        payload = {
            "checkpoint_id": checkpoint.id,
            "checkpoint_type": checkpoint.checkpoint_type,
            "state": state,
        }
        entries.append(
            ProjectMemoryEntry(
                memory_id=_derived_memory_id("execution-checkpoint", checkpoint.id, payload),
                kind="execution_evidence",
                project_id=project_id,
                task_id=checkpoint.task_id,
                execution_id=checkpoint.execution_id,
                source="execution-checkpoint",
                summary=_execution_summary(checkpoint, state),
                payload=payload,
                commit_hash=checkpoint.commit_hash,
                tree_hash=checkpoint.tree_hash,
            )
        )
    return tuple(entries)


def _project_memory_checkpoint_entry(
    checkpoint: Checkpoint,
    project_id: str,
) -> ProjectMemoryEntry | None:
    if checkpoint.checkpoint_type != "runtime":
        return None
    try:
        state = json.loads(checkpoint.state)
    except json.JSONDecodeError:
        return None
    if not _is_project_memory_state(state):
        return None
    if state.get("project_id") != project_id:
        return None
    return _entry_from_state(
        checkpoint.id,
        state,
        commit_hash=checkpoint.commit_hash,
        tree_hash=checkpoint.tree_hash,
    )


def _entry_from_state(
    memory_id: str,
    state: dict[str, Any],
    *,
    commit_hash: str | None,
    tree_hash: str | None,
) -> ProjectMemoryEntry:
    return ProjectMemoryEntry(
        memory_id=memory_id,
        kind=state["kind"],
        project_id=state["project_id"],
        task_id=state.get("task_id"),
        execution_id=state.get("execution_id"),
        source=state["source"],
        summary=state["summary"],
        payload=state["payload"],
        commit_hash=commit_hash,
        tree_hash=tree_hash,
    )


def _record_state(
    *,
    kind: ProjectMemoryKind,
    project_id: str,
    task_id: str,
    execution_id: str | None,
    source: str,
    summary: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    state = {
        "generated": GENERATED_MARKER,
        "component_id": PROJECT_MEMORY_COMPONENT_ID,
        "capability_id": PROJECT_MEMORY_CAPABILITY_ID,
        "requirements": [PROJECT_MEMORY_REQUIREMENT_ID],
        "contract_version": PROJECT_MEMORY_CONTRACT_VERSION,
        "schema_version": PROJECT_MEMORY_RECORD_SCHEMA_VERSION,
        "service": PROJECT_MEMORY_SERVICE_NAME,
        "interfaces": [
            WORKER_PACKAGE_INTERFACE_ID,
            EVIDENCE_RECORDING_INTERFACE_ID,
        ],
        "kind": kind,
        "project_id": project_id,
        "task_id": task_id,
        "execution_id": execution_id,
        "source": source,
        "summary": summary.strip(),
        "payload": payload,
    }
    try:
        canonical_bytes(state)
    except TypeError as exc:
        raise ProjectMemoryError("project memory payload must be JSON canonicalizable") from exc
    return state


def _memory_checkpoint_id(state: dict[str, Any]) -> str:
    return _derived_memory_id("runtime-checkpoint", state["project_id"], state)


def _derived_memory_id(source: str, identity: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        canonical_bytes(
            {
                "source": source,
                "identity": identity,
                "payload": payload,
            }
        )
    ).hexdigest()
    return f"pmem-{digest[:32]}"


def _canonical_text(value: dict[str, Any]) -> str:
    return canonical_bytes(value).decode("utf-8")


def _is_project_memory_state(state: Any) -> bool:
    return (
        isinstance(state, dict)
        and state.get("service") == PROJECT_MEMORY_SERVICE_NAME
        and state.get("component_id") == PROJECT_MEMORY_COMPONENT_ID
        and state.get("capability_id") == PROJECT_MEMORY_CAPABILITY_ID
        and state.get("schema_version") == PROJECT_MEMORY_RECORD_SCHEMA_VERSION
        and state.get("kind") in {"context", "decision", "execution_evidence"}
        and isinstance(state.get("payload"), dict)
        and isinstance(state.get("summary"), str)
        and isinstance(state.get("source"), str)
        and isinstance(state.get("project_id"), str)
    )


def _json_or_raw(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _execution_summary(checkpoint: Checkpoint, state: Any) -> str:
    if isinstance(state, dict) and isinstance(state.get("status"), str):
        return f"Execution checkpoint {checkpoint.id} recorded {state['status']}"
    return f"Execution checkpoint {checkpoint.id} recorded evidence"


def _kind_count(entries: tuple[ProjectMemoryEntry, ...], kind: ProjectMemoryKind) -> int:
    return sum(1 for entry in entries if entry.kind == kind)


def _entry_sort_key(entry: ProjectMemoryEntry) -> tuple[str, str, str, str, str]:
    return (
        entry.source,
        entry.task_id or "",
        entry.execution_id or "",
        entry.kind,
        entry.memory_id,
    )


def project_memory_snapshot(
    session: Session,
    *,
    project_id: str,
    include_execution_evidence: bool = True,
) -> ProjectMemorySnapshot:
    return ProjectMemoryService().snapshot(
        session,
        project_id=project_id,
        include_execution_evidence=include_execution_evidence,
    )
