from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_ent.persistence.models import (
    RuntimeHumanGate,
    RuntimeTaskPlanBinding,
    Task,
    TaskDependency,
    TaskLease,
)
from ai_ent.persistence.repositories.tasks import TaskRepository
from ai_ent.scheduler.claiming import database_now

ReadinessStatus = Literal[
    "READY",
    "WAITING_DEPENDENCIES",
    "BLOCKED_DEPENDENCY",
    "NOT_SCHEDULABLE",
    "DECISION_BLOCKED",
    "TERMINAL",
    "RUNNING",
    "ALREADY_LEASED",
    "NOT_FOUND",
    "INVALID_GRAPH_STATE",
]

SATISFIED_DEPENDENCY_STATUSES = {"passed"}
BLOCKING_DEPENDENCY_STATUSES = {"failed", "blocked"}
TERMINAL_TASK_STATUSES = {"passed", "failed", "blocked"}


@dataclass(frozen=True)
class ReadinessDecision:
    task_id: str
    status: ReadinessStatus
    reasons: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    blocking_dependencies: tuple[str, ...] = ()
    waiting_dependencies: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.status == "READY"


@dataclass(frozen=True)
class _ProjectGraph:
    tasks: dict[str, Task]
    dependencies: dict[str, set[str]] = field(default_factory=dict)
    active_leased_task_ids: frozenset[str] = frozenset()
    cycle_tasks: frozenset[str] = frozenset()
    pending_gate_task_ids: frozenset[str] = frozenset()
    unresolved_decisions_by_task: dict[str, tuple[str, ...]] = field(default_factory=dict)


class TaskReadinessService:
    """Derived PostgreSQL-backed readiness policy for scheduler selection."""

    def __init__(self, tasks: TaskRepository | None = None) -> None:
        self.tasks = tasks or TaskRepository()

    def evaluate_task(self, session: Session, task_id: str) -> ReadinessDecision:
        task = self.tasks.get(session, task_id)
        if task is None:
            return ReadinessDecision(task_id=task_id, status="NOT_FOUND", reasons=("task_missing",))
        graph = self._load_project_graph(session, task.project_id)
        return self._evaluate_loaded(task_id, graph)

    def explain_not_ready(self, session: Session, task_id: str) -> ReadinessDecision:
        return self.evaluate_task(session, task_id)

    def list_ready_tasks(
        self,
        session: Session,
        *,
        project_id: str,
        limit: int | None = None,
    ) -> list[Task]:
        graph = self._load_project_graph(session, project_id)
        ready: list[Task] = []
        for task in sorted(graph.tasks.values(), key=lambda item: (item.created_at, item.id)):
            if self._evaluate_loaded(task.id, graph).ready:
                ready.append(task)
                if limit is not None and len(ready) >= limit:
                    break
        return ready

    def _evaluate_loaded(self, task_id: str, graph: _ProjectGraph) -> ReadinessDecision:
        task = graph.tasks.get(task_id)
        if task is None:
            return ReadinessDecision(
                task_id=task_id,
                status="INVALID_GRAPH_STATE",
                reasons=("dependency_target_missing",),
            )
        if task_id in graph.cycle_tasks:
            return ReadinessDecision(
                task_id=task_id,
                status="INVALID_GRAPH_STATE",
                reasons=("dependency_cycle",),
                dependencies=tuple(sorted(graph.dependencies.get(task_id, set()))),
            )
        if not task.schedulable:
            return ReadinessDecision(task_id=task_id, status="NOT_SCHEDULABLE", reasons=("not_schedulable",))
        if task_id in graph.pending_gate_task_ids:
            return ReadinessDecision(task_id=task_id, status="NOT_SCHEDULABLE", reasons=("pending_human_gate",))
        if task.execution_class != "implementation":
            return ReadinessDecision(
                task_id=task_id,
                status="NOT_SCHEDULABLE",
                reasons=("non_implementation_execution_class",),
            )
        if task.status in TERMINAL_TASK_STATUSES:
            return ReadinessDecision(task_id=task_id, status="TERMINAL", reasons=(f"task_{task.status}",))
        if task_id in graph.active_leased_task_ids:
            return ReadinessDecision(task_id=task_id, status="ALREADY_LEASED", reasons=("active_valid_lease",))
        if task.status == "running":
            return ReadinessDecision(task_id=task_id, status="RUNNING", reasons=("task_running",))
        if task.status != "pending":
            return ReadinessDecision(
                task_id=task_id,
                status="INVALID_GRAPH_STATE",
                reasons=(f"unknown_task_status:{task.status}",),
            )

        dependencies = graph.dependencies.get(task_id, set())
        missing = sorted(dependency for dependency in dependencies if dependency not in graph.tasks)
        if missing:
            return ReadinessDecision(
                task_id=task_id,
                status="INVALID_GRAPH_STATE",
                reasons=("dependency_target_missing",),
                dependencies=tuple(sorted(dependencies)),
                blocking_dependencies=tuple(missing),
            )

        blocking = sorted(
            dependency for dependency in dependencies if graph.tasks[dependency].status in BLOCKING_DEPENDENCY_STATUSES
        )
        if blocking:
            return ReadinessDecision(
                task_id=task_id,
                status="BLOCKED_DEPENDENCY",
                reasons=("dependency_failed_or_blocked",),
                dependencies=tuple(sorted(dependencies)),
                blocking_dependencies=tuple(blocking),
            )

        waiting = sorted(
            dependency
            for dependency in dependencies
            if graph.tasks[dependency].status not in SATISFIED_DEPENDENCY_STATUSES
        )
        if waiting:
            return ReadinessDecision(
                task_id=task_id,
                status="WAITING_DEPENDENCIES",
                reasons=("dependency_not_complete",),
                dependencies=tuple(sorted(dependencies)),
                waiting_dependencies=tuple(waiting),
            )

        unresolved_decisions = graph.unresolved_decisions_by_task.get(task_id, ())
        if unresolved_decisions:
            return ReadinessDecision(
                task_id=task_id,
                status="DECISION_BLOCKED",
                reasons=tuple(f"unresolved_decision:{decision_id}" for decision_id in unresolved_decisions),
                dependencies=tuple(sorted(dependencies)),
            )

        return ReadinessDecision(
            task_id=task_id,
            status="READY",
            reasons=("claimable",),
            dependencies=tuple(sorted(dependencies)),
        )

    def _load_project_graph(self, session: Session, project_id: str) -> _ProjectGraph:
        tasks = {
            task.id: task
            for task in session.scalars(select(Task).where(Task.project_id == project_id).order_by(Task.created_at, Task.id))
        }
        dependencies: dict[str, set[str]] = {task_id: set() for task_id in tasks}
        dependency_rows = session.execute(
            select(TaskDependency.task_id, TaskDependency.depends_on_task_id)
            .join(Task, TaskDependency.task_id == Task.id)
            .where(Task.project_id == project_id)
        )
        for task_id, depends_on_task_id in dependency_rows:
            dependencies.setdefault(task_id, set()).add(depends_on_task_id)
        now = database_now(session)
        active_leased_task_ids = set(
            session.scalars(
                select(TaskLease.task_id)
                .join(Task, TaskLease.task_id == Task.id)
                .where(
                    Task.project_id == project_id,
                    TaskLease.status == "active",
                    TaskLease.expires_at > now,
                )
            ).all()
        )
        pending_gate_task_ids = set(
            session.scalars(
                select(RuntimeHumanGate.task_id)
                .join(Task, RuntimeHumanGate.task_id == Task.id)
                .where(Task.project_id == project_id, RuntimeHumanGate.status == "pending")
            ).all()
        )
        unresolved_decisions_by_task = _unresolved_decisions_by_task(session, project_id)
        return _ProjectGraph(
            tasks=tasks,
            dependencies=dependencies,
            active_leased_task_ids=frozenset(active_leased_task_ids),
            cycle_tasks=frozenset(_cycle_nodes(dependencies)),
            pending_gate_task_ids=frozenset(pending_gate_task_ids),
            unresolved_decisions_by_task=unresolved_decisions_by_task,
        )


