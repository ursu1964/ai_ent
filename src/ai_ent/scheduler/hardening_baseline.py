from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import desc, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ai_ent.persistence.models import Checkpoint, RuntimeTaskPlanBinding, Task, utc_now

BASELINE_CHECKPOINT_KIND = "hardening_baseline_state"

BaselineAdvanceStatus = Literal[
    "ADVANCED",
    "ALREADY_REPRESENTED",
    "STALE_ADVANCEMENT",
    "PLAN_NOT_FOUND",
    "TASK_NOT_FOUND",
    "TASK_NOT_PASSED",
    "BASELINE_POLICY_MISSING",
    "BASELINE_LOCKED",
]


@dataclass(frozen=True)
class HardeningBaselineState:
    plan_id: str
    plan_version: str
    owner_task_id: str
    baseline_commit: str
    baseline_tree: str
    generation: int
    integrated_task_ids: tuple[str, ...] = ()
    task_commits: dict[str, str] | None = None
    task_trees: dict[str, str] | None = None

    def represents(self, task_id: str) -> bool:
        return task_id in set(self.integrated_task_ids)

    def as_checkpoint_state(self) -> str:
        payload = {
            "record_kind": BASELINE_CHECKPOINT_KIND,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "owner_task_id": self.owner_task_id,
            "baseline_commit": self.baseline_commit,
            "baseline_tree": self.baseline_tree,
            "generation": self.generation,
            "integrated_task_ids": list(self.integrated_task_ids),
            "task_commits": dict(sorted((self.task_commits or {}).items())),
            "task_trees": dict(sorted((self.task_trees or {}).items())),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class BaselineAdvanceResult:
    status: BaselineAdvanceStatus
    state: HardeningBaselineState | None = None
    reason: str | None = None

    @property
    def advanced(self) -> bool:
        return self.status == "ADVANCED"


class HardeningBaselineService:
    """PostgreSQL-backed cumulative hardening baseline policy."""

    def requires_policy(self, binding: RuntimeTaskPlanBinding) -> bool:
        return _initial_state_from_binding(binding) is not None

    def current_for_task(self, session: Session, task_id: str) -> HardeningBaselineState | None:
        binding = session.get(RuntimeTaskPlanBinding, task_id)
        if binding is None:
            return None
        return self.current_for_binding(session, binding)

    def current_for_binding(
        self,
        session: Session,
        binding: RuntimeTaskPlanBinding,
    ) -> HardeningBaselineState | None:
        initial = _initial_state_from_binding(binding)
        if initial is None:
            return None
        checkpoint = _latest_baseline_checkpoint(session, initial)
        if checkpoint is None:
            return initial
        parsed = _state_from_checkpoint(checkpoint)
        return parsed if parsed is not None else initial

    def blockers_by_task(
        self,
        session: Session,
        *,
        tasks: dict[str, Task],
        dependencies: dict[str, set[str]],
    ) -> dict[str, tuple[str, ...]]:
        bindings = {
            binding.task_id: binding
            for binding in session.scalars(
                select(RuntimeTaskPlanBinding).where(RuntimeTaskPlanBinding.task_id.in_(tuple(tasks)))
            ).all()
        }
        states: dict[tuple[str, str], HardeningBaselineState | None] = {}
        blockers: dict[str, tuple[str, ...]] = {}
        for task_id, task in tasks.items():
            binding = bindings.get(task_id)
            if binding is None:
                continue
            initial = _initial_state_from_binding(binding)
            if initial is None:
                continue
            key = (binding.plan_id, binding.plan_version)
            if key not in states:
                states[key] = self.current_for_binding(session, binding)
            state = states[key]
            if state is None:
                continue
            missing = tuple(
                sorted(
                    dependency
                    for dependency in dependencies.get(task_id, set())
                    if tasks.get(dependency) is not None
                    and tasks[dependency].status == "passed"
                    and not state.represents(dependency)
                )
            )
            if missing and task.status == "pending":
                blockers[task_id] = missing
        return blockers

    def blocking_dependencies_for_claim(self, session: Session, task: Task) -> tuple[str, ...]:
        dependencies = {
            dependency.depends_on_task_id
            for dependency in task.dependencies
        }
        if not dependencies:
            return ()
        return self.blockers_by_task(
            session,
            tasks={task.id: task, **{dependency.id: dependency for dependency in _tasks(session, dependencies)}},
            dependencies={task.id: dependencies},
        ).get(task.id, ())

    def advance(
        self,
        session: Session,
        *,
        plan_id: str,
        plan_version: str,
        task_id: str,
        task_commit: str,
        task_tree: str,
        resulting_baseline_commit: str,
        resulting_baseline_tree: str,
        expected_generation: int,
    ) -> BaselineAdvanceResult:
        binding = session.scalar(
            select(RuntimeTaskPlanBinding)
            .where(
                RuntimeTaskPlanBinding.plan_id == plan_id,
                RuntimeTaskPlanBinding.plan_version == plan_version,
            )
            .order_by(RuntimeTaskPlanBinding.task_id)
            .limit(1)
        )
        if binding is None:
            return BaselineAdvanceResult("PLAN_NOT_FOUND", reason="plan_not_found")
        initial = _initial_state_from_binding(binding)
        if initial is None:
            return BaselineAdvanceResult("BASELINE_POLICY_MISSING", reason="baseline_policy_missing")
        try:
            owner = session.scalars(
                select(Task).where(Task.id == initial.owner_task_id).with_for_update(nowait=True)
            ).one_or_none()
        except OperationalError:
            return BaselineAdvanceResult("BASELINE_LOCKED", reason="baseline_owner_locked")
        if owner is None:
            return BaselineAdvanceResult("TASK_NOT_FOUND", reason=f"owner_task_missing:{initial.owner_task_id}")

        current = self.current_for_binding(session, binding)
        if current is None:
            return BaselineAdvanceResult("BASELINE_POLICY_MISSING", reason="baseline_policy_missing")
        if current.generation != expected_generation:
            return BaselineAdvanceResult("STALE_ADVANCEMENT", state=current, reason="baseline_generation_changed")
        if current.represents(task_id):
            return BaselineAdvanceResult("ALREADY_REPRESENTED", state=current, reason=f"task_already_integrated:{task_id}")

        task = session.get(Task, task_id)
        if task is None:
            return BaselineAdvanceResult("TASK_NOT_FOUND", state=current, reason=f"task_missing:{task_id}")
        if task.status != "passed":
            return BaselineAdvanceResult("TASK_NOT_PASSED", state=current, reason=f"task_not_passed:{task_id}")

        task_commits = dict(current.task_commits or {})
        task_trees = dict(current.task_trees or {})
        task_commits[task_id] = task_commit
        task_trees[task_id] = task_tree
        next_state = HardeningBaselineState(
            plan_id=current.plan_id,
            plan_version=current.plan_version,
            owner_task_id=current.owner_task_id,
            baseline_commit=resulting_baseline_commit,
            baseline_tree=resulting_baseline_tree,
            generation=current.generation + 1,
            integrated_task_ids=tuple(sorted((*current.integrated_task_ids, task_id))),
            task_commits=task_commits,
            task_trees=task_trees,
        )
        session.add(
            Checkpoint(
                id=_checkpoint_id(next_state),
                task_id=current.owner_task_id,
                checkpoint_type="runtime",
                state=next_state.as_checkpoint_state(),
                commit_hash=resulting_baseline_commit,
                tree_hash=resulting_baseline_tree,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        session.flush()
        return BaselineAdvanceResult("ADVANCED", state=next_state)


def _tasks(session: Session, task_ids: set[str]) -> tuple[Task, ...]:
    if not task_ids:
        return ()
    return tuple(session.scalars(select(Task).where(Task.id.in_(tuple(task_ids)))).all())


def _initial_state_from_binding(binding: RuntimeTaskPlanBinding) -> HardeningBaselineState | None:
    acceptance = _json_object(binding.acceptance_json)
    policy = acceptance.get("cumulative_baseline_policy")
    if not isinstance(policy, dict) or policy.get("on_violation") != "STOP_BEFORE_CLAIM":
        return None
    baseline_commit = acceptance.get("baseline_commit")
    baseline_tree = acceptance.get("baseline_tree")
    owner_task_id = policy.get("owner_task")
    if not all(isinstance(item, str) and item for item in (baseline_commit, baseline_tree, owner_task_id)):
        return None
    return HardeningBaselineState(
        plan_id=binding.plan_id,
        plan_version=binding.plan_version,
        owner_task_id=str(owner_task_id),
        baseline_commit=str(baseline_commit),
        baseline_tree=str(baseline_tree),
        generation=0,
    )


def _latest_baseline_checkpoint(
    session: Session,
    initial: HardeningBaselineState,
) -> Checkpoint | None:
    rows = session.scalars(
        select(Checkpoint)
        .where(Checkpoint.task_id == initial.owner_task_id, Checkpoint.checkpoint_type == "runtime")
        .order_by(desc(Checkpoint.created_at), desc(Checkpoint.id))
    ).all()
    for row in rows:
        state = _state_from_checkpoint(row)
        if state is not None and state.plan_id == initial.plan_id and state.plan_version == initial.plan_version:
            return row
    return None


def _state_from_checkpoint(checkpoint: Checkpoint) -> HardeningBaselineState | None:
    payload = _json_object(checkpoint.state)
    if payload.get("record_kind") != BASELINE_CHECKPOINT_KIND:
        return None
    required = ("plan_id", "plan_version", "owner_task_id", "baseline_commit", "baseline_tree")
    if not all(isinstance(payload.get(key), str) and payload.get(key) for key in required):
        return None
    generation = payload.get("generation")
    if not isinstance(generation, int) or generation < 0:
        return None
    integrated = payload.get("integrated_task_ids", [])
    if not isinstance(integrated, list) or not all(isinstance(item, str) for item in integrated):
        return None
    task_commits = payload.get("task_commits", {})
    task_trees = payload.get("task_trees", {})
    return HardeningBaselineState(
        plan_id=str(payload["plan_id"]),
        plan_version=str(payload["plan_version"]),
        owner_task_id=str(payload["owner_task_id"]),
        baseline_commit=str(payload["baseline_commit"]),
        baseline_tree=str(payload["baseline_tree"]),
        generation=generation,
        integrated_task_ids=tuple(sorted(integrated)),
        task_commits=task_commits if isinstance(task_commits, dict) else {},
        task_trees=task_trees if isinstance(task_trees, dict) else {},
    )


def _checkpoint_id(state: HardeningBaselineState) -> str:
    digest = hashlib.sha256(f"{state.plan_id}:{state.plan_version}".encode()).hexdigest()[:20]
    return f"hbl-{digest}-g{state.generation:06d}"


def _json_object(raw: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
