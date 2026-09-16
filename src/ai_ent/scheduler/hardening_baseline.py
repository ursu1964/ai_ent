from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import desc, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ai_ent.bootstrap.git import require_git
from ai_ent.bootstrap.paths import ROOT
from ai_ent.persistence.models import (
    Checkpoint,
    Execution,
    RuntimeBaselineIntegration,
    RuntimeTaskPlanBinding,
    Task,
    utc_now,
)

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
    "EXECUTION_NOT_FOUND",
    "EXECUTION_NOT_VERIFIED",
    "INTEGRATION_NOT_FOUND",
    "INTEGRATION_NOT_VERIFIED",
    "PROVENANCE_MISMATCH",
    "GIT_OBJECT_INVALID",
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


@dataclass(frozen=True)
class _ValidatedAdvancement:
    status: BaselineAdvanceStatus | None = None
    reason: str | None = None
    task_commit: str | None = None
    task_tree: str | None = None
    baseline_commit: str | None = None
    baseline_tree: str | None = None


class HardeningBaselineService:
    """PostgreSQL-backed cumulative hardening baseline policy."""

    def __init__(self, *, repository_path: Path = ROOT) -> None:
        self.repository_path = repository_path

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
        integration_id: str,
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

        task = session.get(Task, task_id)
        if task is None:
            return BaselineAdvanceResult("TASK_NOT_FOUND", state=current, reason=f"task_missing:{task_id}")
        if task.status != "passed":
            return BaselineAdvanceResult("TASK_NOT_PASSED", state=current, reason=f"task_not_passed:{task_id}")

        provenance = _validate_advancement_provenance(
            session,
            repository_path=self.repository_path,
            binding=binding,
            current=current,
            task=task,
            task_commit=task_commit,
            task_tree=task_tree,
            resulting_baseline_commit=resulting_baseline_commit,
            resulting_baseline_tree=resulting_baseline_tree,
            expected_generation=expected_generation,
            integration_id=integration_id,
        )
        if provenance.status is not None:
            return BaselineAdvanceResult(provenance.status, state=current, reason=provenance.reason)

        assert provenance.task_commit is not None
        assert provenance.task_tree is not None
        assert provenance.baseline_commit is not None
        assert provenance.baseline_tree is not None

        if current.represents(task_id):
            if (
                (current.task_commits or {}).get(task_id) == provenance.task_commit
                and (current.task_trees or {}).get(task_id) == provenance.task_tree
                and current.baseline_commit == provenance.baseline_commit
                and current.baseline_tree == provenance.baseline_tree
            ):
                return BaselineAdvanceResult(
                    "ALREADY_REPRESENTED",
                    state=current,
                    reason=f"task_already_integrated:{task_id}",
                )
            return BaselineAdvanceResult("PROVENANCE_MISMATCH", state=current, reason="task_integrated_with_different_provenance")

        if current.generation != expected_generation:
            return BaselineAdvanceResult("STALE_ADVANCEMENT", state=current, reason="baseline_generation_changed")

        task_commits = dict(current.task_commits or {})
        task_trees = dict(current.task_trees or {})
        task_commits[task_id] = provenance.task_commit
        task_trees[task_id] = provenance.task_tree
        next_state = HardeningBaselineState(
            plan_id=current.plan_id,
            plan_version=current.plan_version,
            owner_task_id=current.owner_task_id,
            baseline_commit=provenance.baseline_commit,
            baseline_tree=provenance.baseline_tree,
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
                commit_hash=provenance.baseline_commit,
                tree_hash=provenance.baseline_tree,
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


def _validate_advancement_provenance(
    session: Session,
    *,
    repository_path: Path,
    binding: RuntimeTaskPlanBinding,
    current: HardeningBaselineState,
    task: Task,
    task_commit: str,
    task_tree: str,
    resulting_baseline_commit: str,
    resulting_baseline_tree: str,
    expected_generation: int,
    integration_id: str,
) -> _ValidatedAdvancement:
    integration = session.get(RuntimeBaselineIntegration, integration_id)
    if integration is None:
        return _ValidatedAdvancement("INTEGRATION_NOT_FOUND", f"integration_missing:{integration_id}")
    if integration.status != "verified":
        return _ValidatedAdvancement("INTEGRATION_NOT_VERIFIED", f"integration_not_verified:{integration_id}")
    if (
        integration.plan_id != binding.plan_id
        or integration.plan_version != binding.plan_version
        or integration.import_id != binding.import_id
        or integration.project_id != task.project_id
        or integration.task_id != task.id
    ):
        return _ValidatedAdvancement("PROVENANCE_MISMATCH", "integration_task_plan_binding_mismatch")
    if integration.prior_baseline_generation != expected_generation:
        return _ValidatedAdvancement("PROVENANCE_MISMATCH", "integration_prior_baseline_mismatch")
    if (
        integration.source_commit != task_commit
        or integration.source_tree != task_tree
        or integration.integrated_commit != resulting_baseline_commit
        or integration.integrated_tree != resulting_baseline_tree
    ):
        return _ValidatedAdvancement("PROVENANCE_MISMATCH", "caller_integration_provenance_mismatch")

    execution = session.get(Execution, integration.source_execution_id)
    if execution is None:
        return _ValidatedAdvancement("EXECUTION_NOT_FOUND", f"execution_missing:{integration.source_execution_id}")
    if execution.task_id != task.id:
        return _ValidatedAdvancement("PROVENANCE_MISMATCH", "execution_task_mismatch")
    if execution.status != "succeeded" or execution.terminal_state != "success":
        return _ValidatedAdvancement("EXECUTION_NOT_VERIFIED", f"execution_not_verified:{execution.id}")
    if not execution.commit_hash or not execution.candidate_tree_hash:
        return _ValidatedAdvancement("EXECUTION_NOT_VERIFIED", f"execution_missing_commit_or_tree:{execution.id}")
    if (
        execution.baseline_generation != expected_generation
        or execution.baseline_commit != integration.prior_baseline_commit
        or execution.baseline_tree != integration.prior_baseline_tree
    ):
        return _ValidatedAdvancement("PROVENANCE_MISMATCH", "execution_baseline_mismatch")

    checkpoint = _latest_success_checkpoint(session, execution)
    if checkpoint is None:
        return _ValidatedAdvancement("EXECUTION_NOT_VERIFIED", f"execution_success_checkpoint_missing:{execution.id}")
    checkpoint_state = _json_object(checkpoint.state)
    if checkpoint_state.get("status") != "completed":
        return _ValidatedAdvancement("EXECUTION_NOT_VERIFIED", f"execution_checkpoint_not_completed:{execution.id}")
    if not _verification_succeeded(checkpoint_state.get("verification")):
        return _ValidatedAdvancement("EXECUTION_NOT_VERIFIED", f"execution_verification_missing_or_failed:{execution.id}")
    committed_tree = checkpoint_state.get("committed_tree_hash")
    candidate_tree = checkpoint_state.get("candidate_tree_hash")
    if not isinstance(committed_tree, str) or not isinstance(candidate_tree, str) or committed_tree != candidate_tree:
        return _ValidatedAdvancement("PROVENANCE_MISMATCH", "candidate_committed_tree_mismatch")
    if candidate_tree != execution.candidate_tree_hash:
        return _ValidatedAdvancement("PROVENANCE_MISMATCH", "execution_tree_checkpoint_mismatch")

    source = _resolve_commit_tree(repository_path, integration.source_commit, integration.source_tree)
    if source is None:
        return _ValidatedAdvancement("GIT_OBJECT_INVALID", "source_commit_or_tree_invalid")
    integrated = _resolve_commit_tree(repository_path, integration.integrated_commit, integration.integrated_tree)
    if integrated is None:
        return _ValidatedAdvancement("GIT_OBJECT_INVALID", "integrated_commit_or_tree_invalid")
    execution_commit = _resolve_commit(repository_path, execution.commit_hash)
    if execution_commit is None:
        return _ValidatedAdvancement("GIT_OBJECT_INVALID", "execution_commit_invalid")
    if source[0] != execution_commit or source[1] != execution.candidate_tree_hash:
        return _ValidatedAdvancement("PROVENANCE_MISMATCH", "execution_source_git_identity_mismatch")

    return _ValidatedAdvancement(
        task_commit=source[0],
        task_tree=source[1],
        baseline_commit=integrated[0],
        baseline_tree=integrated[1],
    )


def _latest_success_checkpoint(session: Session, execution: Execution) -> Checkpoint | None:
    return session.scalars(
        select(Checkpoint)
        .where(Checkpoint.execution_id == execution.id, Checkpoint.checkpoint_type == "execution")
        .order_by(desc(Checkpoint.created_at), desc(Checkpoint.id))
        .limit(1)
    ).first()


def _verification_succeeded(raw: object) -> bool:
    if not isinstance(raw, list) or not raw:
        return False
    return all(isinstance(item, dict) and item.get("ok") is True for item in raw)


def _resolve_commit_tree(repository_path: Path, commit: str, tree: str) -> tuple[str, str] | None:
    resolved_commit = _resolve_commit(repository_path, commit)
    if resolved_commit is None:
        return None
    resolved_tree = _resolve_tree(repository_path, tree)
    if resolved_tree is None:
        return None
    try:
        commit_tree = require_git(["rev-parse", f"{resolved_commit}^{{tree}}"], cwd=repository_path)
    except RuntimeError:
        return None
    return (resolved_commit, resolved_tree) if commit_tree == resolved_tree else None


def _resolve_commit(repository_path: Path, commit: str) -> str | None:
    try:
        object_type = require_git(["cat-file", "-t", commit], cwd=repository_path)
        if object_type != "commit":
            return None
        return require_git(["rev-parse", f"{commit}^{{commit}}"], cwd=repository_path)
    except RuntimeError:
        return None


def _resolve_tree(repository_path: Path, tree: str) -> str | None:
    try:
        object_type = require_git(["cat-file", "-t", tree], cwd=repository_path)
        if object_type != "tree":
            return None
        return require_git(["rev-parse", f"{tree}^{{tree}}"], cwd=repository_path)
    except RuntimeError:
        return None


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