def _cycle_nodes(dependencies: dict[str, set[str]]) -> set[str]:
    visiting: set[str] = set()
    visited: set[str] = set()
    cycle_members: set[str] = set()

    def visit(task_id: str, path: list[str]) -> None:
        if task_id in visiting:
            cycle_start = path.index(task_id)
            cycle_members.update(path[cycle_start:])
            return
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in dependencies.get(task_id, set()):
            if dependency in dependencies:
                visit(dependency, [*path, dependency])
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in dependencies:
        visit(task_id, [task_id])
    return cycle_members


def _unresolved_decisions_by_task(session: Session, project_id: str) -> dict[str, tuple[str, ...]]:
    unresolved: dict[str, tuple[str, ...]] = {}
    rows = session.execute(
        select(RuntimeTaskPlanBinding.task_id, RuntimeTaskPlanBinding.acceptance_json)
        .join(Task, RuntimeTaskPlanBinding.task_id == Task.id)
        .where(Task.project_id == project_id)
    )
    for task_id, acceptance_json in rows:
        try:
            payload = json.loads(acceptance_json)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        required = payload.get("required_decisions", [])
        states = payload.get("decision_states", {})
        if not isinstance(required, list) or not isinstance(states, dict):
            continue
        decision_ids = []
        for marker in required:
            decision_id = str(marker).removeprefix("DECISION_REQUIRED:")
            state = states.get(decision_id, {})
            if isinstance(state, dict) and str(state.get("state", "")).startswith("UNRESOLVED_"):
                decision_ids.append(decision_id)
        if decision_ids:
            unresolved[task_id] = tuple(sorted(set(decision_ids)))
    return unresolved
